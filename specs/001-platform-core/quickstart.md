# Quickstart and Validation Guide: Platform Core

This guide is for **after** the feature is implemented. Nothing here can be run yet: no code exists at plan time. It states what to run and what you should see. Details of messages and endpoints are in [contracts/](contracts/); entities are in [data-model.md](data-model.md).

All commands are PowerShell, run from the repository root.

## Prerequisites

- Windows 11, Python 3.12 and uv installed (already true for this project).
- No hardware attached. An NVIDIA GPU is optional (without one the GPU slot reads "unavailable", which is correct).

## 1. Set up and run the automated checks

```powershell
uv sync
uv run ruff check
uv run ruff format --check
uv run pytest
```

Expected: all four succeed. `pytest` includes the vector runner (every file in `protocol/vectors/` passes), the architecture tests (no fire path outside the simulator), and the API and WebSocket tests.

Focused runs, useful while learning the code:

```powershell
uv run pytest tests/contract        # the scenario vectors against the simulator
uv run pytest tests/architecture    # safety boundary checks
uv run pytest tests/unit -k arbitration
```

## 2. Start the system

```powershell
uv run uvicorn deterrent.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/ in a browser. Developer tools are on by default in this feature (`PIGEON_DEV_TOOLS=1`).

Helper for the commands below (PowerShell):

```powershell
$base = "http://127.0.0.1:8000"
function Dev($path, $body = @{}) { Invoke-RestMethod -Method Post -Uri "$base$path" -ContentType "application/json" -Body ($body | ConvertTo-Json -Depth 5) }
```

## 3. Walk through each story

### Story 1: laptop health
1. Open the Overview. **Expect** CPU load, GPU load, RAM and temperatures with units, changing without reloading, at least every 2 seconds.
2. `Dev "/api/dev/sensors/gpu_load/fail" @{on=$true}`. **Expect** the GPU slot reads "unavailable" within about 2 seconds; the others keep updating; no error banner.
3. `Dev "/api/dev/sensors/gpu_load/fail" @{on=$false}`. **Expect** the GPU value returns.
4. On a laptop that does not expose CPU temperature, **expect** that slot to say "unavailable" from the start. This is normal.

### Story 2: Overview shell and simulator state
1. **Expect** labelled placeholders: live camera feed with detection overlay, recent logs, and "Pi health: not connected".
2. `Dev "/api/dev/simulator/start"`. **Expect** state BOOT, then HOMING, then DISARMED, with pan and tilt shown (state changes appear within 1 second).
3. `Dev "/api/dev/command" @{type="MOVE"; fields=@{pan_cdeg=6000; tilt_cdeg=2000}}`. **Expect** pan 60.00, tilt 20.00 on the page within 1 second.
4. `Dev "/api/dev/simulator/stop"`. **Expect** pan, tilt and state read "unavailable" (not the last values) and the page makes clear the simulator is stopped.

### Story 3: logs
1. Run an engagement: `Dev "/api/dev/engagement" @{fire=$true; duration_ms=200}` and note the returned `trace_id`.
2. Open `/logs`. Filter by one component, then one severity, then the trace id. **Expect** exactly the matching entries each time, and a "nothing matched" message for a filter with no hits. Combining filters shows only entries matching all.

### Story 4: trace
1. Open `/traces`, pick the trace. **Expect** eight steps in order: capture, encode, network, inference, decision, command, esp32_ack, effector_action, each with start and duration, marked simulated where timings are made up, and a total equal to the end of the last step minus the start of the first.
2. With the key off (the default), the fire is denied: **expect** `esp32_ack` shows DENIED with `KEY_OFF` and `effector_action` shows "no action".
3. `Dev "/api/dev/key" @{on=$true}`, send ARM through `/api/dev/command`, and run the engagement again. **Expect** a granted trace with effector action shown. (This also needs the simulated person-clear heartbeat, which the client sends automatically; after the simulator starts, wait at least 5 seconds for the person re-engage delay, otherwise the fire is denied `PERSON_CLEAR_DELAY`.)

### Story 5: metrics
Run several engagements, then open `/observability`. **Expect** counters (granted and denied by reason), gauges, and p50 and p95 for end-to-end latency (labelled simulated). With a fresh database and no engagements, **expect** an empty state, not zeros.

### Story 6: simulator on its own
```powershell
uv run pytest tests/unit/test_arbitration.py tests/unit/test_zones.py -v
```
**Expect** one passing test per denial reason and per zone rejection. Hand-driven checks: send `FIRE_REQUEST` through `/api/dev/command` in different conditions and confirm the denial `code` matches the Code column of the ladder table in [contracts/protocol.md](contracts/protocol.md).

### Story 7: protocol and vectors
1. `uv run pytest tests/contract -v`. **Expect** every vector passes.
2. Read `protocol/PROTOCOL.md` alone, pick five vectors from `protocol/vectors/`, build their messages by hand from the document and predict the replies. **Expect** your predictions match the `expect_reply` fields (SC-018).

## 4. Edge checks

| Check | How | Expect |
|-------|-----|--------|
| Dashboard disconnect | Stop the server, wait 5 s, start it again | "Disconnected" indicator while down; current values return within 5 s with no manual reload |
| Several tabs | Open the Overview in two tabs | Same live values in both |
| Storage fault | `Dev "/api/dev/storage/fail" @{mode="locked"}` | A visible storage warning within 10 s; live values keep updating; then `mode="ok"` clears it and kept entries are saved |
| Corrupted zone store | `Dev "/api/dev/zone-store/corrupt"` then a fire request | Denied `ZONE_STORE_INVALID` until zones are cleared |
| Restart persistence | Run an engagement, restart the server | Logs, traces and metrics still present; stored zones still listed by the simulator |
| Key off | Arm the device, then `Dev "/api/dev/key" @{on=$false}` | State SAFE/ERROR (not DISARMED), armed false. Key back on: state DISARMED, still not armed until ARM is sent. Same result if the key goes off during a zone upload (upload discarded) |
| Person inhibit | `Dev "/api/dev/person" @{clear=$false}`, then send a `FIRE_REQUEST`; then `@{clear=$true}` and send requests at once, after 4 s and after 5 s | Denied `PERSON_NOT_CLEAR` while a person is reported (a running shot is cut, state kept, still armed, no fault). After clear: denied `PERSON_CLEAR_DELAY` until 5 s of continuous clear, granted at 5 s or later. A cut shot does not restart by itself |
| Reason codes | `uv run pytest tests/contract -k reason_codes` | Passes: codes unique, document and `protocol/reason-codes.json` agree, no released code changed |
| Heartbeat loss | Stop the simulator's heartbeat source (stop the client) | After about 1 s state is SAFE/ERROR; effector off; not re-armed when heartbeat returns |

## 5. Privacy and safety checks

```powershell
git status --short          # data/ and *.sqlite3 must not appear
uv run pytest tests/architecture
```

**Expect** no database files in Git status, and the architecture tests to pass (no module outside `deterrent/simulator/` can grant a fire or switch the effector).

## 6. Done criteria for the feature

All of: section 1 passes; each story's walk-through behaves as described; edge checks behave as described; the measured success criteria SC-001 to SC-019 are recorded in the final task report with their numbers.
