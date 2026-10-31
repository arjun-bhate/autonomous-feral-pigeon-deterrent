# Research and Design Decisions: Platform Core (No Hardware)

Each entry has: **Decision**, **In plain words** (what it means and why, written so the owner can explain it), **Alternatives considered**. The stack itself (Python 3.12, uv, FastAPI, SQLite) is fixed by the constitution and is not discussed.

No `NEEDS CLARIFICATION` items remain. Things the owner should confirm are at the end.

---

## Part A. Structure

### R1. One package, pure simulator core, one wiring file
**Decision**: A single package `deterrent` with a sub-package per job. The simulator (`deterrent.simulator`) is pure logic: it takes bytes and a time value in, and returns bytes and events out. It does not import FastAPI, SQLite for observability, or logging. Only `composition.py` connects the pieces.

**In plain words**: Real firmware cannot use web frameworks or a laptop database, so the simulator is written the same way. That has three payoffs. (1) Tests can drive it with a fake clock and no threads. (2) The safety logic lives in a few small files you can read in one sitting. (3) A test can prove that no other package can make a fire happen, because no other package is allowed to import the simulator.

**Alternatives**: Separate processes per component (simulator as its own program). Rejected for now: more moving parts to start and debug, and nothing in the spec needs it. The pure core can be wrapped in its own process later without changing its logic.

### R2. Simulator runs inside the backend process as an async task
**Decision**: A thin `runner.py` calls the device's `tick(now)` every 10 ms and moves bytes between the device and an in-memory link. The link still carries real framed bytes, so framing, checksums and sequence numbers are exercised exactly as on a serial cable.

**In plain words**: "Start simulator" and "stop simulator" become starting and stopping a background task, which is what the Overview needs to show "unavailable". The 10 ms tick, plus a safety re-check every time a message arrives, is how the effector is cut well inside the 100 ms limit.

**Alternatives**: TCP socket between two processes (more realistic, more fragile on a laptop with firewall prompts). Can be added later as another `DeviceLink`.

### R3. Hardware-facing interfaces behind small interfaces with mocks
**Decision**: `Clock`, `DeviceLink`, `SensorProvider` and `ZoneStorage` are interfaces. Each has a real or in-process implementation and a mock that tests control. The observability store also has a fault-injection hook for "disk full / locked".

**In plain words**: Whatever touches the outside world is hidden behind a plug-shaped interface. In tests we plug in a fake that can fail on demand, so we can test "GPU sensor unreadable" or "storage locked" without breaking real hardware.

---

## Part B. Protocol

### R4. Binary frames, COBS delimiting, CRC-16, integer centidegrees
**Decision**: Each message is `type (1 byte) | sequence (2 bytes) | payload | CRC-16 (2 bytes)`, little-endian, then COBS-encoded and ended with a zero byte. The CRC is CRC-16/CCITT-FALSE over type, sequence and payload. Angles are signed 16-bit integers in hundredths of a degree (centidegrees). Maximum raw frame is 64 bytes.

**In plain words**:
- *Frame end marker*: COBS is a simple trick that guarantees the byte `0x00` never appears inside a message, so `0x00` always means "message over". If the receiver loses its place (noise, half a message), it just waits for the next `0x00` and is back in sync. That is exactly what you want on a serial cable.
- *CRC-16*: a 2-byte fingerprint of the message. If any bit flips in transit the fingerprint no longer matches and the message is thrown away.
- *Centidegrees as integers*: Python and C++ round decimals differently. Integers are identical everywhere, so the simulator and the real firmware will give the same answer for the same zone geometry. 240 degrees = 24000, which fits in a 16-bit integer.

**Alternatives**: JSON lines (easy to read, but needs a heavy parser on the ESP32, is slower, and floats cause cross-language differences). Length-prefix framing (loses sync permanently after one corrupted length). SLIP framing (also fine, but COBS has a fixed, predictable overhead).

### R5. Sequence numbers, duplicates and stale messages
**Decision**: Host-to-device messages carry a 16-bit sequence number. The device remembers the last accepted number and a cache of its last 8 replies. Rules, evaluated after CRC and length checks:
- Same number as one in the cache: it is a duplicate. Resend the cached reply, do nothing else.
- Newer (forward distance 1 to 32767, wrapping): accept.
- Older and not in the cache: reject with `STALE_SEQ`.
- First message after a device boot is accepted whatever its number.
A stale or duplicate heartbeat never refreshes the heartbeat timer.

