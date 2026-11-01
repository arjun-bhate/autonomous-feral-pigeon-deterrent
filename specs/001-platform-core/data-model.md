# Data Model: Platform Core (No Hardware)

Two separate worlds, on purpose:

- **Laptop world** (observability): health readings, logs, traces, spans, metrics. Stored in `data/observability.sqlite3`.
- **Device world** (simulated ESP32): state, angles, flags, faults, zones. Zones persist in `data/device/zone_store.sqlite3`. The laptop never reads this file; it learns about zones only by asking the device (`ZONE_LIST`).

Conventions: times stored as UTC epoch **microseconds** (integer); durations in **microseconds**; angles in **centidegrees** (0.01 degree, integer); sizes and limits come from `config/default.toml`.

---

## 1. Laptop world

### 1.1 HealthReading (in memory; latest values pushed live; gauges persisted every 10 s)

| Field | Type | Rules |
|-------|------|-------|
| name | enum | `cpu_load`, `gpu_load`, `ram_used`, `cpu_temp`, `gpu_temp` (more temperatures can be added as new names) |
| value | number or null | null when state is unavailable |
| unit | string | `percent`, `celsius` |
| state | enum | `available` or `unavailable` |
| read_at | epoch µs | when the read finished (or timed out) |

Rule: a failed or timed-out read always produces `unavailable` with value null. The previous value is never reused (FR-002).

### 1.2 LogEntry (table `logs`)

| Column | Type | Rules |
|--------|------|-------|
| id | INTEGER PK | auto-increment, gives stable ordering and paging |
| ts_us | INTEGER | UTC epoch µs |
| component | TEXT | e.g. `api`, `health`, `simulator`, `link`, `engagement`, `store`; required |
| severity | TEXT | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| message | TEXT | required; must not contain secrets or image data (filtered) |
| trace_id | TEXT null | 16 hex characters when the entry belongs to an engagement |
| span_id | TEXT null | 8 hex characters |
| fields_json | TEXT null | extra structured fields |

Indexes: `(ts_us)`, `(component, ts_us)`, `(severity, ts_us)`, `(trace_id, ts_us)`.
Filters combine with AND (FR-008). Results are ordered by `ts_us, id`.

### 1.3 Trace (table `traces`)

| Column | Type | Rules |
|--------|------|-------|
| trace_id | TEXT PK | 16 hex characters |
| started_us | INTEGER | start of the first span |
| ended_us | INTEGER null | end of the last span; null while running |
| outcome | TEXT | `GRANTED`, `DENIED`, `REJECTED`, `TIMEOUT`, `NO_FIRE_REQUESTED`, `RUNNING`. `REJECTED` = the device replied REJECTED (the message itself was refused, for example `FIELD_RANGE` for `duration_ms` 499, or `NOT_HOMED` in BOOT); it was never arbitrated, so it is not a denial |
| deny_reason | TEXT null | set when the device denied |
| simulated | INTEGER | 1 when any step timing is made up (always 1 in this feature) |

Derived: `total_us = ended_us - started_us`.

### 1.4 Span (table `spans`)

| Column | Type | Rules |
|--------|------|-------|
| span_id | TEXT PK | 8 hex characters |
| trace_id | TEXT | FK to traces |
| position | INTEGER | 1 to 8, fixed by name (below) |
| name | TEXT | `capture`, `encode`, `network`, `inference`, `decision`, `command`, `esp32_ack`, `effector_action` |
| started_us | INTEGER | wall clock, for display |
| duration_us | INTEGER null | monotonic measurement; null if the step never finished |
| status | TEXT | `OK`, `DENIED`, `REJECTED`, `TIMEOUT`, `NOT_RUN` |
| simulated | INTEGER | 1 for made-up timings (capture, encode, network, inference) |
| attrs_json | TEXT null | e.g. deny reason on `esp32_ack`, "no action" on `effector_action` |

