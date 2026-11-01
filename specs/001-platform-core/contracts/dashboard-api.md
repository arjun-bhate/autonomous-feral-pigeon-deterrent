# Dashboard API Contract (draft v0.1)

Server: FastAPI, bound to `127.0.0.1:8000` only. JSON everywhere. No login (single user, loopback only). Timestamps are UTC epoch microseconds (`*_us`); the browser formats them for display.

Errors use one shape: `{"error": {"code": "SHORT_CODE", "message": "plain words"}}` with a suitable HTTP status.

No endpoint grants or performs a fire. The only fire-related endpoint is the developer tool that **sends a fire request** through the normal client and returns the device's reply (see section 4).

## 1. Pages (static files)

| URL | File |
|-----|------|
| `/` | `frontend/index.html` (Overview) |
| `/logs` | `frontend/logs.html` |
| `/traces` | `frontend/traces.html` (list, and `?trace_id=` for one trace) |
| `/observability` | `frontend/observability.html` |
| `/static/...` | css, js, vendor |

## 2. Read endpoints

### GET /api/health/current
Latest laptop readings.
```json
{ "readings": [
  { "name": "cpu_load", "value": 12.5, "unit": "percent", "state": "available", "read_at_us": 0 },
  { "name": "cpu_temp", "value": null, "unit": "celsius", "state": "unavailable", "read_at_us": 0 }
] }
```

### GET /api/device/status
```json
{ "running": true, "state": "DISARMED", "pan_cdeg": 0, "tilt_cdeg": 0, "key_on": false,
  "armed": false, "homed": true, "zone_store_valid": true, "effector_on": false,
  "fault_mask": 0, "zone_count": 0, "updated_us": 0 }
```
When the simulator is not running: `{ "running": false }` and the Overview shows "unavailable" for pan, tilt and state.

### GET /api/system/status
`{ "simulator_running": true, "storage_warning": null | {"message": "...", "since_us": 0}, "dev_tools": true }`

### GET /api/logs
Query: `component`, `severity`, `trace_id` (all optional, combined with AND), `limit` (default 200, max 1000), `before_id` (paging). Ordered oldest to newest within the page.
```json
{ "items": [ { "id": 1, "ts_us": 0, "component": "simulator", "severity": "INFO",
               "message": "...", "trace_id": null, "span_id": null, "fields": {} } ],
  "has_more": false }
```
An empty match returns `items: []` (not an error).

### GET /api/logs/components
`{ "components": ["api", "engagement", "health", "link", "simulator", "store"] }` (for the filter drop-down).

### GET /api/traces
Query: `limit` (default 50). Newest first.
`{ "items": [ { "trace_id": "...", "started_us": 0, "total_us": 0, "outcome": "DENIED", "deny_reason": "KEY_OFF", "simulated": true } ] }`

### GET /api/traces/{trace_id}
```json
{ "trace_id": "...", "started_us": 0, "ended_us": 0, "total_us": 0, "outcome": "DENIED",
  "deny_reason": "KEY_OFF", "simulated": true,
  "spans": [ { "position": 1, "name": "capture", "started_us": 0, "duration_us": 0,
               "status": "OK", "simulated": true, "attrs": {} } ] }
```
Always eight spans for a completed trace. 404 with `TRACE_NOT_FOUND` otherwise.

`outcome` is one of `GRANTED`, `DENIED`, `REJECTED`, `TIMEOUT`, `NO_FIRE_REQUESTED`, `RUNNING`. A span `status` is one of `OK`, `DENIED`, `REJECTED`, `TIMEOUT`, `NOT_RUN`. When the device replies REJECTED (for example `FIELD_RANGE` for `duration_ms` 499, or `NOT_HOMED` in BOOT) the `esp32_ack` span has status `REJECTED` and `attrs.reject_code` holds the reject code name, the trace outcome is `REJECTED` and `deny_reason` is null. When the device denies, `esp32_ack` has status `DENIED` and `attrs.reason` holds the denial name. After any outcome other than `GRANTED`, `effector_action` has status `NOT_RUN` and `attrs.note` is "no action". Names only, never numeric codes.

