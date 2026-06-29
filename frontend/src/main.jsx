import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const tabs = ["Control", "Validation", "Logs"];
const themeKey = "videosim-theme";

function readState() {
  const node = document.getElementById("initial-state");
  return JSON.parse(node?.textContent || "{}");
}

function readTheme() {
  try {
    return localStorage.getItem(themeKey) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

function applyTheme(theme) {
  document.body.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

const initialTheme = readTheme();
applyTheme(initialTheme);

function App() {
  const [state, setState] = useState(() => readState());
  const [tab, setTab] = useState("Control");
  const [theme, setTheme] = useState(initialTheme);
  const [copied, setCopied] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(state.status === "running");
  const [previewTick, setPreviewTick] = useState(Date.now());
  const mode = useMemo(
    () => state.modes.find((item) => item.value === state.mode) || state.modes[0],
    [state.mode, state.modes]
  );
  const selectedStream = useMemo(
    () => state.streams.find((item) => item.id === state.selectedStreamId),
    [state.selectedStreamId, state.streams]
  );

  async function copyEndpoint() {
    await navigator.clipboard.writeText(state.endpoint);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  }

  function toggleTheme() {
    setTheme(theme === "dark" ? "light" : "dark");
  }

  useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem(themeKey, theme);
    } catch {
      return;
    }
  }, [theme]);

  useEffect(() => {
    let active = true;
    async function refreshState() {
      try {
        const response = await fetch("/state.json", { cache: "no-store" });
        if (active && response.ok) {
          setState(await response.json());
        }
      } catch {
        return;
      }
    }
    const timer = setInterval(refreshState, 1000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!previewOpen || state.status !== "running" || !state.previewAvailable) {
      return undefined;
    }
    const timer = setInterval(() => setPreviewTick(Date.now()), 1800);
    return () => clearInterval(timer);
  }, [previewOpen, state.previewAvailable, state.status]);

  return (
    <div className="app-shell">
      <aside className="nav">
        <div>
          <p className="eyebrow">VideoSim</p>
          <h1>Feed Console</h1>
        </div>
        <button aria-pressed={theme === "light"} className="theme-toggle" onClick={toggleTheme} type="button">
          <span aria-hidden="true" className="theme-dot" />
          {theme === "dark" ? "Light mode" : "Dark mode"}
        </button>
        <nav aria-label="Main">
          {tabs.map((item) => (
            <button className={item === tab ? "active" : ""} key={item} onClick={() => setTab(item)} type="button">
              {item}
            </button>
          ))}
        </nav>
        <StreamNav streams={state.streams} selectedStreamId={state.selectedStreamId} />
        <a className="download" href="/diagnostics.txt">Download diagnostics</a>
      </aside>

      <main className="workspace">
        <section className="hero">
          <div>
            <p className="eyebrow">Current profile</p>
            <h2>{selectedStream?.name || "Create feed"}</h2>
            <span>{selectedStream ? `${mode?.label || state.mode} · ${state.protocol.toUpperCase()}` : "No feeds configured"}</span>
          </div>
          <StatusPill status={state.status} />
        </section>

        {state.status === "running" && (
          <PreviewPopup
            open={previewOpen}
            previewAvailable={state.previewAvailable}
            previewUrl={`${state.previewUrl}?t=${previewTick}`}
            onClose={() => setPreviewOpen(false)}
            onOpen={() => setPreviewOpen(true)}
          />
        )}

        <section className="metrics">
          <Metric label="Outage" value={state.intentionalOutage ? "Intentional" : "Normal"} />
          <Metric label="Last error" value={state.lastError} />
          <Metric label="Endpoint" value={state.endpoint || "Create a feed"} wide />
        </section>

        <StreamMetrics streams={state.streams} />

        {tab === "Control" && <ControlPanel state={state} copied={copied} onCopy={copyEndpoint} />}
        {tab === "Validation" && <Output title="Validation" body={state.validationOutput} />}
        {tab === "Logs" && <Output title="Logs" body={(state.logs || []).join("\n") || "No logs yet"} />}
      </main>
    </div>
  );
}

function StreamNav({ streams, selectedStreamId }) {
  return (
    <section className="stream-nav" aria-label="Streams">
      <p className="eyebrow">Streams</p>
      <a className={!selectedStreamId ? "active create-link" : "create-link"} href="/">
        <span>{selectedStreamId ? "All feeds" : "Create feed"}</span>
        <small>{selectedStreamId ? "List and add" : "New feed workflow"}</small>
      </a>
      {streams.length === 0 && <small>No feeds</small>}
      {streams.map((stream) => (
        <a className={stream.id === selectedStreamId ? "active" : ""} href={stream.url} key={stream.id}>
          <span>{stream.name}</span>
          <small>{stream.protocol.toUpperCase()} · {stream.mode} · {stream.status}</small>
        </a>
      ))}
    </section>
  );
}

function PreviewPopup({ open, previewAvailable, previewUrl, onClose, onOpen }) {
  if (!open) {
    return (
      <button className="preview-tab" onClick={onOpen} type="button">
        Preview
      </button>
    );
  }

  return (
    <section className="preview-pop" role="dialog" aria-label="stream preview">
      <header>
        <div>
          <p className="eyebrow">Live Feed</p>
          <h3>Preview</h3>
        </div>
        <button className="secondary" onClick={onClose} type="button">Hide</button>
      </header>
      {previewAvailable ? (
        <img alt="Live stream preview" src={previewUrl} />
      ) : (
        <div className="preview-empty">No video track in this mode.</div>
      )}
    </section>
  );
}