**In plain words**: The number is a ticket. Seeing the same ticket twice means "the Wi-Fi repeated itself", so answer again but do not act again. This is how a repeated fire request can never produce two grants. A ticket older than the newest one is a message that arrived late and out of order, so it is refused.

**Alternatives**: Reject all gaps (too strict on a lossy link). Per-message-type sequence spaces (more state, no benefit).

### R6. Time: a device time base, with TTL stated against it
**Decision**: The device has one clock (a `Clock` object; fake in tests). Every device reply to a heartbeat includes the device's current time in milliseconds. The laptop's `DeviceClient` keeps the offset and writes `issued_at_ms` in the device's time base on each fire request, along with `ttl_ms`. The device denies with `REQUEST_EXPIRED` if `now > issued_at_ms + ttl_ms` (with 32-bit wrap arithmetic). Heartbeat freshness and the person-clear hold-off are measured by the device's own receive time, never by the timestamp inside the heartbeat.

**In plain words**: A "time to live" only makes sense if both sides agree on what time it is, but the laptop's clock and the ESP32's clock never match. So the device tells the laptop what time the device thinks it is, and the laptop stamps requests in the device's time. The heartbeat's own timestamp is kept for logs and latency, but is never trusted for safety.

**Alternatives**: Compare laptop wall-clock to device wall-clock (wrong whenever clocks drift). Have the laptop send "valid for 500 ms from arrival" (the device cannot know how long the message was delayed on Wi-Fi, which is the exact case TTL exists for).

### R7. Fire decision: one function, fixed priority, plain list of reasons
**Decision**: `arbitration.py` evaluates conditions in this fixed order and returns the first failure; if none fail it returns granted.

| Priority | Denial reason | Wire code | The one condition that failed | Also cuts the effector if on? |
|---------:|---------------|----------:|-------------------------------|-------------------------------|
| 1 | HEARTBEAT_LOST | 1 | Heartbeat not alive (none yet, or silent longer than the watchdog) | Yes, and SAFE_ERROR |
| 2 | FAULT_ACTIVE | 2 | A fault bit other than HEARTBEAT_LOST or KEY_OFF is set (only reserved bits or test injection in this feature) | Yes |
| 3 | ZONE_SETUP | 3 | Device is in ZONE_SETUP | n/a |
| 4 | NOT_HOMED | 4 | Not homed (includes HOMING and BOOT) | n/a |
| 5 | KEY_OFF | 5 | Key input is off (live level) | Yes, and SAFE_ERROR |
| 6 | NOT_ARMED | 6 | Not armed | Yes (via DISARM) |
| 7 | PERSON_NOT_CLEAR | 7 | Person reported, or status missing or stale | Yes, state kept |
| 8 | PERSON_CLEAR_DELAY | 14 | Person gone, but not yet clear for the re-engage delay | n/a |
| 9 | REQUEST_EXPIRED | 8 | Request's TTL has passed | No |
| 10 | ZONE_STORE_INVALID | 9 | Derived zone-store validity is false | Yes, state kept |
| 11 | ELEVATION_LIMIT | 10 | Current tilt outside the elevation limits | Yes, state kept |
| 12 | IN_ZONE_MARGIN | 11 | Current direction inside a zone or within margin of its edge | Yes, state kept |
| 13 | ON_TIME_LIMIT | 12 | Requested duration exceeds the per-shot limit | Yes (shot timer) |
| 14 | RATE_LIMIT | 13 | Per-minute on-time budget would be exceeded | No |

**Priority and wire code are separate data (owner decision 2026-10-31).** An earlier draft used the position in the list as the number on the wire. That tied an identifier that must never change to an order that may change whenever a condition is added, and inserting `PERSON_CLEAR_DELAY` would have renumbered every later reason. Codes 1 to 13 therefore keep the values they had before that insertion, `PERSON_CLEAR_DELAY` is 14, and the rules in protocol.md section 6.1 apply (never reused, next unused number for a new reason, version change if a meaning changes). A lock test enforces this.

Order logic: device-level conditions first (is the device alive and in a sane state), then operator conditions (key, arm), then the request itself (person, expiry), then geometry, then budgets.

