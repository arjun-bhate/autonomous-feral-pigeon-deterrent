// overview.js: the Overview page. It draws the laptop health tiles and the simulated
// device panel (pan, tilt and system state), and the list of recent log entries.
//
// It listens to the shared live connection (live.js):
//   * "snapshot": sent after every (re)connect. It REPLACES everything shown.
//   * "health":   sent every second with the newest readings.
//   * "device":   the simulated device status (pan and tilt in centidegrees, a state name).
//   * "device_offline": the simulator stopped. Pan, tilt and state show "unavailable"
//     and a "Simulator not running" label appears. The last values are never kept.
//   * "log":      one new log entry, added at the TOP of the list (newest first). The
//     snapshot carries the last 50 entries, oldest first, and replaces the list.
//     The list never holds more than 50.
//   * connection lost: every slot shows "unavailable". The page never keeps showing
//     an old number as if it were current.
//
// A reading the laptop could not take (a sensor missing, broken or too slow) has
// state "unavailable" and no value; its tile shows the word "unavailable".

import { connectLive } from "./live.js";

const UNAVAILABLE_TEXT = "unavailable";
const SLOTS = ["cpu_load", "gpu_load", "ram_used", "cpu_temp", "gpu_temp"];
// The twelve device states (codes.py State). The device sends the name; the page shows
// only a name from this list. SAFE_ERROR is written SAFE/ERROR. A name not in the list
// is shown as "unavailable", never as a raw value.
const STATE_NAMES = {
  BOOT: "BOOT",
  HOMING: "HOMING",
  DISARMED: "DISARMED",
  SCANNING: "SCANNING",
  CANDIDATE: "CANDIDATE",
  SLEW: "SLEW",
  CONFIRM: "CONFIRM",
  TRACK: "TRACK",
  DETER: "DETER",
  COOLDOWN: "COOLDOWN",
  ZONE_SETUP: "ZONE_SETUP",
  SAFE_ERROR: "SAFE/ERROR",
};
const RECENT_LOG_LIMIT = 50;
const UNIT_SYMBOLS = { percent: "%", celsius: "°C" };

function slotElements(slot) {
  return {
    value: document.getElementById(`health-${slot}-value`),
    unit: document.getElementById(`health-${slot}-unit`),
  };
}

function showReading(slot, reading) {
  const { value, unit } = slotElements(slot);
  if (!value || !unit) return;
  if (reading && reading.state === "available" && reading.value !== null) {
    value.textContent = reading.value.toFixed(1);
    value.classList.remove("unavailable");
    unit.textContent = UNIT_SYMBOLS[reading.unit] || reading.unit;
  } else {
    value.textContent = UNAVAILABLE_TEXT;
    value.classList.add("unavailable");
    unit.textContent = "";
  }
}

// Draws all five slots from a list of readings. A slot missing from the list is
// shown as unavailable too, so nothing from an earlier update can linger.
function showHealth(health) {
  const byName = {};
  for (const reading of (health && health.readings) || []) {
    byName[reading.name] = reading;
  }
  for (const slot of SLOTS) {
    showReading(slot, byName[slot]);
  }
}

function showAllUnavailable() {
  showHealth(null);
}

// ---- the simulated device panel ----

function setDeviceText(id, text, available) {
  const element = document.getElementById(id);
  if (!element) return;
  element.textContent = text;
  element.classList.toggle("unavailable", !available);
}

function setDeviceUnit(id, text) {
  const element = document.getElementById(id);
  if (element) element.textContent = text;
}

function showAngle(id, centidegrees) {
  if (typeof centidegrees !== "number") {
    setDeviceText(id, UNAVAILABLE_TEXT, false);
    setDeviceUnit(`${id}-unit`, "");
    return;
  }
  setDeviceText(id, (centidegrees / 100).toFixed(2), true);
  setDeviceUnit(`${id}-unit`, "°");
}

function setNotRunningLabel(visible) {
  const label = document.getElementById("device-not-running");
  if (label) label.hidden = !visible;
}

// The simulator is not running (or the server cannot be reached): nothing is known, so
// all three values say "unavailable" and the label says why.
function showDeviceOffline() {
  setDeviceText("device-pan", UNAVAILABLE_TEXT, false);
  setDeviceUnit("device-pan-unit", "");
  setDeviceText("device-tilt", UNAVAILABLE_TEXT, false);
  setDeviceUnit("device-tilt-unit", "");
  setDeviceText("device-state", UNAVAILABLE_TEXT, false);
  setNotRunningLabel(true);
}

function showDevice(device) {
  if (!device || device.running !== true) {
    showDeviceOffline();
    return;
  }
  setNotRunningLabel(false);
  showAngle("device-pan", device.pan_cdeg);
  showAngle("device-tilt", device.tilt_cdeg);
  const known = typeof device.state === "string" && Object.hasOwn(STATE_NAMES, device.state);
  setDeviceText("device-state", known ? STATE_NAMES[device.state] : UNAVAILABLE_TEXT, known);
}

const live = connectLive();
showAllUnavailable();
showDeviceOffline();
// ---- the recent-log list ----

function logRow(entry) {
  const row = document.createElement("li");
  row.className = "log-row";
  row.dataset.severity = String(entry.severity);
  const when = new Date(entry.ts_us / 1000).toLocaleTimeString();
  row.textContent = `${when}  ${entry.severity}  ${entry.component}: ${entry.message}`;
  return row;
}

function updateLogEmptyNote() {
  const list = document.getElementById("recent-logs");
  const note = document.getElementById("recent-logs-empty");
  if (list && note) note.hidden = list.children.length > 0;
}

// Adds one entry at the top and drops the oldest rows beyond the limit.
function addLog(entry) {
  const list = document.getElementById("recent-logs");
  if (!list) return;
  list.prepend(logRow(entry));
  while (list.children.length > RECENT_LOG_LIMIT) {
    list.removeChild(list.lastChild);
  }
  updateLogEmptyNote();
}

// Replaces the whole list. The snapshot is oldest first, so it is reversed here.
function showLogs(entries) {
  const list = document.getElementById("recent-logs");
  if (!list) return;
  list.replaceChildren();
  for (const entry of [...(entries || [])].reverse().slice(0, RECENT_LOG_LIMIT)) {
    list.append(logRow(entry));
  }
  updateLogEmptyNote();
}

live.on("snapshot", (data) => {
  showHealth(data.health);
  showDevice(data.device);
  showLogs(data.recent_logs);
});
live.on("log", (data) => addLog(data));
live.on("health", (data) => showHealth(data));
live.on("device", (data) => showDevice(data));
live.on("device_offline", () => showDeviceOffline());
live.on("connection", (state) => {
  if (state === "disconnected") {
    showAllUnavailable();
    showDeviceOffline();
  }
});
