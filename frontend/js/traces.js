// traces.js: the Traces page.
//
// Without an address parameter it lists the newest engagements (newest first); each row
// links to /traces?trace_id=<id>. With ?trace_id=<id> it shows that one engagement as
// its eight steps, in order, each with its start (counted from the first step), its
// duration and its status, and the total time.
//
// How unfinished or empty steps are worded:
//   * duration missing (the step never finished)      -> "incomplete"
//   * status TIMEOUT (no reply came in time)           -> "timed out"
//   * effector_action with status NOT_RUN              -> "no action"
//   * any other NOT_RUN step                           -> "not run"
//   * status REJECTED (the device refused the message) -> "rejected by the device"
//   * status DENIED (the device arbitrated and said no) -> "denied by the device"
// A step whose timing is made up (there is no camera yet) carries the label "simulated".

import { connectLive } from "./live.js";

// The eight steps of every trace, in order (the server always sends all eight).
const STEP_NAMES = [
  "capture",
  "encode",
  "network",
  "inference",
  "decision",
  "command",
  "esp32_ack",
  "effector_action",
];

const errorNote = document.getElementById("trace-error");

function showError(message) {
  errorNote.textContent = message || "";
  errorNote.hidden = !message;
}

function formatMs(us) {
  return `${(us / 1000).toFixed(1)} ms`;
}

function cell(text) {
  const element = document.createElement("td");
  element.textContent = text;
  return element;
}

function statusText(span) {
  if (span.status === "TIMEOUT") return "timed out";
  if (span.status === "NOT_RUN") {
    return span.name === "effector_action" ? "no action" : "not run";
  }
  if (span.duration_us === null) return "incomplete";
  if (span.status === "REJECTED") return "rejected by the device";
  if (span.status === "DENIED") return "denied by the device";
  return span.status;
}

function durationText(span) {
  if (span.duration_us === null || span.duration_us === undefined) {
    return span.status === "NOT_RUN" ? "-" : "incomplete";
  }
  return formatMs(span.duration_us);
}

// Extra words for a step: the denial reason, the reject code, the made-up flag.
function detailsText(span) {
  const parts = [];
  if (span.simulated) parts.push("simulated");
  const attrs = span.attrs || {};
  if (attrs.reason) parts.push(`reason: ${attrs.reason}`);
  if (attrs.reject_code) parts.push(`rejected: ${attrs.reject_code}`);
  if (attrs.note) parts.push(String(attrs.note));
  return parts.join(" · ");
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    const problem = await response.json().catch(() => null);
    const failure = problem && problem.error ? problem.error : null;
    const error = new Error(failure ? failure.message : `status ${response.status}`);
    error.code = failure ? failure.code : null;
    throw error;
  }
  return response.json();
}

function traceLink(traceId) {
  const link = document.createElement("a");
  link.href = `/traces?trace_id=${encodeURIComponent(traceId)}`;
  link.textContent = traceId;
  return link;
}

async function showList() {
  const body = await getJson("/api/traces");
  const rows = document.getElementById("trace-list-body");
  rows.replaceChildren();
  for (const item of body.items) {
    const row = document.createElement("tr");
    const idCell = document.createElement("td");
    idCell.append(traceLink(item.trace_id));
    row.append(
      idCell,
      cell(new Date(item.started_us / 1000).toLocaleString()),
      cell(item.simulated ? `${item.outcome} (simulated timings)` : item.outcome),
      cell(item.total_us === null ? "incomplete" : formatMs(item.total_us)),
    );
    rows.append(row);
  }
  document.getElementById("trace-list-empty").hidden = body.items.length > 0;
}

async function showDetail(traceId) {
  const trace = await getJson(`/api/traces/${encodeURIComponent(traceId)}`);
  const byName = new Map(trace.spans.map((span) => [span.name, span]));
  const first = trace.spans.length > 0 ? trace.spans[0].started_us : trace.started_us;
  const rows = document.getElementById("trace-steps");
  rows.replaceChildren();
  STEP_NAMES.forEach((name, index) => {
    const span = byName.get(name);
    const row = document.createElement("tr");
    if (!span) {
      row.append(cell(String(index + 1)), cell(name), cell("-"), cell("-"), cell("missing"), cell(""));
    } else {
      row.append(
        cell(String(span.position)),
        cell(name),
        cell(`+${formatMs(span.started_us - first)}`),
        cell(durationText(span)),
        cell(statusText(span)),
        cell(detailsText(span)),
      );
    }
    rows.append(row);
  });
  const summary = document.getElementById("trace-summary");
  summary.textContent =
    `Trace ${trace.trace_id}: ${trace.outcome}` +
    (trace.deny_reason ? ` (${trace.deny_reason})` : "") +
    (trace.simulated ? " · camera, encode, network and inference timings are simulated" : "");
  document.getElementById("trace-total").textContent =
    trace.total_us === null ? "Total: incomplete" : `Total: ${formatMs(trace.total_us)}`;
  document.getElementById("trace-logs-link").href =
    `/logs?trace_id=${encodeURIComponent(trace.trace_id)}`;
  document.getElementById("trace-detail").hidden = false;
}

connectLive(); // only drives the connection indicator on this page

const wanted = new URLSearchParams(location.search).get("trace_id");
(async () => {
  try {
    await showList();
    if (wanted) await showDetail(wanted);
  } catch (error) {
    showError(
      error.code === "TRACE_NOT_FOUND"
        ? `There is no trace with id ${wanted}.`
        : `Could not load traces: ${error.message}`,
    );
  }
})();
