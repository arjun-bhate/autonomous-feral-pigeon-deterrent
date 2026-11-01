// live.js: the one WebSocket connection to the server, shared by every page.
//
// What it does, in plain words:
//   * opens ONE WebSocket to /ws/live (the server only sends; the page never sends);
//   * the server sends a "tick" every second, so if NOTHING arrives for 3 seconds the
//     connection is treated as down: the indicator turns red and the socket is closed;
//   * after any loss it reconnects by itself, waiting 1 s, then 2 s, then 4 s, and 4 s
//     again for every later attempt;
//   * every time a connection succeeds the server first sends a "snapshot" (all current
//     values). Pages must REPLACE what they show with the snapshot, never merge with
//     old values, so nothing stale survives a reconnect.
//
// Pages use it like this:
//   const live = connectLive();
//   live.on("health", (data) => { ... });   // any message type: snapshot, health, device,
//                                           // device_offline, log, storage_warning, tick
//   live.on("connection", (state) => { ... }); // "connecting" | "connected" | "disconnected"

// Reconnect schedule: wait 1 s, then 2 s, then 4 s; every later attempt waits 4 s again.
export const RECONNECT_DELAYS_MS = [1000, 2000, 4000];
// No message for this long (3 s) means the link is down, even if the socket looks open.
export const DISCONNECT_AFTER_MS = 3000;

const INDICATOR_LABELS = {
  connecting: "Connecting…",
  connected: "Live",
  disconnected: "Disconnected",
};

export function nextReconnectDelay(attempt) {
  return RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
}

function liveUrl() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}/ws/live`;
}

function showState(state) {
  const indicator = document.getElementById("connection-indicator");
  if (!indicator) return;
  indicator.dataset.state = state;
  indicator.textContent = INDICATOR_LABELS[state];
}

export function connectLive() {
  const listeners = {};
  let socket = null;
  let silenceTimer = null;
  let reconnectTimer = null;
  let attempt = 0;
  let stopped = false;

  function emit(type, data) {
    for (const listener of listeners[type] || []) {
      try {
        listener(data);
      } catch (error) {
        console.error("live listener failed:", error);
      }
    }
  }

  function setState(state) {
    showState(state);
    emit("connection", state);
  }

  function restartSilenceTimer() {
    clearTimeout(silenceTimer);
    silenceTimer = setTimeout(() => {
      // Nothing for DISCONNECT_AFTER_MS: give up on this socket. Closing it
      // makes onclose run, which schedules the reconnect.
      setState("disconnected");
      socket.close();
    }, DISCONNECT_AFTER_MS);
  }

  function scheduleReconnect() {
    clearTimeout(silenceTimer);
    if (stopped) return;
    reconnectTimer = setTimeout(open, nextReconnectDelay(attempt));
    attempt += 1;
  }

  function open() {
    setState("connecting");
    socket = new WebSocket(liveUrl());
    restartSilenceTimer();
    socket.onmessage = (event) => {
      restartSilenceTimer();
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        return; // not JSON: ignore it
      }
      if (message.type === "snapshot") {
        attempt = 0; // a real connection: the next loss starts the schedule again at 1 s
        setState("connected");
      }
      emit(message.type, message.data);
    };
    socket.onclose = () => {
      setState("disconnected");
      scheduleReconnect();
    };
    socket.onerror = () => socket.close();
  }

  open();

  return {
    on(type, listener) {
      (listeners[type] = listeners[type] || []).push(listener);
    },
    stop() {
      stopped = true;
      clearTimeout(silenceTimer);
      clearTimeout(reconnectTimer);
      socket.close();
    },
  };
}
