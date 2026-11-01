// overview.js: the Overview page. For now it draws the laptop health tiles.
//
// It listens to the shared live connection (live.js):
//   * "snapshot": sent after every (re)connect. It REPLACES everything shown.
//   * "health":   sent every second with the newest readings.
//   * connection lost: every slot shows "unavailable". The page never keeps showing
//     an old number as if it were current.
//
// A reading the laptop could not take (a sensor missing, broken or too slow) has
// state "unavailable" and no value; its tile shows the word "unavailable".

import { connectLive } from "./live.js";

const UNAVAILABLE_TEXT = "unavailable";
const SLOTS = ["cpu_load", "gpu_load", "ram_used", "cpu_temp", "gpu_temp"];
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

const live = connectLive();
showAllUnavailable();
live.on("snapshot", (data) => showHealth(data.health));
live.on("health", (data) => showHealth(data));
live.on("connection", (state) => {
  if (state === "disconnected") {
    showAllUnavailable();
  }
});