function StatusPill({ status }) {
  return <span className={`status ${status}`}>{status}</span>;
}

function Metric({ label, value, wide }) {
  return (
    <article className={wide ? "metric wide" : "metric"}>
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function StreamMetrics({ streams }) {
  if (streams.length === 0) {
    return null;
  }

  return (
    <section className="stream-metrics" aria-label="Feed metrics">
      {streams.map((stream) => (
        <article className="stream-metric-card" key={stream.id}>
          <header>
            <div>
              <p className="eyebrow">{stream.protocol.toUpperCase()} · {stream.mode}</p>
              <h3>{stream.name}</h3>
            </div>
            <StatusPill status={stream.status} />
          </header>
          <div className="stream-stat-grid">
            <MetricCell label="Bit rate (est.)" value={stream.metrics.bitrateLabel} />
            <MetricCell label="Outbound total (est.)" value={stream.metrics.outboundLabel} />
            <MetricCell label="Uptime" value={`${stream.metrics.uptimeSeconds}s`} />
            <MetricCell label="Video frames" value={stream.metrics.videoFramesLabel} />
          </div>
        </article>
      ))}
    </section>
  );
}

function MetricCell({ label, value }) {
  return (
    <div className="stream-stat">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ControlPanel({ state, copied, onCopy }) {
  const selected = state.streams.find((stream) => stream.id === state.selectedStreamId);
  const createForm = (
    <form action="/streams/create" className="control-row create-feed" method="post">
      <label>
        New feed
        <input defaultValue={`Feed ${state.streams.length + 1}`} name="name" />
      </label>
      <label>
        Protocol
        <select defaultValue={state.protocol} name="protocol">
          {state.protocols.map((protocol) => (
            <option key={protocol.value} value={protocol.value}>{protocol.label}</option>
          ))}
        </select>
      </label>
      <label>
        Mode
        <select defaultValue={state.mode} name="mode">
          {state.modes.map((mode) => (
            <option key={mode.value} value={mode.value}>{mode.label}</option>
          ))}
        </select>
      </label>
      <button type="submit">Create feed</button>
    </form>
  );

  if (!selected) {
    return (
      <section className="panel">
        {createForm}
        <div className="empty-state">No feeds configured.</div>
      </section>
    );
  }

  return (
    <section className="panel">
      <form action="/streams/update" className="control-row" method="post">
        <input name="stream_id" type="hidden" value={state.selectedStreamId} />
        <label>
          Selected stream
          <input defaultValue={selected?.name || ""} name="name" />
        </label>
        <label>
          Protocol
          <select defaultValue={state.protocol} name="protocol">
            {state.protocols.map((protocol) => (
              <option key={protocol.value} value={protocol.value}>{protocol.label}</option>
            ))}
          </select>
        </label>
        <label>
          Mode
          <select defaultValue={state.mode} name="mode">
            {state.modes.map((mode) => (
              <option key={mode.value} value={mode.value}>{mode.label}</option>
            ))}
          </select>
        </label>
        <button className="secondary" type="submit">Update</button>
      </form>

      <form action="/start" className="control-row" method="post">
        <input name="stream_id" type="hidden" value={state.selectedStreamId} />
        <label>
          Protocol
          <select defaultValue={state.protocol} name="protocol">
            {state.protocols.map((protocol) => (
              <option key={protocol.value} value={protocol.value}>{protocol.label}</option>
            ))}
          </select>
        </label>
        <label>
          Mode
          <select defaultValue={state.mode} name="mode">
            {state.modes.map((mode) => (
              <option key={mode.value} value={mode.value}>{mode.label}</option>
            ))}
          </select>
        </label>
        <button type="submit">Start</button>
      </form>

      <form action="/start" className="toggles" method="post">
        <input name="controls" type="hidden" value="1" />
        <input name="protocol" type="hidden" value={state.protocol} />
        <input name="stream_id" type="hidden" value={state.selectedStreamId} />
        {[
          ["video", "Video"],
          ["audio", "Audio"],
          ["captions", "Captions"],
          ["black_video", "Black video"],
          ["frozen_video", "Frozen video"]
        ].map(([name, label]) => (
          <label key={name}>
            <input defaultChecked={state.controls[name]} name={name} type="checkbox" />
            <span>{label}</span>
          </label>
        ))}
        <button type="submit">Apply controls</button>
      </form>

      <div className="actions">
        <form action="/stop" method="post">
          <input name="stream_id" type="hidden" value={state.selectedStreamId} />
          <button className="secondary" type="submit">Stop</button>
        </form>
        <form action="/validate" method="post">
          <input name="stream_id" type="hidden" value={state.selectedStreamId} />
          <button className="secondary" type="submit">Validate</button>
        </form>
        <button className="secondary" onClick={onCopy} type="button">{copied ? "Copied" : "Copy URL"}</button>
        <form action="/streams/delete" method="post">
          <input name="stream_id" type="hidden" value={state.selectedStreamId} />
          <button className="secondary" type="submit">Delete</button>
        </form>
      </div>
    </section>
  );
}

function Output({ title, body }) {
  return (
    <section className="panel">
      <h3>{title}</h3>
      <pre>{body}</pre>
    </section>
  );
}

document.body.classList.add("react-ready");
createRoot(document.getElementById("app")).render(<App />);