Every denial is a normal reply: one reason, no state change, no latched fault. Person detection, zone-margin and elevation-limit conditions are denials (and cuts), not faults, as decided by the owner on 2026-10-31 and written into `docs/system-scope.md` v1.5. Audit result: each reason names one condition. No reason had to be split. `PERSON_NOT_CLEAR` deliberately covers "person reported, or no status, or stale status" because the owner kept it as one condition: person-clear cannot be asserted. A new reason `PERSON_CLEAR_DELAY` was added after it.

**In plain words**: When several things are wrong at once the device must still give one clear answer. The list is a fixed ladder, top to bottom, so the answer never depends on luck and every test can aim at exactly one rung by making only that condition fail. The device decides using its own measured angles, not the direction written in the request; the request's direction is logged, and a mismatch beyond 1 degree is logged as a warning, not used for the decision.

The effector, once on, is re-checked against rungs 1 to 12 except rung 9 (`REQUEST_EXPIRED`, a precondition checked once at admission) on every tick and on every received message, plus an on-time timer for rung 13. Any failure cuts it and counts as an interlock event. Only heartbeat loss and key off also change the state (to SAFE_ERROR); every other cut simply ends the shot and the device stays armed.

**Alternatives**: Return all failed reasons (richer, but the spec requires exactly one). Fire direction taken from the request (unsafe if the turret is not where the laptop believes).

### R8. Device states used in this feature
**Decision**: The simulator really enters: BOOT, HOMING, DISARMED, SCANNING (armed and idle), DETER (effector on), ZONE_SETUP, SAFE/ERROR. The enum also contains CANDIDATE, SLEW, CONFIRM, TRACK and COOLDOWN so the dashboard can show all twelve names, but the simulator does not enter them yet because they belong to the engagement logic in feature 005. Key off (the change from on to off) and heartbeat loss both move the device to SAFE/ERROR from any state and cut the effector, as `docs/system-scope.md` requires ("Any state -> SAFE/ERROR on ... lost heartbeat ... key off"). Leaving SAFE/ERROR follows one recovery rule (key back on for a key-off fault, a new heartbeat for a heartbeat fault) and always lands in DISARMED, never armed. Re-arming is always a deliberate command. `DISARM` is accepted in every state (spec FR-020) and replies OK; in BOOT, HOMING and SAFE_ERROR it changes nothing (homing is not abandoned and SAFE_ERROR is left only by its recovery rule), and in every other state it moves to DISARMED. The details are in protocol.md section 5.2 (arm, disarm and key off) and 5.6 (SAFE_ERROR).

**Correction (earlier wording was wrong)**: the first version of this plan said key off "auto-disarms" the device (goes to DISARMED) and that the spec was silent. That contradicted `docs/system-scope.md`, which is a source of truth and says key off sends any state to SAFE/ERROR. It was missed because only the spec was checked, not the scope document's state list. The owner chose to follow the scope document (decision 2026-10-31); all plan files were corrected. Two other triggers in the same scope line, "person detected" and "limit or zone violation", are still handled here as effector cut plus return to SCANNING; see follow-up item 11 below.

**In plain words**: Only states the device itself can reach on its own are simulated. The "hunting" states come from the laptop's engagement logic later; adding them now would be inventing behaviour the spec did not ask for.

### R9. Zone upload protocol and ZONE_SETUP
**Decision**: Uploading is `ZONE_BEGIN(zone_id, point_count)`, then `ZONE_POINT(index, pan, tilt)` repeated, then `ZONE_COMMIT(CRC-32)`. `ZONE_BEGIN` is allowed only when homed and disarmed (or already in ZONE_SETUP) and moves the device into ZONE_SETUP. Every way in and out of ZONE_SETUP is listed in one table, "Leaving ZONE_SETUP" in [contracts/protocol.md](contracts/protocol.md) section 5.5 (the state diagram in [data-model.md](data-model.md) matches it row for row). In short: an accepted or rejected commit, the 5 s idle timeout, or `DISARM` return to DISARMED; key off or heartbeat loss go to SAFE_ERROR; a new accepted BEGIN restarts the upload; a rejected BEGIN changes nothing. There is no abort message: `DISARM` is the single abort, and the partial upload (a staging buffer) is discarded on every exit except an accepted commit. The commit writes the zone in one SQLite transaction after all checks (count, CRC-32, minimum 3 points, no self-crossing, no duplicate id, store not full). `ZONE_DELETE`, `ZONE_CLEAR` require disarmed or ZONE_SETUP. `ZONE_LIST` is allowed anywhere.

