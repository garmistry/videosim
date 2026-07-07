import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const detailTabs = ["Logs", "Validation"];
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
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

const initialTheme = readTheme();
applyTheme(initialTheme);

function App() {
  const [state, setState] = useState(() => readState());
  const [tab, setTab] = useState("Logs");
  const [theme, setTheme] = useState(initialTheme);
  const [copiedEndpoint, setCopiedEndpoint] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [fullPreview, setFullPreview] = useState(null);
  const [previewTick, setPreviewTick] = useState(Date.now());
  const selectedStream = useMemo(
    () => (state.streams || []).find((item) => item.id === state.selectedStreamId),
    [state.selectedStreamId, state.streams]
  );

  async function copyEndpoint(value) {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      return;
    }
    setCopiedEndpoint(value);
    setTimeout(() => setCopiedEndpoint(""), 1200);
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
    if (!(state.streams || []).some((stream) => stream.previewAvailable)) {
      return undefined;
    }
    const timer = setInterval(() => setPreviewTick(Date.now()), 1800);
    return () => clearInterval(timer);
  }, [state.streams]);

  useEffect(() => {
    if (fullPreview) {
      const refreshed = (state.streams || []).find((stream) => stream.id === fullPreview.id);
      setFullPreview(refreshed || null);
    }
  }, [state.streams, fullPreview]);

  return (
    <div className="app-shell">
      <TopBar theme={theme} onCreate={() => setCreateOpen(true)} onToggleTheme={toggleTheme} />
      <main className="workspace" data-screen-label={selectedStream ? "Feed detail" : "Active feeds"}>
        {selectedStream ? (
          <FeedDetail
            copiedEndpoint={copiedEndpoint}
            onCopyEndpoint={copyEndpoint}
            onPreview={setFullPreview}
            previewTick={previewTick}
            setTab={setTab}
            state={state}
            stream={selectedStream}
            tab={tab}
          />
        ) : (
          <FeedList
            copiedEndpoint={copiedEndpoint}
            onCopyEndpoint={copyEndpoint}
            onCreate={() => setCreateOpen(true)}
            onPreview={setFullPreview}
            previewTick={previewTick}
            state={state}
          />
        )}
      </main>

      <CreateFeedDialog state={state} open={createOpen} onClose={() => setCreateOpen(false)} />
      <FullPreviewDialog
        copiedEndpoint={copiedEndpoint}
        onCopyEndpoint={copyEndpoint}
        previewTick={previewTick}
        stream={fullPreview}
        onClose={() => setFullPreview(null)}
      />
    </div>
  );
}

function TopBar({ theme, onCreate, onToggleTheme }) {
  return (
    <header className="topbar">
      <a className="wordmark" href="/">
        <span>Video Feed Simulator</span>
        <code>video<span>sim</span></code>
      </a>
      <div className="topbar-actions">
        <button className="button ghost" onClick={onToggleTheme} type="button">
          {theme === "light" ? "Dark theme" : "Light theme"}
        </button>
        <button className="button primary" onClick={onCreate} type="button">Create feed</button>
      </div>
    </header>
  );
}

function FeedList({ state, previewTick, copiedEndpoint, onCopyEndpoint, onCreate, onPreview }) {
  const streams = state.streams || [];
  const runningCount = streams.filter((stream) => stream.status === "running").length;
  return (
    <div className="screen-stack">
      <header className="screen-header">
        <div>
          <h1>Active feeds</h1>
          <p className="meta-line">{runningCount} running / {streams.length} total</p>
        </div>
        <button className="button primary" onClick={onCreate} type="button">Create feed</button>
      </header>
      <section className="card flush">
        {streams.length === 0 ? (
          <div className="empty-state">
            <span>No feeds configured.</span>
            <button className="button primary" onClick={onCreate} type="button">Create feed</button>
          </div>
        ) : (
          <FeedTable
            copiedEndpoint={copiedEndpoint}
            onCopyEndpoint={onCopyEndpoint}
            onPreview={onPreview}
            previewTick={previewTick}
            state={state}
          />
        )}
      </section>
    </div>
  );
}