Rules (FR-009, SC-014):
- A completed trace always has all eight rows, in position order. A step that did not happen is present with status `TIMEOUT` or `NOT_RUN`, never omitted.
- Spans are sequential: `started_us[n+1] >= started_us[n] + duration_us[n]`. The total equals the last end minus the first start.
- Denied fire request: `esp32_ack` status `DENIED` with the reason; `effector_action` status `NOT_RUN` with attrs "no action".
- Rejected fire request (device reply REJECTED): `esp32_ack` status `REJECTED` with the reject code name in attrs (`reject_code`, for example `FIELD_RANGE`); `effector_action` status `NOT_RUN` with attrs "no action". The trace outcome is `REJECTED` and `deny_reason` stays null.

### 1.5 MetricEvent (table `metric_events`)

| Column | Type | Rules |
|--------|------|-------|
| id | INTEGER PK | |
| ts_us | INTEGER | |
| name | TEXT | see list below |
| kind | TEXT | `counter` (value = increment, usually 1), `gauge` (value = reading), `sample` (one latency or drift sample) |
| labels_json | TEXT null | e.g. `{"reason":"KEY_OFF"}` |
| value | REAL | |
| unit | TEXT | required |

Index: `(name, ts_us)`.

Metrics that must exist (FR-011):

| Name | Kind | Unit | Labels |
|------|------|------|--------|
| laptop_cpu_load, laptop_gpu_load, laptop_ram_used | gauge | percent | |
| laptop_cpu_temp, laptop_gpu_temp | gauge | celsius | |
| e2e_latency_simulated | sample | milliseconds | |
| fire_requests_granted | counter | count | |
| fire_requests_denied | counter | count | `reason` |
| interlock_events | counter | count | `cause` (e.g. HEARTBEAT_LOST, PERSON_NOT_CLEAR, KEY_OFF, IN_ZONE_MARGIN) |
| protocol_messages_rejected | counter | count | `cause` |
| homing_drift_simulated | sample | degrees | |

Summaries computed on read: counter total, latest gauge, and for samples: count, p50, p95 using **nearest-rank** (sort, take position ceil(p/100 x n)). No samples gives an empty state, never zeros (US5 scenario 4).

### 1.6 SystemStatus (in memory, pushed live)

`storage_warning` (bool + message + since), `simulator_running` (bool), `dashboard_link` (client side only: connected/disconnected).

---

## 2. Device world (simulated ESP32)

### 2.1 DeviceState (in memory)

| Field | Type | Notes |
|-------|------|-------|
| state | enum (12) | BOOT, HOMING, DISARMED, SCANNING, CANDIDATE, SLEW, CONFIRM, TRACK, DETER, COOLDOWN, ZONE_SETUP, SAFE_ERROR. This feature enters only BOOT, HOMING, DISARMED, SCANNING, DETER, COOLDOWN, ZONE_SETUP, SAFE_ERROR. |
| pan_cdeg, tilt_cdeg | int16 | 0 to 24000 and 0 to 8000 (soft limits; relative to homed zero) |
| homed | bool | false until homing completes |
| key_on | bool | test input (hardware key in the real device) |
| armed | bool | false on every boot |
| effector_on | bool | simulated |
| faults | bitmask (16 bits) | bit 0 `HEARTBEAT_LOST`, bit 1 unused (zone-store validity is derived, not a fault), bit 2 `HOMING_FAILED` and bit 3 `INTERNAL` (both reserved for feature 003, must stay clear here), bit 4 `KEY_OFF`. Person detection, zone-margin and elevation conditions never set a bit. |
| last_heartbeat_rx | device ms | when the last accepted heartbeat arrived (device clock) |
| person_clear | bool | `person_clear` field of the latest accepted heartbeat (1 = clear) |
| clear_since | device ms or none | start of the current clear period: receive time of the first accepted clear heartbeat after a moment of "person present". None while a person is present. Used for the re-engage delay (protocol.md 5.3). Not a fault and not a state. |
| on_time_shot_ms | int | on-time so far of the current shot; the shot ends when it reaches the request's `duration_ms` (500 to `per_shot_ms`) |
| cooldown_started_ms | device ms or none | when the last shot ended (any cause except one that enters SAFE_ERROR). The cooldown is running while `now - cooldown_started_ms < cooldown_ms`. Independent of the state (a DISARM cut starts it while the state is DISARMED). Cleared only by boot. |
| last_seq, reply_cache | u16, 8 entries | duplicate and stale detection |
| upload_session | UploadSession or none | see 2.3 |