**In plain words**: A zone arrives in pieces and only becomes real at the last step, like posting a parcel that is only accepted if everything inside matches the packing list. Anything wrong and the parcel is thrown away and the saved zones stay exactly as they were. Entering ZONE_SETUP when a drawing session starts means the effector is hard-disabled while zones are being changed.

**Gap in the spec (flagged)**: The spec lists the command set without an explicit "enter/leave ZONE_SETUP" command, yet the state exists. This plan lets `ZONE_BEGIN` enter it and the exits in the table leave it, with `DISARM` as the abort (no new message type). The long "drawing by jogging" session described in the constitution is built in a later feature, which may add explicit enter/exit commands.

### R10. Zone geometry in exact integer arithmetic
**Decision**: Polygon code uses integer centidegrees only. Self-crossing uses orientation tests (exact). Point-in-zone-with-margin = inside by the even-odd ray rule, OR squared distance to any edge ≤ margin squared (int64). The polygon closes automatically from the last point to point 1. Margin = 50 centidegrees (0.5 degrees) + the configured aiming error (default 20 = 0.2 degrees). Pan and tilt are treated as flat x/y coordinates. Duplicate consecutive points and zero-area polygons are rejected as `ZONE_DEGENERATE`.

**In plain words**: No decimals means no rounding surprises, and the same maths can be copied into C++ and give identical answers. Treating pan and tilt as a flat grid is slightly off near steep tilt angles; with the 8 and 16 point limits and a 0.7 degree margin this is acceptable for v1, and it is noted as a known simplification to revisit with real data.

**Alternatives**: Floating-point geometry with shapely (fast to write, but not portable to the ESP32 and a dependency for a safety check). Spherical geometry (more correct, more code, no benefit before real aiming error is measured).

### R11. Zone limits, persistence and corruption
**Decision**: Defaults: 8 zones, 16 points per zone (closing open decision 5; both configurable). Each stored zone keeps its CRC-32. Zone-store validity is a **derived value**: recomputed from the persisted bytes whenever it is needed (every fire decision, every tick while the effector is on, after every write, in the status block), never kept as a stored flag. An empty store is valid. An unreadable store counts as invalid; if it becomes readable again and every checksum matches it is valid again without `ZONE_CLEAR`; corrupted bytes stay invalid until `ZONE_CLEAR` leaves a valid store (owner decision 2026-10-31). The persistent copy is a separate SQLite file behind the `ZoneStorage` interface, one row per zone (id, CRC-32, points as bytes). A test hook can corrupt a row; the mock storage can report "full" or "locked".

**In plain words**: 8 by 16 points is 512 bytes, tiny for an ESP32. The device stores a fingerprint with each zone and re-checks it; if any fingerprint fails, it refuses all firing until the checksums match again (readable again after a read problem) or `ZONE_CLEAR` leaves a valid store (corrupted bytes). Why derived and not a flag: an earlier draft also had a fault bit for this, which made rung 2 (any fault) hide rung 10 (store invalid), so the named reason could never be returned. With one source of truth (the stored bytes) that bug cannot exist. It is a different file from the observability database so that laptop code cannot reach it, mirroring that the real zones live only inside the ESP32.

### R12. Fixed timing defaults (all configurable)
Heartbeat interval 200 ms, watchdog 1000 ms, person-clear hold-off (status staleness limit) 1000 ms (provisional; equal to the watchdog, so a silent laptop shows HEARTBEAT_LOST first and a stale-status PERSON_NOT_CLEAR is reachable only when the hold-off is below the watchdog; both to be re-tuned in feature 004 with measured Wi-Fi jitter), person re-engage delay 5000 ms (provisional, owner decision 2026-10-31), fire TTL 500 ms, per-shot 2000 ms, per-minute 10000 ms, aiming error 0.2 degrees, zone upload inactivity timeout 5000 ms (new), simulated homing duration 3000 ms (new), engagement acknowledgement timeout 500 ms (new). Elevation limits default to the full tilt travel (0 to 80 degrees) until placement is known (open decision 1). Angles are measured from the homed zero at one end of travel: pan 0 to 240 degrees, tilt 0 to 80 degrees.

