import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const tabs = ["Control", "Validation", "Logs"];

function readState() {
  const node = document.getElementById("initial-state");
  return JSON.parse(node?.textContent || "{}");
}

function App() {
  const state = readState();
  const [tab, setTab] = useState("Control");
  const [copied, setCopied] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(state.status === "running");
  const [previewTick, setPreviewTick] = useState(Date.now());
  const mode = useMemo(
    () => state.modes.find((item) => item.value === state.mode) || state.modes[0],
    [state.mode, state.modes]
  );

  async function copyEndpoint() {
    await navigator.clipboard.writeText(state.endpoint);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  }

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
        <nav aria-label="Main">
          {tabs.map((item) => (
            <button className={item === tab ? "active" : ""} key={item} onClick={() => setTab(item)} type="button">
              {item}
            </button>
          ))}
        </nav>
        <a className="download" href="/diagnostics.txt">Download diagnostics</a>
      </aside>

      <main className="workspace">
        <section className="hero">
          <div>
            <p className="eyebrow">Current profile</p>
            <h2>{mode?.label || state.mode}</h2>
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
          <Metric label="Endpoint" value={state.endpoint} wide />
        </section>

        {tab === "Control" && <ControlPanel state={state} copied={copied} onCopy={copyEndpoint} />}
        {tab === "Validation" && <Output title="Validation" body={state.validationOutput} />}
        {tab === "Logs" && <Output title="Logs" body={(state.logs || []).join("\n") || "No logs yet"} />}
      </main>
    </div>
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

function ControlPanel({ state, copied, onCopy }) {
  return (
    <section className="panel">
      <form action="/start" className="control-row" method="post">
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
        <form action="/stop" method="post"><button className="secondary" type="submit">Stop</button></form>
        <form action="/validate" method="post"><button className="secondary" type="submit">Validate</button></form>
        <button className="secondary" onClick={onCopy} type="button">{copied ? "Copied" : "Copy URL"}</button>
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