### GET /api/metrics
```json
{ "counters": [ { "name": "fire_requests_denied", "labels": {"reason": "KEY_OFF"}, "total": 3, "unit": "count" } ],
  "gauges": [ { "name": "laptop_cpu_load", "value": 12.5, "unit": "percent", "read_at_us": 0 } ],
  "distributions": [ { "name": "e2e_latency_simulated", "unit": "milliseconds", "count": 40, "p50": 0, "p95": 0,
                       "method": "nearest-rank" } ] }
```
A distribution with no samples is omitted (the page shows an empty state), never reported as zeros.

### GET /api/metrics/{name}/series
Query: `since_us`, `labels` (optional). Returns points for a Chart.js line chart: `{ "unit": "...", "points": [[ts_us, value], ...] }`.

## 3. WebSocket /ws/live

Server to client only (the client sends nothing). Every message: `{ "type": "...", "data": { ... } }`.

| type | When | data |
|------|------|------|
| `snapshot` | immediately after connect | `health` (same as /api/health/current), `device` (same as /api/device/status), `system` (same as /api/system/status), `recent_logs` (last 50) |
| `health` | every collection (1 s) | `readings` |
| `device` | on every device status change, and at least once a second while running | device status |
| `device_offline` | simulator stopped | `{}` |
| `log` | each new log entry | one log item |
| `storage_warning` | storage fault raised or cleared | `{ "message": "..." }` or `null` |
| `tick` | every 1 s | `{ "server_us": 0 }` |

Client behaviour (contract for `live.js`): no message for 3 s means "disconnected" (indicator turns red); reconnect after 1 s, 2 s, then every 4 s; every successful connect replaces all displayed live values with the new snapshot.

## 4. Developer tools (only when `PIGEON_DEV_TOOLS=1`)

When disabled these routes return 404. They are for the owner and tests. None of them can decide, grant or perform a fire.

| Method and path | Body | Effect |
|-----------------|------|--------|
| POST `/api/dev/simulator/start` | none | Start the simulated ESP32 (boots, homes, ends DISARMED) |
| POST `/api/dev/simulator/stop` | none | Stop it (Overview shows "unavailable") |
| POST `/api/dev/key` | `{"on": true}` | Set the simulated key input |
| POST `/api/dev/person` | `{"clear": false}` | Set the person-clear value that the client puts in every heartbeat (stands in for the future detector; default clear). The device then applies the person inhibit and re-engage delay itself |
| POST `/api/dev/command` | `{"type": "MOVE", "fields": {...}}` | Send one protocol command through the normal client; returns the decoded reply. Accepts any type in the protocol including `ARM`, `DISARM`, `FIRE_REQUEST` and `FIRE_STOP`; a fire request returns the device's GRANTED / DENIED reply unchanged |
| POST `/api/dev/engagement` | `{"fire": true, "duration_ms": 500}` | Run one simulated engagement; `duration_ms` is the shot's maximum on-time and is passed to the device unchanged (the device accepts 500 up to its per-shot limit); returns `{"trace_id": "..."}` |
| POST `/api/dev/zone-store/corrupt` | none | Corrupt one stored zone (test input) |
| POST `/api/dev/sensors/{name}/fail` | `{"on": true}` | Make a health sensor unreadable (hang or error) |
| POST `/api/dev/storage/fail` | `{"mode": "full" \| "locked" \| "ok"}` | Make the observability store fail, to see the warning |

## 5. Rules the API must keep

1. No route, in any mode, grants or performs a fire. The only authority is the device's reply (FR-021).
2. The laptop holds no authoritative copy of zones. Zone data shown anywhere comes from a `ZONE_LIST` reply.
3. No response contains secrets or image data (FR-014).
4. Denial reasons are always shown and returned by **name** (for example `KEY_OFF`); numeric wire codes and priorities never appear in the API or the pages.
5. A slow or failing store must not slow these endpoints' live paths: `/ws/live` and `/api/device/status` read from memory.
6. Shot duration and stopping: the operator control that offers 500 to 5000 ms in 500 ms steps belongs to a later feature (there is no fire control in this feature). The API passes `duration_ms` through unchanged, so dev tools can send any value and exercise the device's own range check (500 to its per-shot limit). `FIRE_STOP` is sent like any other command through `/api/dev/command`; the API never decides whether it takes effect. The `state` field can read `COOLDOWN`, and denials appear by name (`COOLDOWN_ACTIVE`), never by number.