State transitions (this feature):

```text
BOOT --> HOMING --(complete, simulated drift recorded)--> DISARMED
DISARMED --(ARM: homed, key on, no fault)--> SCANNING
any state except BOOT/HOMING/SAFE_ERROR --(DISARM)--> DISARMED      (DISARM is always accepted and replies OK; it cuts the effector and discards any upload; in BOOT, HOMING and SAFE_ERROR it changes nothing else: no state change, homing is not abandoned)
SCANNING --(granted fire)--> DETER
DETER --(max duration reached, FIRE_STOP, or any interlock lapses except heartbeat loss and key off)--> COOLDOWN   (effector off, cooldown starts; earlier drafts returned to SCANNING)
DETER --(DISARM)--> DISARMED   (effector cut, cooldown starts and keeps running)
COOLDOWN --(cooldown_ms elapsed on the device clock)--> SCANNING
any state --(FIRE_STOP)--> unchanged   (only a running shot is ended: DETER --> COOLDOWN; otherwise reply OK and nothing changes)
any state except SAFE_ERROR --(key input on -> off)--> SAFE_ERROR   (effector cut, upload discarded, armed=false, fault KEY_OFF, interlock event)
any state except BOOT --(heartbeat lost > watchdog, after the first accepted heartbeat)--> SAFE_ERROR   (motion stopped, effector off, upload discarded, fault HEARTBEAT_LOST; in HOMING, homing is abandoned and homed stays false; already in SAFE_ERROR it only adds the fault)
SAFE_ERROR --(every latched fault's clearing condition true: heartbeat accepted for HEARTBEAT_LOST, key input on for KEY_OFF)--> DISARMED   (never re-arms; no clear command; homed stays false if homing was abandoned, so a HOME is needed before ARM)
DISARMED --(ZONE_BEGIN accepted)--> ZONE_SETUP
ZONE_SETUP --(ZONE_COMMIT accepted: zone stored atomically)--> DISARMED
ZONE_SETUP --(ZONE_COMMIT rejected: nothing stored)--> DISARMED
ZONE_SETUP --(5 s idle timeout: upload discarded)--> DISARMED
ZONE_SETUP --(ZONE_BEGIN accepted: old upload discarded, fresh start)--> ZONE_SETUP
ZONE_SETUP, DISARMED --(ZONE_BEGIN rejected)--> unchanged (not entered / stays)
```

The ZONE_SETUP rows are exactly the table "Leaving ZONE_SETUP" in [protocol/PROTOCOL.md](../../protocol/PROTOCOL.md) section 5.5; the SAFE_ERROR rows are its section 5.6. If the two ever differ, the protocol document wins and this diagram must be corrected.

Key-off trigger: the **change** from on to off, in every state except SAFE_ERROR. A device that boots with the key already off is not faulted; it simply cannot arm.

Refusals: while BOOT or HOMING, MOVE, JOG and ARM are refused with a specific reject code (`NOT_HOMED` in BOOT, `BUSY_HOMING` in HOMING); no angle changes. FIRE_REQUEST in BOOT is rejected `NOT_HOMED` without arbitration; in HOMING it is denied `NOT_HOMED` by arbitration.

### 2.2 Zone and ZoneStore

**Zone**

| Field | Type | Rules |
|-------|------|-------|
| zone_id | u8, 1 to 255 | supplied by the uploader; duplicates rejected |
| points | list of (pan_cdeg, tilt_cdeg) | 3 to 16 points (configurable); polygon closes from last point to first |
| crc32 | u32 | CRC-32 (IEEE) over: zone_id (1 byte), count (1 byte), then each point as pan i16 LE, tilt i16 LE |

