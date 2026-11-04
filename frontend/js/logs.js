// logs.js: the Logs page. It asks the server for stored log entries and shows them.
//
// Filters (all optional, combined with AND):
//   * component  - chosen from the list the server gives (/api/logs/components)
//   * severity   - DEBUG, INFO, WARNING, ERROR or CRITICAL
//   * trace id   - the 16-character id of one engagement
// Leaving a filter on "Any" (or empty) simply does not send it.
//
// The server returns the newest PAGE_SIZE matching entries, oldest first. "Load older
// entries" asks for the page before the oldest one shown (before_id).
//
// An empty result is not an error: it says "Nothing matched these filters." A failed
// request (for example the log database cannot be read) shows its own error message.

import { connectLive } from "./live.js";

const PAGE_SIZE = 200;

// Builds the address of the logs request. The names are exactly the API's query names.
export function buildLogsQuery(filters, beforeId) {
  const params = new URLSearchParams();
  if (filters.component) params.set("component", filters.component);
  if (filters.severity) params.set("severity", filters.severity);
  if (filters.traceId) params.set("trace_id", filters.traceId);
  params.set("limit", String(PAGE_SIZE));
  if (beforeId !== null && beforeId !== undefined) params.set("before_id", String(beforeId));
  return `/api/logs?${params.toString()}`;
}

const componentSelect = document.getElementById("filter-component");
const severitySelect = document.getElementById("filter-severity");
const traceInput = document.getElementById("filter-trace-id");
const results = document.getElementById("log-results");
const emptyNote = document.getElementById("logs-empty");
const errorNote = document.getElementById("logs-error");
const olderButton = document.getElementById("logs-older");

let currentFilters = { component: "", severity: "", traceId: "" };
let oldestShownId = null;

function readFilters() {
  return {
    component: componentSelect.value,
    severity: severitySelect.value,
    traceId: traceInput.value.trim(),
  };
}

function showError(message) {
  errorNote.textContent = message || "";
  errorNote.hidden = !message;
}

function entryRow(entry) {
  const row = document.createElement("li");
  row.className = "log-row";
  row.dataset.severity = String(entry.severity);
  const when = new Date(entry.ts_us / 1000).toLocaleString();
  row.append(document.createTextNode(`${when}  ${entry.severity}  ${entry.component}: ${entry.message}  `));
  if (entry.trace_id) {
    const link = document.createElement("a");
    link.href = `/traces?trace_id=${encodeURIComponent(entry.trace_id)}`;
    link.textContent = `trace ${entry.trace_id}`;
    row.append(link);
  }
  return row;
}

// Fetches one page. With older=true the rows go ABOVE the ones already shown.
async function load(older) {
  showError("");
  if (!older) {
    results.replaceChildren(); // never leave old rows beside a failed or empty new result
    emptyNote.hidden = true;
    olderButton.hidden = true;
  }
  let body;
  try {
    const response = await fetch(buildLogsQuery(currentFilters, older ? oldestShownId : null));
    if (!response.ok) {
      const problem = await response.json().catch(() => null);
      const detail = problem && problem.error ? problem.error.message : `status ${response.status}`;
      throw new Error(detail);
    }
    body = await response.json();
  } catch (error) {
    showError(`Could not load log entries: ${error.message}`);
    return;
  }
  const items = body.items;
  results.prepend(...items.map(entryRow));
  if (items.length > 0) oldestShownId = items[0].id;
  emptyNote.hidden = !(items.length === 0 && results.children.length === 0);
  olderButton.hidden = !body.has_more;
}

async function loadComponents() {
  try {
    const response = await fetch("/api/logs/components");
    if (!response.ok) return;
    const body = await response.json();
    for (const name of body.items) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      componentSelect.append(option);
    }
  } catch (error) {
    // The filter list stays on "Any"; the entries can still be loaded.
  }
}

function apply() {
  currentFilters = readFilters();
  oldestShownId = null;
  return load(false);
}

document.getElementById("log-filters").addEventListener("submit", (event) => {
  event.preventDefault();
  apply();
});
document.getElementById("filters-clear").addEventListener("click", () => {
  componentSelect.value = "";
  severitySelect.value = "";
  traceInput.value = "";
  apply();
});
olderButton.addEventListener("click", () => load(true));

connectLive(); // only drives the connection indicator on this page

// A link such as /logs?trace_id=... opens the page already filtered.
const startTrace = new URLSearchParams(location.search).get("trace_id");
if (startTrace) traceInput.value = startTrace;
loadComponents().then(apply);