**Flag**: "zero at the end of travel" is an assumption; it only matters when real hardware arrives (feature 003).

---

## Part C. Observability and storage

### R13. Logging: standard library, JSON, trace context by context variables
**Decision**: Python's built-in `logging` with a JSON formatter. A `contextvars` value carries the current trace id and span id so any log line written during an engagement is tagged automatically. A logging filter drops fields named like secrets and rejects byte payloads that look like image data. A custom handler passes records to the store writer.

**In plain words**: No new logging library to learn. "Context variables" are a hidden notepad that follows the code as it runs; once the engagement writes its trace id there, every log line during it carries that id, which is what makes "filter by trace id" work.

**Alternatives**: structlog (nice, but one more concept for no gain here).

### R14. Traces: small own implementation with eight fixed spans
**Decision**: `tracing.py` defines `Trace` and `Span`. A simulated engagement creates spans in the fixed order: capture, encode, network, inference, decision, command, esp32_ack, effector_action. Start time is wall-clock microseconds (for display); duration comes from a monotonic clock (for accuracy). A span has a status: OK, DENIED, TIMEOUT, NOT_RUN. Spans are sequential, so the total is the end of the last minus the start of the first, and they cannot overlap by construction.

**In plain words**: A trace is the stopwatch record of one engagement, split into eight named laps. A lap that never finished is stored as TIMEOUT instead of being left out.

**Alternatives**: OpenTelemetry (powerful, but large, and the constitution asks for exactly these eight spans in local SQLite; can be exported to later if wanted).

### R15. Metrics: store raw samples; percentiles by nearest-rank
**Decision**: Three kinds: counters (stored as increment events), gauges (current value in memory for the live view, persisted every 10 s), latency distributions (every sample stored). The percentile method is nearest-rank: sort the n samples and take the value at position ceil(p/100 × n). The Observability page and the tests both use it, and the test computes it a second, independent way.

**In plain words**: Keeping every sample means p50 and p95 are exact and anyone can recompute them. Gauges are saved every 10 s rather than every second to keep the file small (about 5 MB per day). "Nearest-rank" is the simplest percentile rule to explain: sort, then pick the item at the percentile position.

**Alternatives**: Prometheus-style histogram buckets (approximate, and the spec requires matching an independent calculation).

### R16. SQLite: WAL mode, background writer, memory buffer, retry, warning
**Decision**: One writer thread owns the observability database connection (WAL mode). Producers (logging, metrics, tracing) put records on a bounded queue and never wait. If a write fails with "disk full" or "database locked", the writer sets a storage-warning flag (pushed to the dashboard within 10 s), keeps the newest 5000 records in a memory ring, retries every 2 s, and logs the fault; on recovery it writes what it kept. All timestamps are stored as UTC epoch microseconds. Tables and indexes (component, severity, trace id, time) are in [data-model.md](data-model.md). There is no retention in this feature.

**In plain words**: Writing to disk is slow and can fail. Code that does safety work must never wait for it. So everything drops its record into a queue and carries on; a single helper does the slow disk work, and if the disk misbehaves it holds the recent records in memory and shows a warning.

### R17. Laptop health sensors
**Decision**: `psutil` for CPU load and RAM. `nvidia-ml-py` (NVML) for GPU load and GPU temperature. CPU temperature on Windows is normally not exposed by `psutil` and needs a third-party driver, so it is expected to read "unavailable"; the spec treats that as normal. Each sensor read runs in a worker thread with a 1 s timeout; one slow sensor cannot hold up the others. The collector samples every 1 s. A reading that failed this round is "unavailable", never the old value.

**In plain words**: Each sensor gets its own short deadline. If one stalls, only its slot says "unavailable" and the rest keep their schedule.

**Alternatives**: LibreHardwareMonitor / WMI for CPU temperature (needs admin rights or drivers; deferred, can be added as another `SensorProvider` without changing anything else).

---

## Part D. API and dashboard

### R18. REST for lookups, one WebSocket for live updates
**Decision**: REST endpoints for logs, traces, metrics and current values. One WebSocket `/ws/live` pushes health readings, device status, new log lines, storage warnings and a `tick` once per second. On connect the server first sends a `snapshot` of current values. Full definitions are in [contracts/dashboard-api.md](contracts/dashboard-api.md).

