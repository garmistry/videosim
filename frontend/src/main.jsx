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
  const [createOpen, setCreateOpen] = useState(false);
  const [fullPreview, setFullPreview] = useState(null);
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
    if (!state.streams.some((stream) => stream.previewAvailable)) {
      return undefined;
    }
    const timer = setInterval(() => setPreviewTick(Date.now()), 1800);
    return () => clearInterval(timer);
  }, [state.streams]);

  useEffect(() => {
    if (fullPreview) {
      const refreshed = state.streams.find((stream) => stream.id === fullPreview.id);
      setFullPreview(refreshed || null);
    }
  }, [state.streams, fullPreview]);

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
            <p className="eyebrow">Feed console</p>
            <h2>{selectedStream?.name || "Active feeds"}</h2>
            <span>{selectedStream ? `${mode?.label || state.mode} · ${state.protocol.toUpperCase()}` : `${state.streams.length} configured`}</span>
          </div>
          <button onClick={() => setCreateOpen(true)} type="button">Create feed</button>
        </section>

        <FeedTable state={state} previewTick={previewTick} onCreate={() => setCreateOpen(true)} onPreview={setFullPreview} />
        <CreateFeedDialog state={state} open={createOpen} onClose={() => setCreateOpen(false)} />
        <FullPreviewDialog stream={fullPreview} previewTick={previewTick} onClose={() => setFullPreview(null)} />

        <section className="metrics">
          <Metric label="Outage" value={state.intentionalOutage ? "Intentional" : "Normal"} />
          <Metric label="Last error" value={state.lastError} />
          <Metric label="Endpoint" value={state.endpoint || "Create a feed"} wide />
        </section>

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
        <span>All feeds</span>
        <small>{selectedStreamId ? "Table view" : "Current view"}</small>
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

function FeedTable({ state, previewTick, onCreate, onPreview }) {
  return (
    <section className="feed-table-panel">
      <header className="table-toolbar">
        <div>
          <p className="eyebrow">Primary view</p>
          <h3>Active feeds</h3>
        </div>
        <button onClick={onCreate} type="button">Create feed</button>
      </header>
      {state.streams.length === 0 ? (
        <div className="empty-state">No feeds configured.</div>
      ) : (
        <div className="feed-table-wrap">
          <table className="feed-table">
            <thead>
              <tr>
                <th>Preview</th>
                <th>Feed</th>
                <th>Status</th>
                <th>Endpoint</th>
                <th>Metrics</th>
                <th>Last error</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {state.streams.map((stream) => (
                <tr key={stream.id}>
                  <td>
                    <button className="preview-thumb" onClick={() => onPreview(stream)} type="button">
                      <img alt={`${stream.name} preview`} src={`${stream.previewUrl}?t=${previewTick}`} />
                    </button>
                  </td>
                  <td>
                    <strong>{stream.name}</strong>
                    <small>{stream.protocol.toUpperCase()} · {stream.mode}</small>
                  </td>
                  <td><StatusPill status={stream.status} /></td>
                  <td><code>{stream.endpoint}</code></td>
                  <td>
                    <div className="metric-stack">
                      <span>{stream.metrics.bitrateLabel}</span>
                      <span>{stream.metrics.outboundLabel}</span>
                      <span>{stream.metrics.uptimeSeconds}s · {stream.metrics.videoFramesLabel} frames</span>
                    </div>
                  </td>
                  <td>{stream.lastError}</td>
                  <td>
                    <div className="row-actions">
                      <a className="small-action" href={stream.url}>Open</a>
                      <form action="/start" method="post">
                        <input name="stream_id" type="hidden" value={stream.id} />
                        <input name="protocol" type="hidden" value={stream.protocol} />
                        <input name="mode" type="hidden" value={stream.mode} />
                        <button className="small-action" type="submit">Start</button>
                      </form>
                      <form action="/stop" method="post">
                        <input name="stream_id" type="hidden" value={stream.id} />
                        <button className="small-action secondary" type="submit">Stop</button>
                      </form>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function CreateFeedDialog({ state, open, onClose }) {
  if (!open) {
    return null;
  }

  return (
    <dialog className="modal" onCancel={onClose} open>
      <header>
        <div>
          <p className="eyebrow">New feed</p>
          <h3>Create feed</h3>
        </div>
        <button className="secondary" onClick={onClose} type="button">Close</button>
      </header>
      <form action="/streams/create" className="control-row create-feed" method="post">
        <label>
          Name
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
    </dialog>
  );
}

function FullPreviewDialog({ stream, previewTick, onClose }) {
  if (!stream) {
    return null;
  }

  return (
    <dialog className="modal preview-modal" onCancel={onClose} open>
      <header>
        <div>
          <p className="eyebrow">{stream.protocol.toUpperCase()} · {stream.mode}</p>
          <h3>{stream.name}</h3>
        </div>
        <button className="secondary" onClick={onClose} type="button">Close</button>
      </header>
      <img alt={`${stream.name} full preview`} className="preview-full" src={`${stream.previewUrl}?t=${previewTick}`} />
      <div className="preview-details">
        <StatusPill status={stream.status} />
        <code>{stream.endpoint}</code>
        <span>{stream.metrics.bitrateLabel}</span>
        <span>{stream.metrics.outboundLabel}</span>
      </div>
      <a className="small-action" href={stream.url}>Open feed controls</a>
    </dialog>
  );
}

function ControlPanel({ state, copied, onCopy }) {
  const selected = state.streams.find((stream) => stream.id === state.selectedStreamId);
  if (!selected) {
    return (
      <section className="panel">
        <div className="empty-state">Select a feed from the table to edit controls.</div>
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