Validation at commit (in this order, first failure is returned): upload exists, point count matches what arrived, CRC-32 matches, at least 3 points, no degenerate or duplicate consecutive points, no self-crossing edges, zone id not already stored, zone count below the maximum, storage write succeeds. A failure never changes stored zones.

**ZoneStore**

| Field | Rules |
|-------|-------|
| zones | at most 8 (configurable), each independent, deleted whole, never edited |
| valid | **Derived, never stored**: recomputed each time it is needed from the persisted zone bytes. Valid only if every stored zone's recomputed CRC-32 equals its stored CRC-32. An empty store is valid. An unreadable store counts as invalid. |
| Persistence | `ZoneStorage` interface; SQLite implementation: table `zone(zone_id INTEGER PK, crc32 INTEGER, point_count INTEGER, points BLOB)`, writes in one transaction |

Behaviour of the derived value (the same rules as protocol.md section 5.5):
- Empty store: valid. Unreadable store: invalid.
- If a store that was unreadable becomes readable again and every checksum matches, it is valid again **without** `ZONE_CLEAR`.
- Corrupted bytes stay invalid until `ZONE_CLEAR` leaves a valid store (accepted while invalid; the stored bytes do not repair themselves).
- While invalid, every fire request is denied `ZONE_STORE_INVALID` (priority 10, wire code 9), which no fault bit can hide, and a running effector is cut. No fault bit is used.

### 2.3 UploadSession (in memory)

`zone_id`, `declared_count`, `points_received` (indexed, must arrive in order starting at 0), `last_activity_ms`. This is the **staging buffer**: points wait here and are not part of the zone store. Created by an accepted `ZONE_BEGIN`. It ends on every exit in the "Leaving ZONE_SETUP" table: consumed by an accepted commit (the only way it becomes a zone), and discarded on a rejected commit, the 5 s idle timeout, `DISARM`, key off, heartbeat loss, or a new accepted `ZONE_BEGIN`. A rejected `ZONE_BEGIN` leaves it untouched. Never persisted and never used for a fire decision.

### 2.4 Fire attempt (in memory, for the reply and the events)

Fields in the request: `issued_at_ms`, `ttl_ms`, `intended_pan_cdeg`, `intended_tilt_cdeg`, `duration_ms` (required; the maximum on-time, 500 to `per_shot_ms`). Result: `GRANTED` (shot started) or `DENIED` with one reason from the priority ladder (research R7). The reply carries the reason's permanent code, not its priority position (protocol.md section 6). The decision inputs are the device's own state and angles.

---

## 3. Messages

Authoritative layouts, byte by byte, are in [protocol/PROTOCOL.md](../../protocol/PROTOCOL.md). Entities:

- **Command message**: `type`, `seq`, payload, `crc16`.
- **Reply**: the request type with the high bit set (`type | 0x80`), the request's `seq`, a result (`OK`, `REJECTED`, `GRANTED`, `DENIED`), a code, and optional data.
- **Device events (unsolicited)**: `STATUS` (state or angle change), `REJECT_NOTICE` (a frame was discarded and why), `EFFECTOR_EVENT` (on, normal end, cut with cause, stopped by host).
- **Heartbeat**: `person_clear`, `laptop_ts_ms`; reply carries `device_ms`.

---

## 4. Scenario vectors (file data)

Defined in [contracts/scenario-vectors.md](contracts/scenario-vectors.md): `id`, `covers` (the denial reason, zone rejection or timing rule it tests), `config` overrides, `initial` conditions, timed `steps`, `expect`.

---

## 5. Relationships (summary)

```text
Trace 1 ---- 8 Span                 (every completed trace has exactly eight)
Trace 1 ---- * LogEntry             (via trace_id)
Trace 1 ---- * MetricEvent          (latency sample recorded at the end of the trace)
Device  1 ---- 1 ZoneStore 1 ---- 0..8 Zone
Device  1 ---- 0..1 UploadSession
```
