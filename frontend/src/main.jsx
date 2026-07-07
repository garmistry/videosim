import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const detailTabs = ["Logs", "Validation"];
const METRICS_WINDOW_MS = 5 * 60 * 1000;
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
  const [metricHistory, setMetricHistory] = useState({});
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

  useEffect(() => {
    const now = Date.now();
    setMetricHistory((history) => {
      const next = {};
      for (const stream of state.streams || []) {
        const sample = {
          time: now,
          bitrateBps: stream.metrics?.bitrateBps || 0,
          outboundBytes: stream.metrics?.outboundBytes || 0
        };
        next[stream.id] = [...(history[stream.id] || []), sample].filter(
          (item) => item.time >= now - METRICS_WINDOW_MS
        );
      }
      return next;
    });
  }, [state.streams]);

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
            metricSamples={metricHistory[selectedStream.id] || []}
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
      <MonitorSummary monitor={state.monitor || {}} />
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
                    <Tag>{stream.sourceLabel || stream.source || "Generated"}</Tag>
                    <Tag>{stream.protocol.toUpperCase()}</Tag>
                    {stream.source !== "external" ? <Tag>{stream.mode}</Tag> : null}
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
                  {stream.source !== "external" ? (
                    <>
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
                    </>
                  ) : null}
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