function FeedTable({ state, previewTick, copiedEndpoint, onCopyEndpoint, onPreview }) {
  return (
    <div className="feed-table-wrap">
      <table className="feed-table">
        <thead>
          <tr>
            <th>Preview</th>
            <th>Feed</th>
            <th>Status</th>
            <th>Endpoint</th>
            <th className="numeric">Bit rate</th>
            <th className="numeric">Uptime</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {(state.streams || []).map((stream) => (
            <tr key={stream.id}>
              <td>
                <button className="preview-thumb" onClick={() => onPreview(stream)} type="button">
                  <img alt={`${stream.name} preview`} src={`${stream.previewUrl}?t=${previewTick}`} />
                </button>
              </td>
              <td>
                <div className="feed-name-cell">
                  <a href={stream.url}>{stream.name}</a>
                  <span>
                    <Tag>{stream.protocol.toUpperCase()}</Tag>
                    <Tag>{stream.mode}</Tag>
                  </span>
                </div>
              </td>
              <td><StatusBadge stream={stream} state={state} /></td>
              <td className="endpoint-cell">
                <EndpointField
                  copied={copiedEndpoint === stream.endpoint}
                  value={stream.endpoint}
                  onCopy={onCopyEndpoint}
                />
              </td>
              <td className="numeric mono">{bitrateText(stream)}</td>
              <td className="numeric mono">{uptimeText(stream)}</td>
              <td>
                <div className="row-actions">
                  <a className="button small ghost" href={stream.url}>Open</a>
                  <form action="/start" method="post">
                    <input name="stream_id" type="hidden" value={stream.id} />
                    <input name="protocol" type="hidden" value={stream.protocol} />
                    <input name="mode" type="hidden" value={stream.mode} />
                    <button className="button small secondary" type="submit">Start</button>
                  </form>
                  <form action="/stop" method="post">
                    <input name="stream_id" type="hidden" value={stream.id} />
                    <button className="button small secondary" type="submit">Stop</button>
                  </form>
                  <form action="/validate" method="post">
                    <input name="stream_id" type="hidden" value={stream.id} />
                    <button className="button small ghost" type="submit">Validate</button>
                  </form>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FeedDetail({ state, stream, tab, setTab, previewTick, copiedEndpoint, onCopyEndpoint, onPreview }) {
  const controls = state.controls || {};
  return (
    <div className="screen-stack">
      <a className="back-link" href="/">Active feeds</a>
      <header className="screen-header detail-header">
        <div>
          <div className="title-row">
            <h1>{stream.name}</h1>
            <StatusBadge stream={stream} state={state} />
            <Tag>{stream.protocol.toUpperCase()}</Tag>
            <Tag>{stream.mode}</Tag>
            <Tag accent>{stream.url}</Tag>
          </div>
          <p className="meta-line">{stream.intentionalOutage ? "Intentional outage" : "Normal feed"}</p>
        </div>
        <div className="header-actions">
          <form action="/validate" method="post">
            <input name="stream_id" type="hidden" value={stream.id} />
            <button className="button secondary" type="submit">Validate</button>
          </form>
          {stream.status === "running" ? (
            <form action="/stop" method="post">
              <input name="stream_id" type="hidden" value={stream.id} />
              <button className="button danger" type="submit">Stop feed</button>
            </form>
          ) : (
            <form action="/start" method="post">
              <input name="stream_id" type="hidden" value={stream.id} />
              <input name="protocol" type="hidden" value={stream.protocol} />
              <input name="mode" type="hidden" value={stream.mode} />
              <button className="button primary" type="submit">Start feed</button>
            </form>
          )}
        </div>
      </header>

      <section className="detail-grid">
        <article className="card">
          <header className="card-header"><h2>Preview</h2></header>
          <div className="card-body">
            <button className="preview-button" onClick={() => onPreview(stream)} type="button">
              <img alt={`${stream.name} preview`} src={`${stream.previewUrl}?t=${previewTick}`} />
            </button>
          </div>
        </article>

        <div className="detail-side">
          <article className="card">
            <header className="card-header"><h2>Endpoint</h2></header>
            <div className="card-body">
              <EndpointField
                copied={copiedEndpoint === stream.endpoint}
                value={stream.endpoint}
                onCopy={onCopyEndpoint}
              />
            </div>
          </article>

          <article className="card">
            <header className="card-header"><h2>Telemetry</h2></header>
            <div className="metric-grid">
              <MetricStat label="Bit rate" value={bitrateText(stream)} />
              <MetricStat label="Uptime" value={uptimeText(stream)} />
              <MetricStat label="Frames" value={stream.status === "running" ? stream.metrics.videoFramesLabel : "0"} />
              <MetricStat label="Outbound" value={stream.metrics.outboundLabel} />
            </div>
          </article>

          <article className="card">
            <header className="card-header"><h2>Configuration</h2></header>
            <div className="card-body">
              <form action="/streams/update" className="form-grid" method="post">
                <input name="stream_id" type="hidden" value={stream.id} />
                <label>
                  Feed name
                  <input defaultValue={stream.name} name="name" />
                </label>
                <label>
                  Protocol
                  <select defaultValue={stream.protocol} name="protocol">
                    {(state.protocols || []).map((protocol) => (
                      <option key={protocol.value} value={protocol.value}>{protocol.label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Mode
                  <select defaultValue={stream.mode} name="mode">
                    {(state.modes || []).map((mode) => (
                      <option key={mode.value} value={mode.value}>{mode.label}</option>
                    ))}
                  </select>
                </label>
                <button className="button secondary" type="submit">Update</button>
              </form>
            </div>
          </article>
        </div>
      </section>

      <section className="card">
        <header className="card-header"><h2>Fault controls</h2></header>
        <div className="card-body">
          <form action="/start" className="switch-row" method="post">
            <input name="controls" type="hidden" value="1" />
            <input name="protocol" type="hidden" value={stream.protocol} />
            <input name="stream_id" type="hidden" value={stream.id} />
            {[
              ["video", "Video"],
              ["audio", "Audio"],
              ["captions", "Captions"],
              ["black_video", "Black video"],
              ["frozen_video", "Frozen video"]
            ].map(([name, label]) => (
              <label className="switch" key={name}>
                <input defaultChecked={controls[name]} name={name} type="checkbox" />
                <span>{label}</span>
              </label>
            ))}
            <button className="button primary" type="submit">Apply controls</button>
          </form>
        </div>
      </section>

      <section className="card flush">
        <div className="tabs" role="tablist">
          {detailTabs.map((item) => (
            <button
              aria-selected={item === tab}
              className={item === tab ? "active" : ""}
              key={item}
              onClick={() => setTab(item)}
              role="tab"
              type="button"
            >
              {item}
              {item === "Logs" ? <span>{(stream.logs || []).length}</span> : null}
            </button>
          ))}
        </div>
        <div className="tab-body">
          {tab === "Logs" ? (
            <div className="log-section">
              <LogViewer lines={stream.logs || []} />
              <a className="button small ghost diagnostics-link" href="/diagnostics.txt">Download diagnostics</a>
            </div>
          ) : (
            <LogViewer lines={(stream.validationOutput || state.validationOutput || "not run").split("\n")} />
          )}
        </div>
      </section>

      <div className="delete-row">
        <form action="/streams/delete" method="post">
          <input name="stream_id" type="hidden" value={stream.id} />
          <button className="button danger" type="submit">Delete feed</button>
        </form>
      </div>
    </div>
  );
}

function CreateFeedDialog({ state, open, onClose }) {
  if (!open) {
    return null;
  }

  return (
    <dialog className="modal" onCancel={onClose} open>
      <header>
        <h2>Create feed</h2>
        <button className="icon-button" aria-label="Close" onClick={onClose} type="button">x</button>
      </header>
      <form action="/streams/create" className="modal-form" method="post">
        <label>
          Feed name
          <input defaultValue={`Feed ${(state.streams || []).length + 1}`} name="name" />
        </label>
        <label>
          Protocol
          <select defaultValue={state.protocol} name="protocol">
            {(state.protocols || []).map((protocol) => (
              <option key={protocol.value} value={protocol.value}>{protocol.label}</option>
            ))}
          </select>
        </label>
        <label>
          Mode
          <select defaultValue={state.mode} name="mode">
            {(state.modes || []).map((mode) => (
              <option key={mode.value} value={mode.value}>{mode.label}</option>
            ))}
          </select>
        </label>
        <footer>
          <button className="button secondary" onClick={onClose} type="button">Cancel</button>
          <button className="button primary" type="submit">Create feed</button>
        </footer>
      </form>
    </dialog>
  );
}

function FullPreviewDialog({ stream, previewTick, copiedEndpoint, onCopyEndpoint, onClose }) {
  if (!stream) {
    return null;
  }

  return (
    <dialog className="modal preview-modal" onCancel={onClose} open>
      <header>
        <h2>Preview - {stream.name}</h2>
        <button className="icon-button" aria-label="Close" onClick={onClose} type="button">x</button>
      </header>
      <div className="preview-frame">
        <img alt={`${stream.name} full preview`} src={`${stream.previewUrl}?t=${previewTick}`} />
      </div>
      <div className="preview-details">
        <StatusBadge stream={stream} />
        <EndpointField copied={copiedEndpoint === stream.endpoint} value={stream.endpoint} onCopy={onCopyEndpoint} />
      </div>
    </dialog>
  );
}

function StatusBadge({ stream, state }) {
  const status = statusMeta(stream, state);
  return (
    <span className={`status ${status.key}`}>
      <span aria-hidden="true" />
      {status.label}
    </span>
  );
}

function statusMeta(stream, state) {
  if (!stream) {
    return { key: "stopped", label: "Stopped" };
  }
  if (stream.lastError && stream.lastError !== "none") {
    return { key: "error", label: "Error" };
  }
  if (stream.status === "running" && stream.intentionalOutage) {
    return { key: "fault", label: `Fault - ${modeLabel(stream.mode, state)}` };
  }
  if (stream.status === "running") {
    return { key: "running", label: "Running" };
  }
  return { key: "stopped", label: "Stopped" };
}

function EndpointField({ value, copied, onCopy }) {
  return (
    <div className="endpoint-field">
      <code title={value}>{value}</code>
      <button className={copied ? "copied" : ""} onClick={() => onCopy(value)} type="button">
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

function MetricStat({ label, value }) {
  return (
    <div className="metric-stat">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function LogViewer({ lines }) {
  return (
    <pre className="log-viewer">
      {(lines || []).filter(Boolean).join("\n") || "not run"}
    </pre>
  );
}

function Tag({ accent = false, children }) {
  return <span className={accent ? "tag accent" : "tag"}>{children}</span>;
}

function modeLabel(mode, state) {
  const match = (state?.modes || []).find((item) => item.value === mode);
  return match?.label || String(mode || "").replaceAll("_", " ");
}

function bitrateText(stream) {
  if (stream.status !== "running") {
    return "0 b/s";
  }
  return String(stream.metrics.bitrateLabel || "0 b/s")
    .replace("Mbps", "Mb/s")
    .replace("kbps", "kb/s")
    .replace("bps", "b/s");
}

function uptimeText(stream) {
  return formatSeconds(stream.status === "running" ? stream.metrics.uptimeSeconds : 0);
}

function formatSeconds(value) {
  const total = Math.max(0, Math.floor(Number(value) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":");
}

document.body.classList.add("react-ready");
createRoot(document.getElementById("app")).render(<App />);