**In plain words**: Fetching history is a question and answer (REST). Live values are pushed (WebSocket) so the page never has to ask. The one-per-second `tick` is a pulse: if the browser stops hearing it for 3 s it shows "disconnected" and reconnects with growing pauses (1 s, 2 s, 4 s, capped at 4 s so recovery stays under 5 s). On reconnect the snapshot restores everything without a reload.

### R19. Frontend: static pages, vanilla JavaScript, vendored Chart.js
**Decision**: Four HTML pages served by FastAPI as static files. JavaScript is small ES modules that only fetch, listen and draw; no business rules. Chart.js is stored in `frontend/vendor/` (no CDN) so the dashboard works offline. No npm, no bundler. The Overview keeps the layout the later features fill: labelled placeholders for the camera feed, recent logs and "Pi health: not connected".

**Testing approach**: The API and WebSocket are tested automatically; the pages are checked by the manual checklist in [quickstart.md](quickstart.md). Keeping the JavaScript thin is what makes this acceptable.

### R20. Configuration
**Decision**: `config/default.toml` holds every tunable value with its unit in the key name (for example `watchdog_ms`). `pydantic-settings` loads it and allows environment overrides with the prefix `PIGEON_`. Settings are validated at start; the app refuses to start on nonsense (for example a negative timeout).

### R21. Developer tools and exposure
**Decision**: The server binds to 127.0.0.1 only. Developer endpoints (`/api/dev/...`) exist only when `PIGEON_DEV_TOOLS=1`, which is the default in this feature because it is the only way to drive the simulator by hand. They can set the key input, send protocol commands (including a fire request) through the normal client, trigger an engagement, corrupt the zone store, inject a sensor or storage fault, and start or stop the simulator. They cannot call into the arbitration code or switch the effector.

**In plain words**: The dev panel is a remote control for the simulated switches on the box. It can press the buttons a person could press (key, commands) but it cannot reach inside and force a "granted".

---

## Part E. Testing

### R22. Test strategy
- **Tests first** for every task (constitution I, FR-045).
- **Unit**: framing (including `hypothesis` property tests: any single-bit flip is rejected; encode then decode returns the original), geometry, arbitration (one test per denial reason where only that condition fails), zones, metrics, collector, store.
- **Contract**: a vector runner loads every file in `protocol/vectors/` and runs each against the simulator through an adapter (R23).
- **Integration**: FastAPI test client and WebSocket with the simulator, fake sensors and a fake clock where possible.
- **Architecture**: (a) parse every module outside `deterrent/simulator/` and fail if it imports from `deterrent.simulator` other than the allowed files (`composition.py`, `link/inprocess.py`); (b) fail if any module outside the simulator contains the "granted" reply constructor or sets the effector; (c) check that `SimulatorControl` has only the allowed methods. This is the automated check required by SC-019, and the review is the second part.
- **Time**: all simulator timing tests use `FakeClock`, so "cut within 100 ms" is exact and the 1-second watchdog test takes microseconds.

### R23. Scenario vectors and the adapter
**Decision**: Vectors are JSON, validated by `protocol/vector-schema.json`. A vector has starting conditions, timed steps and expected results; messages are written as readable fields plus, where the test is about framing, as raw hex. A `DeviceAdapter` interface (`apply_initial`, `send`, `advance`, `set_key`, `get_status`) is the only thing a firmware test harness has to implement. Format in [contracts/scenario-vectors.md](contracts/scenario-vectors.md).

**In plain words**: The vectors are a rule book of "given this starting situation and these messages, the device must answer exactly this". The simulator is one player of the rule book, the real ESP32 will be another, and neither can bend the rules because the rules are in a separate file.

### R24. Continuous integration
**Decision**: `.github/workflows/ci.yml` runs `ruff check`, `ruff format --check` and `pytest` on every push (constitution). It runs on Linux; sensor tests use mocks so no GPU is needed.

### R25. Dependency versions
**Decision**: Exact versions are resolved by `uv add` when the skeleton task runs, and recorded in `uv.lock`, with the date and the reason recorded in the decision log. This plan does not guess version numbers from memory. Chart.js is pinned by file name and checksum when vendored.

---

## Items for owner confirmation

All items below were **confirmed by the owner on 2026-10-31**. The decision log (`docs/decisions.md`) is updated by the owner by hand; this plan does not edit it.