function FeedDetail({ state, stream, metricSamples, tab, setTab, previewTick, copiedEndpoint, onCopyEndpoint, onPreview }) {
  const controls = state.controls || {};
  const [configSource, setConfigSource] = useState(stream.source || "generated");
  useEffect(() => {
    setConfigSource(stream.source || "generated");
  }, [stream.id, stream.source]);
  return (
    <div className="screen-stack">
      <a className="back-link" href="/">Active feeds</a>
      <header className="screen-header detail-header">
        <div>
          <div className="title-row">
            <h1>{stream.name}</h1>
            <StatusBadge stream={stream} state={state} />
            <Tag>{stream.sourceLabel || stream.source || "Generated"}</Tag>
            <Tag>{stream.protocol.toUpperCase()}</Tag>
            {stream.source !== "external" ? <Tag>{stream.mode}</Tag> : null}
            <Tag accent>{stream.url}</Tag>
          </div>
          <p className="meta-line">{stream.source === "external" ? "External feed" : stream.intentionalOutage ? "Intentional outage" : "Normal feed"}</p>
        </div>
        <div className="header-actions">
          <form action="/validate" method="post">
            <input name="stream_id" type="hidden" value={stream.id} />
            <button className="button secondary" type="submit">Validate</button>
          </form>
          {stream.source !== "external" ? (
            stream.status === "running" ? (
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
            )
          ) : null}
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
            <header className="card-header">
              <h2>Traffic - last 5 min</h2>
              <span className="card-meta">{metricSamples.length} samples</span>
            </header>
            <div className="chart-grid">
              <MetricChart
                label="Bit rate"
                samples={metricSamples}
                valueKey="bitrateBps"
                formatValue={formatBps}
              />
              <MetricChart
                label="Outbound data"
                samples={metricSamples}
                valueKey="outboundBytes"
                formatValue={formatBytesValue}
              />
            </div>
          </article>

          <MonitorPanel monitor={state.monitor || {}} stream={stream} />
          <AlertProfileCard state={state} stream={stream} />

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
                  Source
                  <select name="source" onChange={(event) => setConfigSource(event.target.value)} value={configSource}>
                    {(state.sources || []).map((source) => (
                      <option key={source.value} value={source.value}>{source.label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Protocol
                  <select defaultValue={stream.protocol} name="protocol">
                    {(state.protocols || []).map((protocol) => (
                      <option key={protocol.value} value={protocol.value}>{protocol.label}</option>
                    ))}
                  </select>
                </label>
                {configSource !== "external" ? (
                  <>
                    <label>
                      Mode
                      <select defaultValue={stream.mode} name="mode">
                        {(state.modes || []).map((mode) => (
                          <option key={mode.value} value={mode.value}>{mode.label}</option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Frame rate
                      <select defaultValue={stream.framerate} name="framerate">
                        {(state.framerates || []).map((rate) => (
                          <option key={rate.value} value={rate.value}>{rate.label}</option>
                        ))}
                      </select>
                    </label>
                  </>
                ) : (
                  <label>
                    External URL
                    <input defaultValue={stream.externalUrl || ""} name="external_url" placeholder="srt://host:port or https://host/manifest.mpd" />
                  </label>
                )}
                <button className="button secondary" type="submit">Update</button>
              </form>
            </div>
          </article>
        </div>
      </section>

      {stream.source !== "external" ? (
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
      ) : null}

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
  const [source, setSource] = useState("generated");
  useEffect(() => {
    if (open) {
      setSource("generated");
    }
  }, [open]);

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
          Source
          <select name="source" onChange={(event) => setSource(event.target.value)} value={source}>
            {(state.sources || []).map((source) => (
              <option key={source.value} value={source.value}>{source.label}</option>
            ))}
          </select>
        </label>
        <label>
          Protocol
          <select defaultValue={state.protocol} name="protocol">
            {(state.protocols || []).map((protocol) => (
              <option key={protocol.value} value={protocol.value}>{protocol.label}</option>
            ))}
          </select>
        </label>
        {source !== "external" ? (
          <>
            <label>
              Mode
              <select defaultValue={state.mode} name="mode">
                {(state.modes || []).map((mode) => (
                  <option key={mode.value} value={mode.value}>{mode.label}</option>
                ))}
              </select>
            </label>
            <label>
              Frame rate
              <select defaultValue={state.framerate} name="framerate">
                {(state.framerates || []).map((rate) => (
                  <option key={rate.value} value={rate.value}>{rate.label}</option>
                ))}
              </select>
            </label>
          </>
        ) : (
          <label>
            External URL
            <input name="external_url" placeholder="srt://host:port or https://host/manifest.mpd" />
          </label>
        )}
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
  if (stream.source === "external" && stream.status === "running") {
    return { key: "running", label: "Monitoring" };
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

function MetricChart({ label, samples, valueKey, formatValue }) {
  const latest = samples.at(-1);
  const latestTime = latest?.time || Date.now();
  const minTime = latestTime - METRICS_WINDOW_MS;
  const values = samples.map((sample) => Number(sample[valueKey]) || 0);
  const maxValue = Math.max(...values, 1);
  const width = 320;
  const height = 120;
  const pad = 18;
  const plotWidth = width - pad * 2;
  const plotHeight = height - pad * 2;
  const points = samples.map((sample) => {
    const x = pad + ((sample.time - minTime) / METRICS_WINDOW_MS) * plotWidth;
    const y = height - pad - ((Number(sample[valueKey]) || 0) / maxValue) * plotHeight;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const latestValue = formatValue(latest?.[valueKey] || 0);

  return (
    <div className="metric-chart">
      <div className="chart-head">
        <span>{label}</span>
        <strong>{latestValue}</strong>
      </div>
      <svg aria-label={`${label} graph`} role="img" viewBox={`0 0 ${width} ${height}`}>
        <line className="chart-grid-line" x1={pad} x2={width - pad} y1={pad} y2={pad} />
        <line className="chart-grid-line" x1={pad} x2={width - pad} y1={height / 2} y2={height / 2} />
        <line className="chart-grid-line" x1={pad} x2={width - pad} y1={height - pad} y2={height - pad} />
        {points.length > 1 ? <polyline className="chart-line" points={points.join(" ")} /> : null}
        {points.length === 1 ? (
          <circle className="chart-dot" cx={points[0].split(",")[0]} cy={points[0].split(",")[1]} r="2.5" />
        ) : null}
      </svg>
      <div className="chart-axis">
        <span>-5 min</span>
        <span>now</span>
      </div>
    </div>
  );
}

function MonitorSummary({ monitor }) {
  const active = (monitor.alarms || []).filter((alarm) => alarm.active);
  if (active.length === 0) {
    return null;
  }
  return (
    <section className="card alarm-card">
      <header className="card-header">
        <h2>Monitor alarms</h2>
        <span className="card-meta">{active.length} active</span>
      </header>
      <div className="alarm-list">
        {active.slice(0, 4).map((alarm) => (
          <AlarmRow alarm={alarm} key={alarm.id} />
        ))}
      </div>
    </section>
  );
}

function MonitorPanel({ stream, monitor }) {
  const alarms = (monitor.alarms || []).filter((alarm) => alarm.streamId === stream.id);
  const events = (monitor.events || []).filter((event) => event.streamId === stream.id).slice(-8).reverse();
  return (
    <article className="card alarm-card">
      <header className="card-header">
        <h2>Alarms</h2>
        <span className="card-meta">{alarms.filter((alarm) => alarm.active).length} active</span>
      </header>
      <div className="alarm-list">
        {alarms.length === 0 ? <p className="empty-copy">No monitor alarms</p> : alarms.map((alarm) => <AlarmRow alarm={alarm} key={alarm.id} />)}
      </div>
      <header className="card-header subhead">
        <h2>Event audit</h2>
        <span className="card-meta">{events.length} recent</span>
      </header>
      <div className="event-list">
        {events.length === 0 ? <p className="empty-copy">No monitor events</p> : events.map((event) => <EventRow event={event} key={event.id} />)}
      </div>
    </article>
  );
}

function AlertProfileCard({ state, stream }) {
  const options = state.alertOptions || state.monitor?.monitors || [];
  const profile = stream.alertProfile || { allEnabled: true, enabledMonitorIds: null, delaySeconds: 0 };
  const enabled = profile.enabledMonitorIds;
  const isEnabled = (id) => profile.allEnabled || !Array.isArray(enabled) || enabled.includes(id);
  const activeIds = new Set((state.monitor?.alarms || []).filter((alarm) => alarm.streamId === stream.id && alarm.active).map((alarm) => alarm.monitorId));
  const pendingIds = new Set((state.monitor?.pending || []).filter((item) => item.streamId === stream.id).map((item) => item.monitorId));
  const enabledCount = options.filter((option) => isEnabled(option.id)).length;
  const monitorStatus = state.monitor?.connected ? "monitor connected" : "monitor offline";
  const alertState = (id) => activeIds.has(id) ? "Active" : pendingIds.has(id) ? "Pending" : isEnabled(id) ? "On" : "Off";
  return (
    <article className="card">
      <header className="card-header">
        <h2>Alert profile</h2>
        <span className="card-meta">{enabledCount}/{options.length} on - {monitorStatus}</span>
      </header>
      <div className="card-body">
        <form action="/streams/alerts" className="alert-profile-form" method="post">
          <input name="stream_id" type="hidden" value={stream.id} />
          <label className="delay-field">
            Alarm delay seconds
            <input defaultValue={profile.delaySeconds || 0} min="0" name="alert_delay_seconds" step="1" type="number" />
          </label>
          {options.length === 0 ? (
            <p className="empty-copy">Monitor not running</p>
          ) : (
            <>
              <div className="alert-choice-grid">
                {options.map((option) => (
                  <label className="check-row" key={option.id}>
                    <input defaultChecked={isEnabled(option.id)} name="alert_monitor" type="checkbox" value={option.id} />
                    <span>
                      <strong>{option.name}</strong>
                      <em className={`alert-state ${alertState(option.id).toLowerCase()}`}>{alertState(option.id)}</em>
                      <em>{option.severity}</em>
                    </span>
                  </label>
                ))}
              </div>
              <div className="row-actions">
                <button className="button secondary" name="alert_action" type="submit" value="save">Save selected</button>
                <button className="button ghost" name="alert_action" type="submit" value="enable_all">Enable all</button>
                <button className="button ghost" name="alert_action" type="submit" value="disable_all">Disable all</button>
              </div>
            </>
          )}
        </form>
      </div>
    </article>
  );
}

function AlarmRow({ alarm }) {
  return (
    <div className={`alarm-row ${alarm.active ? "active" : "steady"}`}>
      <span>{alarm.severity}</span>
      <strong>{alarm.monitorName}</strong>
      <p>{alarm.message}</p>
    </div>
  );
}

function EventRow({ event }) {
  return (
    <div className="event-row">
      <span>{event.time}</span>
      <strong>{event.type}</strong>
      <p>{event.monitorName}: {event.message}</p>
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
  return formatBps(stream.metrics.bitrateBps);
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

function formatBps(value) {
  const rate = Math.max(0, Number(value) || 0);
  if (rate >= 1_000_000) {
    return `${(rate / 1_000_000).toFixed(1)} Mb/s`;
  }
  if (rate >= 1_000) {
    return `${Math.round(rate / 1_000)} kb/s`;
  }
  return `${Math.round(rate)} b/s`;
}

function formatBytesValue(value) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = Math.max(0, Number(value) || 0);
  for (const unit of units) {
    if (amount < 1024 || unit === units.at(-1)) {
      return unit === "B" ? `${Math.round(amount)} B` : `${amount.toFixed(1)} ${unit}`;
    }
    amount /= 1024;
  }
  return "0 B";
}

document.body.classList.add("react-ready");
createRoot(document.getElementById("app")).render(<App />);
