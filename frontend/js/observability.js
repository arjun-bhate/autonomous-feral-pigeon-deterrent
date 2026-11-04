// observability.js: the Observability page. It shows the stored metrics.
//
//   * Distributions: count, p50 and p95 (nearest-rank) per measurement. A measurement
//     with no samples is not in the server's answer, and if there are none at all the page
//     says "No samples yet" instead of showing zeros.
//   * Counters: running totals, with their labels (for example reason=KEY_OFF).
//   * Gauges: the latest value of each, plus a line chart of its history (Chart.js,
//     vendored in /static/vendor, loaded by observability.html before this file).
//
// Names ending in "_simulated" (and e2e_latency_simulated in particular) are made-up
// timings, so each is labelled "simulated". The page refreshes by itself every 5 s.

import { connectLive } from "./live.js";

const REFRESH_MS = 5000;
const SIMULATED_NAMES = ["e2e_latency_simulated", "homing_drift_simulated"];
const UNIT_SYMBOLS = { percent: "%", celsius: "°C", milliseconds: "ms", degrees: "°", count: "" };

const errorNote = document.getElementById("metrics-error");
const charts = new Map(); // gauge name -> Chart

function showError(message) {
  errorNote.textContent = message || "";
  errorNote.hidden = !message;
}

function isSimulated(name) {
  return SIMULATED_NAMES.includes(name) || name.endsWith("_simulated");
}

function unitText(unit) {
  return Object.hasOwn(UNIT_SYMBOLS, unit) && UNIT_SYMBOLS[unit] ? UNIT_SYMBOLS[unit] : unit;
}

function formatNumber(value) {
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function cell(text) {
  const element = document.createElement("td");
  element.textContent = text;
  return element;
}

function labelsText(labels) {
  const pairs = Object.entries(labels || {}).map(([key, value]) => `${key}=${value}`);
  return pairs.length > 0 ? pairs.join(", ") : "-";
}

function fill(bodyId, emptyId, rows) {
  const body = document.getElementById(bodyId);
  body.replaceChildren(...rows);
  const note = document.getElementById(emptyId);
  if (note) note.hidden = rows.length > 0;
}

function showDistributions(distributions) {
  const rows = distributions.map((item) => {
    const row = document.createElement("tr");
    row.append(
      cell(item.name),
      cell(String(item.count)),
      cell(formatNumber(item.p50)),
      cell(formatNumber(item.p95)),
      cell(item.unit),
      cell(isSimulated(item.name) ? "simulated" : ""),
    );
    return row;
  });
  fill("distributions-body", "distributions-empty", rows);
  // With no samples at all the empty-state note is the only thing shown.
  if (distributions.length === 0) {
    document.getElementById("distributions-empty").hidden = false;
  }
}

function showCounters(counters) {
  const rows = counters.map((item) => {
    const row = document.createElement("tr");
    row.append(cell(item.name), cell(labelsText(item.labels)), cell(formatNumber(item.total)), cell(item.unit));
    return row;
  });
  fill("counters-body", "counters-empty", rows);
}

function showGauges(gauges) {
  const rows = gauges.map((item) => {
    const row = document.createElement("tr");
    row.append(
      cell(item.name),
      cell(formatNumber(item.value)),
      cell(`${item.unit} (${unitText(item.unit) || item.unit})`),
      cell(new Date(item.read_at_us / 1000).toLocaleTimeString()),
    );
    return row;
  });
  fill("gauges-body", "gauges-empty", rows);
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    const problem = await response.json().catch(() => null);
    throw new Error(problem && problem.error ? problem.error.message : `status ${response.status}`);
  }
  return response.json();
}

// One line chart per gauge, redrawn from the stored series each refresh.
async function drawGaugeChart(gauge) {
  if (typeof Chart === "undefined") return; // Chart.js did not load: tables still work
  const series = await getJson(`/api/metrics/${encodeURIComponent(gauge.name)}/series`);
  const labels = series.points.map((point) => new Date(point[0] / 1000).toLocaleTimeString());
  const values = series.points.map((point) => point[1]);
  let chart = charts.get(gauge.name);
  if (!chart) {
    const holder = document.createElement("div");
    holder.className = "chart-box";
    const title = document.createElement("div");
    title.className = "label";
    title.textContent = `${gauge.name} (${series.unit})`;
    const canvas = document.createElement("canvas");
    holder.append(title, canvas);
    document.getElementById("gauge-charts").append(holder);
    chart = new Chart(canvas, {
      type: "line",
      data: { labels, datasets: [{ label: gauge.name, data: values, tension: 0.2, pointRadius: 0 }] },
      options: { animation: false, plugins: { legend: { display: false } } },
    });
    charts.set(gauge.name, chart);
    return;
  }
  chart.data.labels = labels;
  chart.data.datasets[0].data = values;
  chart.update();
}

async function refresh() {
  try {
    const metrics = await getJson("/api/metrics");
    showError("");
    showDistributions(metrics.distributions);
    showCounters(metrics.counters);
    showGauges(metrics.gauges);
    await Promise.all(metrics.gauges.map(drawGaugeChart));
  } catch (error) {
    showError(`Could not load metrics: ${error.message}`);
  }
}

connectLive(); // only drives the connection indicator on this page
refresh();
setInterval(refresh, REFRESH_MS);