1. **Zone limits** (open decision 5): 8 zones x 16 points. CONFIRMED 2026-10-31.
2. **Protocol format** (open decision 7, in part): binary frames, COBS, CRC-16, centidegrees (R4). CONFIRMED 2026-10-31.
3. **Denial priority order** (R7): now a 14-rung ladder (PERSON_CLEAR_DELAY added after PERSON_NOT_CLEAR). CONFIRMED 2026-10-31.
4. **Key off goes to SAFE_ERROR from any state** (R8). CONFIRMED 2026-10-31. The earlier "auto-disarm" wording was wrong and was corrected. Trigger is the on-to-off change; recovery is automatic once the key is on; the device lands in DISARMED, never re-armed, no clear command; arbitration reads the live key level; debouncing is deferred to feature 006.
5. **Fire direction** (R7): the device judges by its own measured angles, not the direction inside the request. CONFIRMED 2026-10-31.
6. **ZONE_SETUP entry and exits** (R9): every exit is in the "Leaving ZONE_SETUP" table (protocol.md 5.5); `DISARM` is the abort; no new message types; a rejected `ZONE_BEGIN` has no effect. CONFIRMED 2026-10-31.
7. **Added timing rules** (R12): zone upload timeout 5 s, simulated homing 3 s, acknowledgement timeout 500 ms. CONFIRMED 2026-10-31.
8. **Percentile method** (R15): nearest-rank. CONFIRMED 2026-10-31.
9. **Test-only state injection** (Complexity Tracking in plan.md): needed for vectors; must be compiled out of production firmware. CONFIRMED 2026-10-31.
10. **Decision log**: the owner records the confirmed decisions in `docs/decisions.md` by hand. Not edited by this plan. CONFIRMED 2026-10-31.
11. **Other scope-document triggers** ("person detected", "limit or zone violation"): RESOLVED 2026-10-31 by items 12 and 13 below. Scope document v1.5 no longer lists them as SAFE/ERROR triggers.
12. **Person detected is a fire inhibit, not a fault** (R7, protocol.md 5.3). CONFIRMED 2026-10-31. Fire is denied and a running effector is cut, with no state change and no latched fault. After the person is gone, fire stays denied until person-clear has been reported continuously for `person_reengage_delay_ms` (default 5000, **provisional**), measured on the device's own clock. Boundary: denied at delay minus 1 ms, allowed at exactly the delay. A cut shot is never resumed; a new request is needed. This is a different timer from the status hold-off.
13. **Zone and limit violations are denials, not faults** (R7). CONFIRMED 2026-10-31. `IN_ZONE_MARGIN` and `ELEVATION_LIMIT` are normal replies naming one reason; mid-shot they cut the effector with the same reason; no fault is latched in this feature except by heartbeat loss and key off.
14. **Zone-store validity is derived** (R11). CONFIRMED 2026-10-31. Recomputed from the stored bytes each time, no fault bit, empty store valid. Amended 2026-10-31: an unreadable store is invalid and becomes valid again by itself when readable with matching checksums; corrupted bytes stay invalid until `ZONE_CLEAR` leaves a valid store.
15. **Reserved fault bits** (protocol.md 4.4). CONFIRMED 2026-10-31. `HOMING_FAILED` and `INTERNAL` are reserved for feature 003 and must stay clear in this feature.
16. **Permanent reason codes** (R7, protocol.md 6.1). CONFIRMED 2026-10-31. Wire code and priority are separate columns; codes 1 to 13 unchanged, `PERSON_CLEAR_DELAY` is 14; codes are never reused or reassigned; adding or reordering priorities never changes a code; a changed meaning needs a protocol version change; a lock test guards the table.
17. **Hold-off and watchdog defaults stay equal at 1000 ms** (protocol.md 5.3). CONFIRMED 2026-10-31, both **provisional**, to be re-tuned in feature 004 with measured Wi-Fi jitter. With equal values a silent laptop shows `HEARTBEAT_LOST` first; a stale-status `PERSON_NOT_CLEAR` is reachable only with hold-off below the watchdog.

## Open questions

1. A re-engage delay of 0 disables the delay. Whether production may use 0 is decided in feature 006.
2. The protocol has no version field on the wire in this draft. Whether to add one is decided before the firmware (feature 003).
3. The spec (Assumptions) says provisional timings are re-baselined in features 003 and 006; this plan adds feature 004 for the hold-off and watchdog. The spec was not changed.
