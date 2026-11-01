# Tasks: Platform Core (No Hardware)

**Input**: `specs/001-platform-core/` (spec.md, plan.md, research.md, data-model.md, quickstart.md, contracts/protocol.md, contracts/scenario-vectors.md, contracts/dashboard-api.md), constitution v1.1.0, CLAUDE.md.

**Prerequisites**: All present. Stale-rule check done before writing (see "Pre-flight check" below).

**Tests**: REQUIRED for every task (FR-045, constitution I). Each task lists its tests first. Write the tests, watch them fail, then write the code. Nothing here writes code yet; this file is the plan for the work.

**Rules for every task** (from CLAUDE.md): one task per session, then stop and report in the CLAUDE.md format; no commit unless the owner asks; never delete files or run destructive commands without asking; explain the new code in plain language in the report so the owner can explain it in an interview.

## Format

`- [ ] T### [P?] [US#?] Action in path` followed by three lines:

- **Tests first**: the test file and the cases to write before any code.
- **Covers**: the FR and SC items the task helps satisfy.
- **Depends**: earlier tasks that must be finished.

`[P]` = can be done in parallel with other `[P]` tasks that have the same dependencies (different files). `[US#]` = the spec user story the task serves. Setup, Foundational and Polish tasks carry no story label.

## Pre-flight check (done before writing this file)

All plan documents (plan.md, research.md, data-model.md, quickstart.md, contracts/*) were checked against spec.md for stale rules.

- `RATE_LIMIT`, "per minute": every mention is an explicit retirement note (code 13 retired, never returned) or a vector proving no per-minute limit exists. None is active. OK.
- `duration 0 means hold`: every mention says the rule was removed; `duration_ms` below 500, including 0, is `FIELD_RANGE`. OK.
- Cooldown 5 s, `COOLDOWN_ACTIVE` as the last (14th) rung, person re-engage delay 5 s, `FIRE_STOP` accepted in every state, per-shot limit 5000 ms: plan documents agree with spec.md and FR-048 to FR-050. OK.
- **One stale item found, not a rule**: `quickstart.md` section 6 says "SC-001 to SC-019"; the spec now has SC-001 to SC-022. Task T075 records all 22 and tells the owner to fix the quickstart line. No task depends on the stale text. See the report.

## Ordering note (a deliberate departure from spec priority order)

The spec ranks the stories P1 (health) to P7 (protocol). You asked for software first, so the simulator's safety logic and the protocol come before the dashboard. Order here: Setup, protocol core, **US6 simulator**, **US7 protocol and vectors**, shared backend (observability, link, engagement, API), then the dashboard stories US1 to US5, then Polish. Every story is still independently testable at its own checkpoint.

## Path conventions

Paths are exactly those in plan.md "Source Code". Files plan.md or the contracts name are used verbatim: `protocol/PROTOCOL.md`, `protocol/vector-schema.json`, `protocol/reason-codes.json`, `protocol/vectors/{fire_arbitration,zones,framing_and_sequence,timing}.json`, `tests/contract/reason_codes_lock.json`, `config/default.toml`, `data/observability.sqlite3`, `data/device/zone_store.sqlite3`. Where the plan names no file (the vector runner and its adapter) the path is a stated assumption: `tests/contract/vector_runner.py`, `tests/contract/simulator_adapter.py`.

---

## Phase 1: Setup

- [X] T001 Create the uv project skeleton: `pyproject.toml`, `.python-version` (3.12), `.gitignore` additions (`data/`, `*.sqlite3`, `__pycache__`), empty package `src/deterrent/__init__.py` and the sub-package folders from plan.md, empty `tests/{unit,contract,integration,architecture}/`. Add dependencies with `uv add` (project-local only; list them in the report).
  - **Tests first**: `tests/unit/test_smoke.py`: `import deterrent` works; `git check-ignore data/x.sqlite3` is true (as a test using subprocess).
  - **Covers**: FR-043, FR-045
  - **Depends**: none

- [X] T002 Configure ruff and pytest in `pyproject.toml` and create `.github/workflows/ci.yml` (ruff check, ruff format --check, pytest on every push; Linux runner).
  - **Tests first**: `tests/unit/test_smoke.py` extended: pytest collects all four test folders; `uv run ruff check` and `uv run ruff format --check` exit 0 on the skeleton. (CI itself can only be seen once pushed; say so in the report.)
  - **Covers**: FR-043, FR-045
  - **Depends**: T001

- [X] T003 Create `config/default.toml` and `src/deterrent/config.py` (settings loader with environment override). Every tunable value from protocol.md section 8 with its unit in the name: `heartbeat_interval_ms` 200, `watchdog_ms` 1000, `person_clear_holdoff_ms` 1000, `person_reengage_delay_ms` 5000, `fire_ttl_ms` 500, `command_ttl_ms` 500, `per_shot_ms` 5000, `cooldown_ms` 5000, `zone_upload_timeout_ms` 5000, `homing_duration_ms` 3000, `aiming_error_cdeg` 20, `max_zones` 8, `max_points_per_zone` 16, `elevation_min_cdeg` 0, `elevation_max_cdeg` 8000. Fixed constants (not configurable): minimum `duration_ms` 500, soft limits pan 24000 and tilt 8000 centidegrees.
  - **Tests first**: `tests/unit/test_config.py`: every default equals the table above; an environment override changes one value; `per_shot_ms` below 500 is refused; negative timings are refused; the file contains no secrets.
  - **Covers**: FR-034, FR-036, FR-018
  - **Depends**: T001

- [X] T004 [P] Create `src/deterrent/clock.py`: `Clock` interface, `SystemClock`, `FakeClock` (manual advance), and a wrap-safe millisecond difference helper for the 32-bit `device_ms` (protocol.md section 1).
  - **Tests first**: `tests/unit/test_clock.py`: FakeClock advances only when told; wrap-safe difference is correct across the 2^32 wrap (hypothesis property); SystemClock is monotonic.
  - **Covers**: FR-044, FR-045, FR-043
  - **Depends**: T001

**Checkpoint**: `uv run pytest` and `uv run ruff check` pass on an empty feature.

---

## Phase 2: Foundational A (protocol core and geometry, pure code, no I/O)

- [X] T005 [P] Create `src/deterrent/protocol/crc.py`: CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no final XOR) and CRC-32 (IEEE, as zlib) for zone checksums.
  - **Tests first**: `tests/unit/test_crc.py`: CRC-16 of `"123456789"` is 0x29B1; CRC-32 of `"123456789"` is 0xCBF43926; CRC-32 of a zone (zone_id, count, points i16 LE) matches a hand-built byte string; a one-bit change always changes the CRC (hypothesis).
  - **Covers**: FR-016, FR-031, FR-038
  - **Depends**: T001

- [X] T006 Create `src/deterrent/protocol/framing.py`: COBS encode/decode, frame build (`type | seq | payload | crc16`, minimum 5, maximum 64 raw bytes, on the wire `COBS(frame) | 0x00`), and frame parse that reports the first failure in protocol.md section 2 order for steps 1 to 3 (`BAD_FRAMING`, `TRUNCATED`, `BAD_CRC`).
  - **Tests first**: `tests/unit/test_framing.py`: round trip for random payloads 0 to 59 bytes (hypothesis); no `0x00` inside an encoded body; oversize (more than 66 wire bytes) is `BAD_FRAMING`; 4-byte frame is `TRUNCATED`; flipped bit is `BAD_CRC`; receiver resynchronises at the next `0x00`; every corrupted, truncated or garbage frame is rejected and never returns a message.
  - **Covers**: FR-016, FR-038, SC-006
  - **Depends**: T005

- [X] T007 Create `src/deterrent/protocol/codes.py` and `protocol/reason-codes.json`: message types, reply results, reject codes (protocol.md section 7), state codes (0 to 11), fault bits, event codes, and the 14-rung denial ladder with priority and permanent code as separate data (HEARTBEAT_LOST 1, FAULT_ACTIVE 2, ZONE_SETUP 3, NOT_HOMED 4, KEY_OFF 5, NOT_ARMED 6, PERSON_NOT_CLEAR 7, PERSON_CLEAR_DELAY 14, REQUEST_EXPIRED 8, ZONE_STORE_INVALID 9, ELEVATION_LIMIT 10, IN_ZONE_MARGIN 11, ON_TIME_LIMIT 12, COOLDOWN_ACTIVE 15) plus the retired `RATE_LIMIT` 13 marked `retired`. The twelve state names, with `SAFE_ERROR` shown as `SAFE/ERROR`, come from this module.
  - **Tests first**: `tests/unit/test_codes.py`: names unique, codes unique and 1 to 255; `reason-codes.json` equals the table in `codes.py` (name, code, status); reordering priorities in a copy never changes a code; exactly 12 state names; `RATE_LIMIT` exists only as retired and no active ladder entry uses code 13; next unused code is 16.
  - **Covers**: FR-023, FR-005, FR-038
  - **Depends**: T001

- [X] T008 Create `src/deterrent/protocol/messages.py` part 1, host-to-device messages: encode/decode of HOME, MOVE, JOG, HEARTBEAT, ARM, DISARM, ZONE_BEGIN, ZONE_POINT, ZONE_COMMIT, ZONE_DELETE, ZONE_LIST, ZONE_CLEAR, FIRE_REQUEST, FIRE_STOP, STATUS_REQUEST with exact payload lengths (`BAD_LENGTH`) and field ranges (`FIELD_RANGE`): booleans 0/1; `ttl_ms` 1 to 5000; `duration_ms` below 500 including 0 is out of range (no "hold" value); FIRE_STOP has no payload.
  - **Tests first**: `tests/unit/test_messages_host.py`: round trip each type (hypothesis); wrong length for every type gives `BAD_LENGTH`; boolean 2 gives `FIELD_RANGE`; `duration_ms` 499 and 0 give `FIELD_RANGE`, 500 and 750 pass; FIRE_STOP with one byte gives `BAD_LENGTH`; unknown type gives `UNKNOWN_TYPE`.
  - **Covers**: FR-015, FR-016, FR-038, FR-022
  - **Depends**: T006, T007

- [X] T009 Extend `src/deterrent/protocol/messages.py` part 2, device-to-host: reply layout `type|0x80, seq, result, code, data`, per-message reply data (MOVE/JOG angles, HEARTBEAT `device_ms` and state, ZONE_LIST entries, FIRE_REQUEST GRANTED `granted_on_ms`, DENIED `fault_mask`), the 13-byte status block, generic reject `0xFE`, and unsolicited events STATUS `0xC0`, REJECT_NOTICE `0xC1`, EFFECTOR_EVENT `0xC2`.
  - **Tests first**: `tests/unit/test_messages_device.py`: round trip each reply and event; status block is exactly 13 bytes with the documented field order; DENIED carries the permanent code, not the priority; EFFECTOR_EVENT cause 13 is refused at encode time; ZONE_LIST for 8 zones fits the 59-byte payload limit.
  - **Covers**: FR-015, FR-028, FR-038, FR-023
  - **Depends**: T008

- [X] T010 [P] Create `src/deterrent/geometry/polygon.py` part 1: integer self-crossing detection (including touching and collinear overlap), duplicate consecutive points, and zero-area test.
  - **Tests first**: `tests/unit/test_polygon_crossing.py`: bow-tie crosses; square and L-shape do not; a vertex touching another edge counts as crossing; repeated consecutive point is degenerate; collinear points have zero area; random simple polygons built by convex hull never report crossing, and a random edge swap on them is caught (hypothesis).
  - **Covers**: FR-032, FR-046
  - **Depends**: T001

- [X] T011 Extend `src/deterrent/geometry/polygon.py` part 2: integer point-in-polygon (even-odd) and the margin test: inside, or squared distance to any edge at most `zone_margin_cdeg` squared, with `zone_margin_cdeg = 50 + aiming_error_cdeg` (default 70).
  - **Tests first**: `tests/unit/test_polygon_margin.py`: inside is in-margin; outside by margin plus 1 is not; exactly at margin distance is in-margin; on a vertex and on an edge; margin follows the aiming error setting (0 and 20); result is identical for translated and rotated-by-90 polygons (property test); no floating point is used.
  - **Covers**: FR-034, FR-046
  - **Depends**: T010, T003

**Checkpoint**: protocol core and geometry fully unit-tested with no simulator yet.

---

## Phase 3: User Story 6 - Simulated ESP32 with the full command set (Priority: P6, built first)

**Goal**: A pure, firmware-style simulator that validates every message and is the only place a fire is granted.

**Independent test**: Drive the simulator with test messages and a FakeClock only; no dashboard, no database other than the device's own zone file.

- [X] T012 [P] [US6] Create `src/deterrent/simulator/events.py`: `DeviceEventSink` interface (state/angle changes, rejects with cause, effector on/normal end/cut with cause/stopped by host, interlock event, homing drift, fire granted/denied with reason) and a `RecordingSink` for tests. No imports from `observability`, `api` or `composition`.
  - **Tests first**: `tests/unit/test_device_events.py`: RecordingSink keeps events in order with the device time; module imports no forbidden package (simple import check).
  - **Covers**: FR-044, FR-011, FR-021
  - **Depends**: T004, T007

- [ ] T013 [US6] Create `src/deterrent/simulator/zone_storage.py` part 1: `ZoneStorage` interface (read all zones as raw bytes with their stored CRC, atomic write of one zone, delete one, clear all) and a `MockZoneStorage` with switchable modes: ok, full, locked, unreadable, and a method that corrupts the bytes of one zone.
  - **Tests first**: `tests/unit/test_zone_storage_mock.py`: write then read; write in `full` or `locked` mode raises a storage error and leaves old data; `unreadable` raises on read; corrupting bytes does not change the stored CRC; clear empties the store.
  - **Covers**: FR-037, FR-044, FR-036
  - **Depends**: T012

- [ ] T014 [US6] Add the SQLite implementation to `src/deterrent/simulator/zone_storage.py`: file `data/device/zone_store.sqlite3`, table `zone(zone_id INTEGER PK, crc32 INTEGER, point_count INTEGER, points BLOB)`, each write in one transaction, kept in a different file and folder from `data/observability.sqlite3`.
  - **Tests first**: `tests/unit/test_zone_storage_sqlite.py` (uses a temp folder): data survives closing and reopening; a failed write (forced error mid-transaction) leaves the old zones intact; the module never opens `observability.sqlite3`; locked database file raises a storage error instead of hanging.
  - **Covers**: FR-036, FR-030, FR-037, FR-012
  - **Depends**: T013

- [ ] T015a [US6] Create `src/deterrent/simulator/device.py` part 1a, the intake steps of protocol.md section 2 and 3 other than the sequence rule: framing (step 1 `BAD_FRAMING`), length of at least 5 bytes (step 2 `TRUNCATED`), CRC (step 3 `BAD_CRC`), known type (step 5 `UNKNOWN_TYPE`), exact payload length (step 6 `BAD_LENGTH`) and field ranges (step 7 `FIELD_RANGE`). Step 4 (sequence) is T015b and step 8 (command allowed in the current state) is done by the later command tasks. Failures at steps 1 to 3 send no reply and are counted by cause. Failures at steps 5 to 7 get a normal `REJECTED` reply (the generic `0xFE` for an unknown type, as the protocol says). Until T015b exists, the tests for steps 5 to 7 call the intake with the sequence check stubbed to accept.
  - **Tests first**: `tests/unit/test_device_intake.py`: corrupted (`BAD_CRC`), truncated and bad-framing frames change no state and get no reply; an unknown type changes no state and gets the generic `0xFE`; each rejection at steps 1 to 3 is counted by cause; a frame of the wrong payload length for its type gives `BAD_LENGTH` and an out-of-range field gives `FIELD_RANGE`, each with a `REJECTED` reply and no state change (state, angles, armed flag, effector and stored zones identical before and after).
  - **Covers**: FR-016, FR-015, SC-006
  - **Depends**: T006, T008, T009, T012, T004

- [ ] T015b [US6] Add the sequence rule (step 4) to `device.py` part 1b: new sequence accepted, duplicate replays the cached reply with no second action, stale rejected `STALE_SEQ`; first message after boot accepted whatever its number. Reply cache of 8. A message rejected at steps 5 to 8 still consumes its sequence number (protocol.md section 3). Steps 1 to 3 emit a rate-limited `REJECT_NOTICE` (at most 5 per second).
  - **Tests first**: extend `tests/unit/test_device_intake.py`: the first message after boot is accepted whatever its number (try 0, 1, 40000 and 65535 on fresh devices); duplicate gets identical reply bytes and exactly one action (use a counting stub handler); older sequence rejected; sequence wrap at 65535 to 0 accepted; gap accepted; a message rejected at steps 5 to 8 still consumes its sequence number; REJECT_NOTICE capped at 5 per second on the FakeClock.
  - **Covers**: FR-029, FR-016, SC-007
  - **Depends**: T015a

- [ ] T016 [US6] Add boot, homing and status to `device.py`: BOOT then HOMING for `homing_duration_ms` (simulated drift value emitted as an event for metric `homing_drift_simulated`) then DISARMED with `armed` false and `homed` true; STATUS_REQUEST and the STATUS event on every change and at least once per second; state is always one of the 12 named states.
  - **Tests first**: `tests/unit/test_device_boot.py`: boot sequence BOOT, HOMING, DISARMED at the right FakeClock times; never armed on boot; exactly one drift event; status block fields correct in every state; STATUS event sent on change and at 1 s; no unnamed state value can be produced.
  - **Covers**: FR-017, FR-028, FR-005, FR-011
  - **Depends**: T015b

- [ ] T017 [US6] Add HOME, MOVE and JOG to `device.py`: refused with `BUSY_HOMING` or `NOT_HOMED` while BOOT or HOMING (no angle changes); moves applied instantly; any target outside pan 0 to 24000 or tilt 0 to 8000 refused `OUT_OF_SOFT_LIMIT` (never clamped); `HOME` allowed only in DISARMED.
  - **Tests first**: `tests/unit/test_device_motion.py`: MOVE, JOG, ARM-adjacent commands during HOMING are refused with the specific code and angles unchanged; after homing MOVE to (6000, 2000) is reported back; MOVE and JOG at and 1 centidegree beyond each limit; JOG that would leave the limit is refused whole; HOME in SCANNING is `WRONG_STATE`.
  - **Covers**: FR-017, FR-018, FR-015
  - **Depends**: T016

- [ ] T018 [US6] Create `src/deterrent/simulator/control.py` with `SimulatorControl.set_key` and add key input, ARM and DISARM to `device.py`: ARM checks in order `BUSY_HOMING`, `NOT_HOMED`, `KEY_OFF`, `FAULT_ACTIVE`, `WRONG_STATE`; ARM when already armed is an idempotent OK; DISARM is always accepted (reply OK in every state), cuts the effector if on, discards any upload, moves every state except BOOT, HOMING and SAFE_ERROR to DISARMED; the key level is reported in status.
  - **Tests first**: `tests/unit/test_device_arm.py`: arm with key off refused `KEY_OFF`; arm before homing refused with the homing codes; arm with key on succeeds and state is SCANNING; DISARM replies OK in all 12 state situations reachable now; DISARM during HOMING does not abandon homing (state HOMING, ends DISARMED, `homed` true); key level reported.
  - **Covers**: FR-019, FR-020, FR-017, FR-028
  - **Depends**: T017

- [ ] T019a [US6] Add heartbeat handling, the watchdog and SAFE_ERROR entry to `device.py`: HEARTBEAT carries `person_clear` and `laptop_ts_ms`; reply carries `device_ms` and state; the watchdog starts at the first accepted heartbeat and, after more than `watchdog_ms` of silence, stops motion, switches the effector off, latches `HEARTBEAT_LOST`, discards any upload, sets armed false and enters SAFE_ERROR; recovery from `HEARTBEAT_LOST` by a new accepted heartbeat. A stale or duplicate heartbeat never refreshes the heartbeat timer (protocol.md section 3).
  - **Tests first**: `tests/unit/test_device_watchdog.py`: silence of exactly `watchdog_ms` is alive, one ms more is SAFE_ERROR; motion refused in SAFE_ERROR; heartbeat before the first accepted heartbeat does not start the watchdog; a new accepted heartbeat clears a `HEARTBEAT_LOST` fault when it is the only latched fault; the HEARTBEAT reply carries `device_ms` (the FakeClock time) and the current state; `armed` is false in the status after entering SAFE_ERROR from an armed state; a stale heartbeat (older sequence) and a duplicate heartbeat (same sequence, cached reply) do not refresh the timer, so the device still enters SAFE_ERROR at the original deadline.
  - **Note**: the effector being switched off and an upload being discarded on watchdog entry are tested later, in T026 (`test_device_interlocks.py`, heartbeat-lost row) and T023 (`test_zones_store.py`, exit row 6), because the effector and zones do not exist yet at T019a.
  - **Covers**: FR-027, FR-024, FR-028, FR-029
  - **Depends**: T018

- [ ] T019b [US6] Add the key-off rule and fault recovery to `device.py`: key on-to-off in any state except SAFE_ERROR does the same as heartbeat loss with fault `KEY_OFF` and an interlock event; leave SAFE_ERROR only when every latched fault's clearing condition is true (new accepted heartbeat for `HEARTBEAT_LOST`, key on for `KEY_OFF`), arriving in DISARMED, never re-armed; reserved fault bits 2 and 3 are never set.
  - **Tests first**: extend `tests/unit/test_device_watchdog.py`: heartbeat alone does not clear a key fault and key alone does not clear a heartbeat fault; both faults need both conditions; recovery lands in DISARMED with armed false and needs an explicit ARM; key off at boot is not an event; DISARM in SAFE_ERROR replies OK and changes nothing; `fault_mask` bits 2 and 3 stay clear through the whole test.
  - **Covers**: FR-020, FR-027, FR-028
  - **Depends**: T019a

- [ ] T020 [US6] Add the two person timers to `device.py` (device clock only; the receive time of the accepted heartbeat, never `laptop_ts_ms`): status fresh while `now - last_rx <= person_clear_holdoff_ms`; person present when no heartbeat yet, latest report is not-clear, or status stale (fail closed); clear period starts at the first accepted clear heartbeat after a moment of person present and restarts on a person report, stale status, boot, heartbeat loss and SAFE_ERROR recovery; delay elapsed when `now - clear_since >= person_reengage_delay_ms` (0 disables). Expose as a pure function used by arbitration.
  - **Tests first**: `tests/unit/test_person_timers.py`: at hold-off exactly fresh, +1 ms stale; delay-1 ms not elapsed, exactly delay elapsed; person reappearing restarts from zero; a stale then clear heartbeat starts a new period at its own receive time (earlier clear time does not count); stale or duplicate heartbeat starts nothing; delay 0 never reports a delay; `laptop_ts_ms` with a wild value changes nothing.
  - **Covers**: FR-024, FR-048, SC-020
  - **Depends**: T019b

- [ ] T021 [US6] Create `src/deterrent/simulator/zones.py` part 1, the upload session: ZONE_BEGIN (homed; DISARMED or ZONE_SETUP; `point_count` 3 to `max_points_per_zone`; store not full) enters ZONE_SETUP and discards any earlier partial upload (exit 7); a rejected BEGIN changes nothing, including an upload in progress (exit 8); ZONE_POINT must arrive in order from index 0 (`ZONE_BAD_INDEX` keeps the upload); only BEGIN, POINT and COMMIT reset the idle timer; after `zone_upload_timeout_ms` the upload is discarded and the state returns to DISARMED (exit 3). The staging buffer is never persisted.
  - **Tests first**: `tests/unit/test_zones_upload.py`: BEGIN from DISARMED enters ZONE_SETUP; BEGIN while SCANNING is `WRONG_STATE`; 2 points and 17 points rejected with the specific codes and state unchanged; second BEGIN replaces the first, no mixed zone; bad index keeps the upload; timeout at exactly the limit minus 1 still ZONE_SETUP and at the limit DISARMED (FakeClock); heartbeats, LIST, MOVE and stale messages do not reset the timer; a point after the timeout gets `ZONE_NO_UPLOAD`.
  - **Covers**: FR-031, FR-015, FR-032
  - **Depends**: T013, T018, T019b

- [ ] T022 [US6] Add ZONE_COMMIT to `zones.py` and `device.py` with the nine ordered checks (upload exists, all points arrived, CRC-32, at least 3 points, no repeated consecutive points and non-zero area, no self-crossing, id not stored, count below `max_zones`, storage write succeeds). The first failure is returned, nothing is stored on any failure, an accepted commit stores the whole zone in one transaction, and every commit outcome returns the state to DISARMED (exits 1 and 2).
  - **Tests first**: `tests/unit/test_zones_commit.py`: valid commit stored and listed; one test per rejection code (`ZONE_NO_UPLOAD`, `ZONE_INCOMPLETE`, `ZONE_BAD_CHECKSUM`, `ZONE_TOO_FEW_POINTS`, `ZONE_DEGENERATE`, `ZONE_SELF_CROSSING`, `ZONE_DUPLICATE_ID`, `ZONE_STORE_FULL`, `ZONE_STORAGE_ERROR`) with the previous zones byte-for-byte unchanged and state DISARMED; when two checks fail only the first is reported; storage `full` and `locked` modes leave the old zones intact.
  - **Covers**: FR-031, FR-032, FR-037, FR-033, SC-011
  - **Depends**: T021, T011, T010, T005, T014

- [ ] T023 [US6] Add ZONE_DELETE, ZONE_LIST, ZONE_CLEAR, the derived zone-store validity, and the full "Leaving ZONE_SETUP" table to `device.py`: delete and clear act on whole zones (`ZONE_UNKNOWN_ID` for an unknown id); validity is recomputed from persisted bytes on each use (empty store valid; unreadable invalid; corrupted bytes invalid until ZONE_CLEAR leaves a valid store; unreadable-then-readable with matching CRCs valid again without ZONE_CLEAR); exits 4, 5 and 6 (DISARM, key off, heartbeat lost) discard the upload; ZONE_LIST works in any state except BOOT; zones are stored only in the device and never edited.
  - **Tests first**: `tests/unit/test_zones_store.py`: delete known/unknown id; clear in DISARMED and ZONE_SETUP; LIST reply matches stored zones and CRCs; corrupt a zone via the mock and see validity false, still false after 10 s, true after clear; unreadable then readable gives valid with no clear; unreadable then corrupt stays invalid; one test per exit row 1 to 8 asserting new state, stored zones and upload; the non-exit list (ARM and HOME give `WRONG_STATE`, MOVE and LIST work, state unchanged).
  - **Covers**: FR-033, FR-035, FR-036, FR-030, FR-031, SC-011, SC-012
  - **Depends**: T022

- [ ] T024 [P] [US6] Create `src/deterrent/simulator/arbitration.py`: the single pure function that decides a fire request from a read-only snapshot of the device (the only grant path). It evaluates the 14 rungs in priority order and returns `GRANTED` or exactly one reason: HEARTBEAT_LOST, FAULT_ACTIVE (any fault bit other than HEARTBEAT_LOST and KEY_OFF, mask in the data), ZONE_SETUP, NOT_HOMED, KEY_OFF (live level), NOT_ARMED, PERSON_NOT_CLEAR, PERSON_CLEAR_DELAY, REQUEST_EXPIRED (`now > issued_at_ms + ttl_ms`), ZONE_STORE_INVALID, ELEVATION_LIMIT, IN_ZONE_MARGIN, ON_TIME_LIMIT (`duration_ms` above `per_shot_ms`), COOLDOWN_ACTIVE (`now - cooldown_started_ms < cooldown_ms`). Judges by the device's own measured angles.
  - **Tests first**: `tests/unit/test_arbitration.py` (this exact file is named in quickstart.md): a baseline snapshot where everything passes is GRANTED; **one test per denial reason in which only that condition fails** and the reply names it; REQUEST_EXPIRED boundary (at `issued_at_ms + ttl_ms` granted, +1 denied); cooldown boundary (1 ms before denied, at the time granted); person delay boundary; pairwise priority tests (7 over 8, 8 over 9, 5 over 14, 8 over 14, 13 over 14); a property test that for random snapshots with at least one failing condition the result is never GRANTED; the module has no I/O and no clock of its own.
  - **Covers**: FR-021, FR-022, FR-023, FR-025, FR-048, FR-049, SC-008, SC-009, SC-020, SC-021
  - **Depends**: T007, T011, T020

- [ ] T025a [US6] Add FIRE_REQUEST handling to `device.py`: intake of the request; `duration_ms` below 500 is `FIELD_RANGE` before arbitration in every state; call `arbitration.py`; on grant: state DETER, effector on, `EFFECTOR_EVENT on`, reply GRANTED with `granted_on_ms`; on denial: reply DENIED with the permanent code, effector off, state unchanged; a duplicated fire request never grants twice.
  - **Note**: the shot does not end until T025b adds shot end, so in T025a every test uses a fresh device (a granted device stays in DETER with the effector on).
  - **Tests first**: `tests/unit/test_device_fire.py`: all-conditions-true request is granted and effector on; the GRANTED reply carries `granted_on_ms` (the FakeClock time of the grant); an `EFFECTOR_EVENT on` is emitted once on a grant and none on a denial; four representative ladder failures each leave effector off and state unchanged and reply with the permanent code: `HEARTBEAT_LOST` (first rung), `NOT_ARMED` (middle rung), `PERSON_CLEAR_DELAY` (code 14, differs from its priority 8) and `ELEVATION_LIMIT` (code 10, differs from its priority 11); all 14 reasons are covered in T024 (arbitration unit tests) and T035 (vectors); 500 and 5000 granted, 499 and 0 `FIELD_RANGE`, 5001 `ON_TIME_LIMIT`; duplicate request gets the cached reply and one grant; fire in HOMING, DISARMED, ZONE_SETUP, SAFE_ERROR denied with the right reason.
  - **Covers**: FR-022, FR-025, FR-021, FR-015, SC-009
  - **Depends**: T024, T023

- [ ] T025b [US6] Add shot end and cooldown to `device.py`: before arbitration, run the SAFE_ERROR recovery check and the cooldown expiry (so a request at exactly `cooldown_ms` finds SCANNING); the shot ends by itself at `duration_ms` (`EFFECTOR_EVENT normal end`, state COOLDOWN); every shot end except one entering SAFE_ERROR stores `cooldown_started_ms`; COOLDOWN returns to SCANNING when the cooldown has elapsed (also runs while DISARMED with no state change); `cooldown_ms` 0 disables it; the cooldown survives DISARM and ARM; only boot clears the cooldown timer.
  - **Tests first**: extend `tests/unit/test_device_fire.py`: shot ends at 1499 not yet and 1500 ended for `duration_ms` 1500; state COOLDOWN then SCANNING at exactly `cooldown_ms`; 1 ms early denied `COOLDOWN_ACTIVE` and at the time granted; `cooldown_ms` 0 never denies for cooldown; cooldown survives DISARM and ARM; three 5000 ms shots at 0, 10 s, 20 s are all granted (no per-minute rule exists); fire in COOLDOWN denied with the right reason; a FIRE_REQUEST in SAFE_ERROR with every recovery condition true (new accepted heartbeat, key on) runs recovery first, so the device is DISARMED and the reply is DENIED `NOT_ARMED`, not `HEARTBEAT_LOST`; the cooldown expiry runs while DISARMED (after a DISARM during a cooldown, advancing the FakeClock past `cooldown_ms` leaves the state DISARMED with no state change and no event, and a later ARM then a request is granted); only a reboot clears the cooldown timer (a rebuilt device on the same FakeClock has no cooldown, while DISARM, ARM, a heartbeat or a key change leave it running).
  - **Covers**: FR-049, FR-025, SC-021, SC-020, SC-009
  - **Depends**: T025a

- [ ] T026 [US6] Add continuous checking to `device.py`: while the effector is on, re-check rungs 1 to 12 except REQUEST_EXPIRED on every tick (at most 10 ms apart) and on every message; when one lapses cut at once (`EFFECTOR_EVENT cut` with that reason's permanent code, an interlock event). Outcome by cause: heartbeat lost and key off enter SAFE_ERROR with no cooldown; DISARM leaves DISARMED and starts the cooldown; person present, fault active, store invalid, elevation and zone margin end the shot, go to COOLDOWN, stay armed, latch nothing. A granted shot is never cut because its TTL passed, and a cut shot is never resumed.
  - **Tests first**: `tests/unit/test_device_interlocks.py`: for each lapse (heartbeat lost, person reported, key off, MOVE into zone margin, MOVE past elevation limit, store corrupted, DISARM) assert the effector is off within 100 ms of FakeClock time, the event cause, the resulting state, armed flag, `fault_mask`, whether a cooldown started, and one interlock event; TTL expiry mid-shot causes no cut; after a person cut and the delay elapsing the effector stays off with no new `on` event; a new request after the cooldown is granted.
  - **Covers**: FR-026, FR-049, FR-048, FR-027, SC-010, SC-020, SC-021, SC-009
  - **Depends**: T025b

- [ ] T027 [US6] Add FIRE_STOP to `device.py`: accepted in every state through steps 1 to 7 only, no arbitration; effector on: off at once, `EFFECTOR_EVENT stopped by host` (cause 0), cooldown starts, DETER to COOLDOWN, still armed, no fault, not an interlock event; no shot running: reply OK and nothing changes (no state, event, cooldown, timer); never turns the effector on or lengthens a shot; does not refresh the heartbeat, person or upload idle timers; one payload byte is `BAD_LENGTH`.
  - **Tests first**: `tests/unit/test_device_fire_stop.py`: one test per state in the protocol FIRE_STOP table (BOOT, HOMING with homing still completing, DISARMED, SCANNING, DETER, COOLDOWN with remaining time unchanged, ZONE_SETUP with the upload untouched and idle timer not reset, SAFE_ERROR); stop within 100 ms and cooldown counted from the stop (request 1 ms early denied, on time granted); idle stop followed by an immediate granted request; duplicate FIRE_STOP gives one stop event; stop never grants (property test over all states).
  - **Covers**: FR-050, FR-015, FR-049, SC-022, SC-021
  - **Depends**: T026

- [ ] T028 [US6] Extend `control.py` with the test-only state-injection interface used by scenario vectors (`apply_initial`, and `inject` for mid-vector changes): it can set inputs and state (state, homed, armed, key, angles, heartbeat age, person status, clear streak, zones, `zone_store_corrupt`, `zone_storage_unreadable`, `cooldown_remaining_ms`, `fault_mask`, `last_seq`) but has no way to produce a grant or switch the effector on. Enabled only when the device is built with test injection allowed; otherwise every call is refused.
  - **Tests first**: `tests/unit/test_control_inject.py`: every initial field takes effect; injecting a state never creates a GRANTED reply by itself (a request is still needed); no setter for the effector exists; with injection disabled the calls raise and change nothing; `set_key` on-to-off from an injected armed state follows the key-off rule.
  - **Covers**: FR-021, FR-019, FR-039, FR-042
  - **Depends**: T027

- [ ] T029 [US6] Create `src/deterrent/simulator/runner.py`: async wrapper that owns a device, feeds it bytes from a link, calls `tick` at most 10 ms apart, and publishes reply and event bytes. No decision logic inside.
  - **Tests first**: `tests/integration/test_sim_runner.py` (async, FakeClock-driven): bytes in produce reply bytes out; ticks fire at least every 10 ms of FakeClock time; stopping the runner stops the ticks; the runner cannot change a decision (a spy confirms it never touches arbitration inputs).
  - **Covers**: FR-015, FR-043, FR-026
  - **Depends**: T028

**Checkpoint (US6)**: the simulator runs alone, answers every command, and every denial reason has a unit test. No dashboard, camera, Pi or motor involved.

---

## Phase 4: User Story 7 - Written protocol and scenario test vectors (Priority: P7, built second)

**Goal**: One protocol document and four vector files that the simulator passes now and the future firmware must pass unchanged.

**Independent test**: `uv run pytest tests/contract` passes every vector; a reader can build messages from `protocol/PROTOCOL.md` alone.

- [ ] T030 [US7] Move (not copy) `specs/001-platform-core/contracts/protocol.md` to `protocol/PROTOCOL.md` with `git mv`, so only one copy exists. **Ask the owner before running the `git mv`.** Before moving, search the repository and list every document that links to `contracts/protocol.md`. A search when this file was written found: `contracts/scenario-vectors.md`, `data-model.md`, `plan.md`, `quickstart.md`, `research.md` (all under `specs/001-platform-core/`) and this file; search again at task time (also `spec.md`, `docs/`, `README`) because the list may have changed. Show the list in the report, and update those links in the same task so none is left pointing at the old path. The lines this task updates include the **Input line of this file (`tasks.md`, line 3)**, which names `contracts/protocol.md`; change it to `protocol/PROTOCOL.md`. Add a worked byte-level example generated by code (`src/deterrent/protocol/example.py`) and insert its output into the document.
  - **Tests first**: `tests/unit/test_protocol_doc.py`: `protocol/PROTOCOL.md` exists and the old path does not; no Markdown link target in any `.md` file in the repository points to `contracts/protocol.md` (the test parses `[text](target)` links, strips any `#anchor`, resolves each relative target against its file, and compares the resolved path with the old path; plain-text and backtick mentions are not links and are not checked, but the Input line of `tasks.md` is updated anyway); the reason table in the document (name, code, status) equals `protocol/reason-codes.json`; every default in section 8 equals `config/default.toml`; the worked example's hex in the document equals what `example.py` produces now.
  - **Covers**: FR-038, FR-042, SC-018, FR-040
  - **Depends**: T007, T009, T003

- [ ] T031 [P] [US7] Create `protocol/vector-schema.json` (JSON Schema for contracts/scenario-vectors.md sections 1 and 2: `id`, `description`, `covers`, `config`, `initial`, `steps` with actions `send`, `send_raw`, `set_key`, `inject`, `advance`, `expect`, and `protocol_version`) and an empty-but-valid skeleton of each file in `protocol/vectors/`.
  - **Tests first**: `tests/contract/test_vector_schema.py`: the schema itself is valid; the contract's illustrative example validates; vectors missing `id`, with a reason number instead of a name, or with an unknown action are rejected; ids are unique across files; every file records `protocol_version`.
  - **Covers**: FR-039, FR-042
  - **Depends**: T001

- [ ] T032 [US7] Create the vector runner `tests/contract/vector_runner.py` and the simulator adapter `tests/contract/simulator_adapter.py` (the `DeviceAdapter` interface: `reset`, `apply_initial`, `send`, `advance_to`, `set_key`, `read_status`, `read_zone_ids`), plus the parametrised test `tests/contract/test_vectors.py` that runs every file in `protocol/vectors/`. The runner owns encoding with the reference encoder, name-to-code resolution through `protocol/reason-codes.json`, comparison and the report; it fails any DENIED reply with code byte 13 or any cut with cause 13 in every vector.
  - **Tests first**: `tests/contract/test_vector_runner.py`: a tiny hand-written vector passes against the simulator adapter; the same vector fails against a deliberately wrong fake adapter (so the runner can fail); a vector writing a reason number is refused; retired code 13 in a reply fails the run; failure output names the vector id and the first mismatch.
  - **Covers**: FR-040, FR-042, FR-039, SC-017
  - **Depends**: T028, T031, T030

- [ ] T033 [US7] Create `tests/contract/reason_codes_lock.json` (name, code, status pairs of the released protocol version, including retired `RATE_LIMIT` 13) and `tests/contract/test_reason_codes.py` implementing checks 1 to 4, 6 and 7 of scenario-vectors.md section 8 (check 5 needs the vectors and is added by T042).
  - **Tests first**: the test file itself is the deliverable; also include negative tests on temporary copies: reassigned code fails; removed retired pair fails; `RATE_LIMIT` marked active fails; code 13 given to another name fails; a new reason with code 16 passes and with 14 fails; reordered priorities leave codes unchanged; document and `reason-codes.json` disagreement fails.
  - **Covers**: FR-023, FR-040, FR-041, SC-017
  - **Depends**: T030, T032

- [ ] T034 [US7] Write `protocol/vectors/framing_and_sequence.json`: corrupted CRC, bad framing, truncated, oversize, unknown type (generic `0xFE`), wrong length, field out of range, duplicate (cached reply, one action), duplicate fire request (one grant), stale and out-of-order sequence, sequence wrap, REJECT_NOTICE rate limit, stale or duplicate heartbeat not refreshing the watchdog. Every vector carries `framing:*` or `seq:*` tags.
  - **Tests first**: `tests/contract/test_vectors.py` runs the new file (add the vectors, run, watch failures, fix the vectors or file a simulator bug); plus the coverage test in T042 will later require these tags.
  - **Covers**: FR-039, FR-041, FR-029, FR-016, SC-006, SC-007, SC-017
  - **Depends**: T032, T015b

- [ ] T035 [US7] Write the denial and grant vectors in `protocol/vectors/fire_arbitration.json` (contract section 3 and 5): one vector per active denial reason (14), each with only that condition failing and tags `deny:<NAME>` (plus `codes:DIFFERS_FROM_PRIORITY` for PERSON_CLEAR_DELAY, REQUEST_EXPIRED, ZONE_STORE_INVALID, ELEVATION_LIMIT, IN_ZONE_MARGIN, ON_TIME_LIMIT, COOLDOWN_ACTIVE), and a grant vector where every condition holds.
  - **Tests first**: run `tests/contract/test_vectors.py`; a mutation check in `tests/contract/test_vector_mutations.py` flips each satisfied condition in the grant vector one at a time and confirms the reply changes to the matching denial (proves "only that condition fails").
  - **Covers**: FR-041, FR-039, FR-022, FR-023, FR-025, SC-008, SC-009, SC-017
  - **Depends**: T034, T025b

- [ ] T036 [US7] Add priority vectors and the retired-code vector to `fire_arbitration.json`: rung pairs `priority:rung7_over_rung8`, `rung8_over_rung9`, `rung5_over_rung14`, `rung8_over_rung14`, `rung13_over_rung14`, and `rate.no_per_minute_limit` (three 5000 ms requests at 0, 10 s, 20 s are all granted and no reply carries code 13).
  - **Tests first**: `tests/contract/test_vectors.py`; priority vectors must fail two or more conditions on purpose (asserted by the mutation test).
  - **Covers**: FR-023, FR-041, FR-048, FR-049, SC-017, SC-008
  - **Depends**: T035

- [ ] T037 [US7] Add the interlock, person, zone-margin, store-validity and key-level vectors to `fire_arbitration.json` (contract section 7.1 to 7.5): person reported, cut while on, not resumed, stale status, stale restarts the delay, boot with no status, delay boundary exact and restart, delay zero, margin and elevation denials not faults, cuts while on, TTL not re-checked while on, store empty/invalid/cut/clear/no-auto-clear/unreadable cases, live key level, reserved fault bits stay clear, `FAULT_ACTIVE` with an injected reserved bit.
  - **Tests first**: `tests/contract/test_vectors.py`; each cut vector asserts the effector is off within 100 ms (`within_ms: 100`).
  - **Covers**: FR-026, FR-048, FR-035, FR-024, FR-041, SC-010, SC-012, SC-020, SC-017
  - **Depends**: T036, T026

- [ ] T038 [US7] Add the shot-duration, cooldown and FIRE_STOP vectors to `fire_arbitration.json` (contract section 7.6, all listed ids): duration range and no-hold, per-shot configurable, step not checked, shot ends at max; cooldown after normal end, boundary exact, after person cut, after DISARM, not started by SAFE_ERROR, interlocks as in other states, zero disables; FIRE_STOP running, idle, all states (BOOT to COOLDOWN), SAFE_ERROR, ZONE_SETUP, duplicate, bad length.
  - **Tests first**: `tests/contract/test_vectors.py`; every FIRE_STOP table row has a `stop:STATE_*` tag.
  - **Covers**: FR-049, FR-050, FR-041, FR-048, SC-021, SC-022, SC-020, SC-017
  - **Depends**: T037, T027

- [ ] T039 [US7] Write `protocol/vectors/zones.json`: every zone rejection code, atomicity (nothing stored on any rejection), partial upload discarded, a new upload replaces the old, delete (known and unknown id), list, clear, store full, storage error, corrupted store; the ZONE_SETUP table rows IN and EXIT_1 to EXIT_8 and the non-exit list (contract section 6.1), idle timer only reset by upload messages.
  - **Tests first**: `tests/contract/test_vectors.py`; tags `zone:<CODE>` for every code in protocol section 7 and `setup:IN`, `setup:EXIT_1` to `setup:EXIT_8`, `setup:NONEXIT`.
  - **Covers**: FR-041, FR-031, FR-032, FR-033, FR-035, FR-037, SC-011, SC-012, SC-017
  - **Depends**: T034, T023

- [ ] T040 [US7] Add the key-off, SAFE_ERROR recovery and DISARM vectors (contract section 6.2) to `fire_arbitration.json` and `timing.json`: key off while armed, firing, in DISARMED and in ZONE_SETUP; boot with key off is not an event; recovery with key on, never re-arms, heartbeat alone and key alone not enough, both faults, non-clearing fault; DISARM in SAFE_ERROR and during HOMING.
  - **Tests first**: `tests/contract/test_vectors.py`; tags `key:*` and `disarm:*` as listed in the contract.
  - **Covers**: FR-027, FR-020, FR-041, SC-010, SC-017
  - **Depends**: T038, T019b

- [ ] T041 [US7] Write `protocol/vectors/timing.json`: one vector per timing rule with boundary values on both sides: `heartbeat_interval_ms` (heartbeats at the interval keep the device alive), `watchdog_ms`, `person_clear_holdoff_ms`, `person_reengage_delay_ms`, `per_shot_ms`, `cooldown_ms`, `zone_upload_timeout_ms`, `homing_duration_ms`, and the TTL rule (`fire_ttl_ms` default on a request, exact expiry boundary).
  - **Tests first**: `tests/contract/test_vectors.py`; tags `timing:<name>` for each named value in protocol section 8 that the device enforces.
  - **Covers**: FR-041, FR-038, FR-025, FR-027, FR-048, FR-049, SC-017
  - **Depends**: T040, T039

- [ ] T042 [US7] Create `tests/contract/test_vector_coverage.py`: fails if any denial reason, zone rejection code, timing rule, ZONE_SETUP table row, key rule (`key:*`) or FIRE_STOP state row in `protocol/PROTOCOL.md` has no vector whose `covers` names it; checks `codes:DIFFERS_FROM_PRIORITY` is present on the seven listed reasons; adds check 5 of the reason-code rules (the runner compares the numeric code byte of every DENIED reply with the number from `reason-codes.json`); retired reasons need no denial vector.
  - **Tests first**: the coverage test is the deliverable; also negative tests using a temporary copy of the vector set with one vector removed (must fail) and with the file complete (must pass).
  - **Covers**: FR-041, FR-040, SC-017, SC-008
  - **Depends**: T041, T033

- [ ] T043 [US7] Create `tests/contract/independent_encoder.py` written only from `protocol/PROTOCOL.md` (separate COBS, CRC and layouts, not importing `deterrent.protocol`) and `tests/contract/test_hand_built_messages.py` that builds the messages of five named vectors (one denial, one zone upload, one duplicate, one fire stop, one corrupted frame) and compares the bytes and the predicted replies with the reference encoder and the simulator.
  - **Tests first**: the test file is the deliverable; it must fail if `PROTOCOL.md` and the reference encoder disagree on any byte.
  - **Covers**: SC-018, FR-042, FR-038
  - **Depends**: T042

**Checkpoint (US7)**: `uv run pytest tests/contract` passes all vectors; the lock test passes; five vectors are reproducible from the document alone.

---

## Phase 5: Foundational B (shared backend: observability, link, engagement, API server)

**Purpose**: What every dashboard story needs. Nothing here can decide a fire.

- [ ] T044 Create `src/deterrent/observability/schema.sql` and `src/deterrent/observability/store.py` part 1: SQLite writer thread for `logs`, `traces`, `spans` and `metric_events` in `data/observability.sqlite3` (columns, types and indexes exactly as data-model.md sections 1.2 to 1.5; times in UTC epoch microseconds), a non-blocking `submit()` for callers, and a read connection.
  - **Tests first**: `tests/unit/test_store_basic.py` (temp folder): records written by `submit()` are readable; ordering by `ts_us, id`; data survives closing and reopening the store; `submit()` returns without waiting for disk (writer thread stopped, call still returns); file lives under `data/` and not in the device folder.
  - **Covers**: FR-012, FR-013
  - **Depends**: T001, T003

- [ ] T045 Extend `store.py` part 2, resilience: when the database is full or locked keep running, hold the newest 5000 records in a memory ring, set a storage-warning flag within 10 s, retry every 2 s, log the fault, clear the warning and save the kept entries when storage recovers. A test hook switches modes `full`, `locked` and `ok`.
  - **Tests first**: `tests/unit/test_store_resilience.py`: in `locked` mode `submit()` never blocks or raises and the warning appears within 10 s of FakeClock time; ring keeps the newest 5000; after `ok` the kept entries are written and the warning clears; zero crashes over 1000 submits; reads of live (in-memory) data still work while failing.
  - **Covers**: FR-013, SC-016, FR-012
  - **Depends**: T044, T004

- [ ] T046 [P] Create `src/deterrent/observability/logging.py`: JSON log records with timestamp, component, severity (`DEBUG`..`CRITICAL`), message, optional `trace_id` (16 hex) and `span_id` (8 hex), a trace-context helper, and a filter that rejects image data and known secret fields.
  - **Tests first**: `tests/unit/test_logging.py`: every record has timestamp, component and severity; trace and span ids have the right shape; a record carrying a bytes image or a field named like a secret is dropped or scrubbed and the drop is itself logged; component required.
  - **Covers**: FR-007, FR-014
  - **Depends**: T044

- [ ] T047 [P] Create `src/deterrent/observability/tracing.py`: `Trace` and `Span` objects for the eight fixed steps (capture, encode, network, inference, decision, command, esp32_ack, effector_action), statuses `OK`, `DENIED`, `TIMEOUT`, `NOT_RUN`, sequential-span rule, total equals last end minus first start, an unfinished step is kept with `TIMEOUT` or `NOT_RUN` (never omitted), `simulated` flag, outcome values from data-model.md.
  - **Tests first**: `tests/unit/test_tracing.py`: a completed trace has exactly eight spans in position order; total equals the sum of sequential durations plus gaps; a missing esp32_ack is stored as `TIMEOUT`; a denied request gives `esp32_ack` `DENIED` with the reason and `effector_action` `NOT_RUN` with "no action"; overlapping spans are refused.
  - **Covers**: FR-009, SC-014
  - **Depends**: T044

- [ ] T048 [P] Create `src/deterrent/observability/metrics.py`: counters, gauges and latency samples written as `metric_events`; nearest-rank percentiles (sort, position `ceil(p/100 x n)`); empty state when no samples; the required metric names and units from data-model.md (laptop CPU/GPU/RAM/temperatures, `e2e_latency_simulated`, `fire_requests_granted`, `fire_requests_denied` by reason, `interlock_events` by cause, `protocol_messages_rejected` by cause, `homing_drift_simulated`).
  - **Tests first**: `tests/unit/test_metrics.py`: p50 and p95 equal an independent calculation (plain Python `sorted` and index) on fixed and random samples; no samples gives "empty", never zeros; one sample gives p50 equal p95; counters sum increments per label; every required name exists with its unit; unit is required.
  - **Covers**: FR-010, FR-011, SC-015
  - **Depends**: T044

- [ ] T049 Create `src/deterrent/observability/queries.py`: log filters by component, severity and trace id combined with AND, newest paging with `before_id`, `limit` default 200 and maximum 1000, trace lookup returning spans in position order, list of traces newest first, metric summaries (counter totals by labels, latest gauge, distribution count/p50/p95 with method `nearest-rank`, omit empty distributions, series for charts).
  - **Tests first**: `tests/unit/test_queries.py`: each filter alone and combined returns exactly the matching entries (100% of a table of test cases); no match returns an empty list; trace filter returns time order; component list; paging has no gaps or repeats; unknown trace id returns "not found".
  - **Covers**: FR-008, SC-013, SC-015, FR-010
  - **Depends**: T046, T047, T048

- [ ] T050 [P] Create `src/deterrent/link/base.py` (`DeviceLink`: send bytes, receive bytes), `link/inprocess.py` (to the in-process simulator runner) and `link/mock.py` (fault injection: corrupt, duplicate, drop, delay, reorder).
  - **Tests first**: `tests/unit/test_link.py`: in-process link round-trips a HEARTBEAT; mock link corrupts, duplicates and drops on command and the simulator still rejects, deduplicates and times out correctly (end to end through T029); the link cannot reach arbitration directly.
  - **Covers**: FR-044, SC-006, SC-007, FR-016, FR-029
  - **Depends**: T029

- [ ] T051 Create `src/deterrent/link/client.py` (`DeviceClient`): u16 sequence numbers that wrap, request and reply matching, `command_ttl_ms` timeout, time offset between laptop clock and device clock for `issued_at_ms`, heartbeat sender every `heartbeat_interval_ms` with a person-clear provider (default clear), and a decoded reply returned unchanged. The client has no code path that decides or fakes a grant: it only returns what the device replied.
  - **Tests first**: `tests/unit/test_client.py` (FakeClock, mock link): sequence wraps 65535 to 0; heartbeat sent every 200 ms; no reply within `command_ttl_ms` raises a timeout (used for the `TIMEOUT` span); a DENIED reply is returned as DENIED even when the test tries every other field to look like success; stopping the heartbeat source leads the simulator to SAFE_ERROR after the watchdog; `issued_at_ms` uses the device time base.
  - **Covers**: FR-021, FR-024, FR-025, FR-044, FR-015
  - **Depends**: T050, T004

- [ ] T052 Create `src/deterrent/composition.py`, the only module that wires simulator, link, client, storage and API, including the `DeviceEventSink` adapter that turns device events into logs (component `simulator`), counters (granted/denied by reason, interlock events by cause, rejected messages by cause), the `homing_drift_simulated` sample and the live status. Reads `config/default.toml`.
  - **Tests first**: `tests/integration/test_composition.py`: building the app graph starts and stops cleanly; a denied fire request increases `fire_requests_denied` for that reason by one; a granted request increases `fire_requests_granted`; a cut increases `interlock_events` by cause; a corrupted frame increases `protocol_messages_rejected` by cause; each generates a structured log entry; no other module imports both `simulator` and `observability` (checked by the architecture test T070).
  - **Covers**: FR-011, FR-007, FR-021, FR-043
  - **Depends**: T029, T051, T049, T012

- [ ] T053 Create `src/deterrent/engagement/simulated.py`: the developer-triggered simulated engagement that records one trace with the eight spans (made-up timings for capture, encode, network and inference labelled `simulated`; real measured durations for decision, command, esp32_ack and effector_action), sends one FIRE_REQUEST through `DeviceClient` when `fire` is true, obtains the decision only from the device's reply, sets `esp32_ack` and `effector_action` from that reply, handles a missing reply as `TIMEOUT`, records the `e2e_latency_simulated` sample, and returns the `trace_id`. Log entries carry the trace and span ids.
  - **Tests first**: `tests/integration/test_engagement.py`: engagement with key off gives a DENIED trace (ack shows `KEY_OFF`, effector `NOT_RUN`); fully armed gives a GRANTED trace; dropped reply (mock link) gives `esp32_ack` `TIMEOUT`; eight spans, total consistent, `simulated` true; the e2e sample equals the trace total; the test replaces the device with a stub that always denies and the engagement still reports DENIED (proves it never decides); `fire: false` gives `NO_FIRE_REQUESTED`.
  - **Covers**: FR-047, FR-009, FR-011, FR-021, SC-014
  - **Depends**: T052, T047, T048

- [ ] T054 Create `src/deterrent/api/app.py` (`create_app()` factory, lifespan, static files mounted for `frontend/`, bind only 127.0.0.1, one error shape `{"error":{"code","message"}}`) and in `api/routes_read.py` the `GET /api/device/status` and `GET /api/system/status` endpoints reading from memory, plus the page routes `/`, `/logs`, `/traces`, `/observability`.
  - **Tests first**: `tests/integration/test_api_basic.py` (httpx): device status with simulator stopped is exactly `{"running": false}`; running status has every field from dashboard-api.md; system status shows `storage_warning` and `dev_tools`; unknown route returns the standard error shape; the server settings bind loopback only; responses never contain image data or secret fields.
  - **Covers**: FR-004, FR-013, FR-014, FR-030
  - **Depends**: T052

- [ ] T055 Create `src/deterrent/api/live.py`: the `/ws/live` hub (server to client only) with message types `snapshot`, `device`, `device_offline`, `log`, `storage_warning`, `tick` (every 1 s); device updates on every status change and at least once per second while running; reads from memory so it never waits on disk. (`health` messages are added in T059.)
  - **Tests first**: `tests/integration/test_live.py` (async websocket client, FakeClock): snapshot arrives immediately on connect; a pan change reaches the client within 1 s of FakeClock time (SC-004 measured here); stopping the simulator sends `device_offline`; a new log entry is pushed; `storage_warning` is pushed when the store flips to warning and cleared later; two clients receive the same values; with the store in `locked` mode the hub still sends within its schedule.
  - **Covers**: FR-004, FR-006, FR-013, SC-004, SC-005, SC-016
  - **Depends**: T054, T045

- [ ] T056 Create `src/deterrent/api/routes_dev.py`: developer tools (only when `PIGEON_DEV_TOOLS=1`; otherwise 404) per dashboard-api.md section 4: simulator start and stop, `key`, `person`, `command` (any protocol type including ARM, DISARM, FIRE_REQUEST, FIRE_STOP, passed through the normal client; returns the decoded reply unchanged), `engagement`, `zone-store/corrupt`, `storage/fail`. (`sensors/{name}/fail` is added in T059.)
  - **Tests first**: `tests/integration/test_api_dev.py`: all routes return 404 when dev tools are off; simulator start leads through BOOT and HOMING to DISARMED; stop makes device status `running: false`; `command` with `duration_ms` 499 returns the device's `FIELD_RANGE` reject unchanged; a FIRE_REQUEST reply is exactly what the device produced; `engagement` returns a `trace_id` that exists; `storage/fail` raises and clears the warning; no dev route has a parameter that can force a grant.
  - **Covers**: FR-047, FR-021, FR-043, FR-015, FR-050
  - **Depends**: T054, T053

**Checkpoint**: with the dev tools one can start the simulator, run an engagement and see logs, traces and metrics in the store, using only HTTP tests. No page exists yet.

---

## Phase 6: User Story 1 - See laptop health live (Priority: P1)

**Goal**: CPU, GPU, RAM and temperatures update on the Overview; unreadable slots read "unavailable".

**Independent test**: Open the Overview with nothing attached; make one sensor unreadable and see only that slot change.

- [ ] T057 [P] [US1] Create `src/deterrent/health/sensors.py`: `SensorProvider` interface, real providers (psutil for CPU load, RAM and CPU temperature where exposed; `nvidia-ml-py` for GPU load and temperature) that return "unavailable" instead of raising when a sensor is missing, and a controllable `MockSensorProvider` (value, error, hang). Names `cpu_load`, `gpu_load`, `ram_used`, `cpu_temp`, `gpu_temp`; units `percent`, `celsius`.
  - **Tests first**: `tests/unit/test_sensors.py`: mock returns values, raises, and hangs on command; real providers are tested only through patched libraries (no hardware): missing temperature attribute gives unavailable, missing NVML gives unavailable; every reading has a unit.
  - **Covers**: FR-001, FR-002, FR-044
  - **Depends**: T001

- [ ] T058 [US1] Create `src/deterrent/health/collector.py`: samples every sensor every 1 s in worker threads with a 1 s per-sensor timeout so one slow sensor cannot hold up the others; a failed, hung or timed-out read produces `unavailable` with value null and never reuses the previous number; gauges persisted every 10 s through `metrics.py`.
  - **Tests first**: `tests/unit/test_collector.py` (FakeClock + mock sensors): all sensors refresh each second; hanging GPU makes only `gpu_load` unavailable and the others stay on schedule; a value that was readable then fails becomes unavailable on the next refresh (no stale number); gauges saved every 10 s; no missed refresh longer than 4 s over a simulated 5-minute run.
  - **Covers**: FR-001, FR-002, FR-011, SC-001, SC-003
  - **Depends**: T057, T048

- [ ] T059 [US1] Wire health into the API: `GET /api/health/current` in `api/routes_read.py`, the `health` message (every collection) in `api/live.py`, the `health` part of the `snapshot`, collector start in `composition.py`, and the dev route `POST /api/dev/sensors/{name}/fail` in `api/routes_dev.py`.
  - **Tests first**: `tests/integration/test_health_api.py`: current readings match the dashboard-api.md shape; failing the GPU sensor gives `state: unavailable`, `value: null` for GPU and live values for the rest; websocket `health` messages arrive at least every 2 s; first data arrives within 3 s of connecting (SC-002); unfailing restores the value.
  - **Covers**: FR-001, FR-002, SC-002, SC-003, SC-001
  - **Depends**: T058, T055, T056, T054

- [ ] T060 [US1] Create the frontend base: `frontend/css/app.css`, `frontend/js/live.js` (one WebSocket; "no message for 3 s" means disconnected and the indicator turns red; reconnect after 1 s, 2 s, then every 4 s; each successful connect replaces all live values with the new snapshot), and copy Chart.js into `frontend/vendor/chart.umd.min.js`. Ask the owner before downloading the Chart.js file (external download); the owner may supply the file instead.
  - **Tests first**: `tests/integration/test_frontend_static.py`: the server serves the page, css, js and vendor file with correct content types; `live.js` exports the reconnect schedule (1, 2, 4 s) as a documented constant checked by a text test; reconnect after a server restart is tested at the API level in T055 and checked in the browser in the owner checklist (JavaScript behaviour is not run by pytest; say so in the report).
  - **Covers**: FR-006, SC-005
  - **Depends**: T055

- [ ] T061 [US1] Create the health tiles on `frontend/index.html` and `frontend/js/overview.js`: CPU load, GPU load, RAM use, CPU and GPU temperature with units, refreshed from `health` messages without reloading; an unreadable value shows the word "unavailable"; connection indicator visible.
  - **Tests first**: `tests/integration/test_frontend_static.py` extended: `index.html` contains an element id for each of the five slots, the word "unavailable" handling in `overview.js` (text test), and the connection indicator; an API-level test already proves the data states (T059). Owner checks the real page using the quickstart steps for Story 1.
  - **Covers**: FR-001, FR-002, FR-006, SC-001, SC-002, SC-003
  - **Depends**: T060, T059

**Checkpoint (US1)**: Overview shows live health; a failed sensor reads "unavailable"; nothing else is affected.

---

## Phase 7: User Story 2 - Overview shell with live simulator state (Priority: P2)

**Goal**: Full Overview layout with labelled placeholders and live pan, tilt and state from the simulator.

**Independent test**: Open the Overview with the simulator running; move it; see the values follow within 1 s; stop it and see "unavailable".

- [ ] T062 [US2] Add the live device panel to `frontend/index.html` and `frontend/js/overview.js`: pan angle, tilt angle (shown in degrees from centidegrees) and system state from `device` messages; state shown only as one of the twelve names from `codes.py` (SAFE_ERROR displayed as "SAFE/ERROR"); when the simulator is stopped show "unavailable" for all three (never the last value) and an obvious "simulator not running" label.
  - **Tests first**: `tests/integration/test_device_panel.py`: with the websocket, a MOVE through `/api/dev/command` is visible in `device` messages within 1 s; every one of the 12 states renders to a name via a text test of the state map in `overview.js` and no unnamed value is possible; `device_offline` clears pan, tilt and state (text test of the handler); `index.html` has ids for pan, tilt, state and the not-running label.
  - **Covers**: FR-004, FR-005, FR-003, SC-004
  - **Depends**: T061, T056

- [ ] T063 [P] [US2] Add the labelled placeholders to `frontend/index.html`: live camera feed with detection overlay (says what will appear and in which later feature), a Pi health panel reading "not connected", and the recent-log list (newest first, updating from `log` messages and the 50 entries in the `snapshot`).
  - **Tests first**: `tests/integration/test_frontend_static.py` extended: `index.html` contains the camera placeholder text, the "not connected" Pi panel and the recent-log list element; an API test confirms the snapshot's `recent_logs` has at most 50 entries newest last and that a new log appears as a `log` message.
  - **Covers**: FR-003, FR-007
  - **Depends**: T061

**Checkpoint (US2)**: the Overview shell is complete.

---

## Phase 8: User Story 3 - Browse and filter structured logs (Priority: P3)

**Goal**: Logs view with filters by component, severity and trace id.

**Independent test**: Run an engagement, filter by component, severity and trace id, confirm exactly the matching entries.

- [ ] T064 [US3] Add `GET /api/logs` and `GET /api/logs/components` to `api/routes_read.py` using `queries.py` (query `component`, `severity`, `trace_id`, `limit`, `before_id`; empty match returns `items: []` and `has_more` false).
  - **Tests first**: `tests/integration/test_api_logs.py`: run an engagement through `/api/dev/engagement`, then each filter alone and combined returns exactly the matching entries; the engagement's trace id returns its entries in time order; no match returns an empty list with status 200; component list matches the components that logged; `limit` above 1000 is capped.
  - **Covers**: FR-008, SC-013, FR-007
  - **Depends**: T049, T056, T054

- [ ] T065 [US3] Create `frontend/logs.html` and `frontend/js/logs.js`: filter controls for component (from the components endpoint), severity and trace id; combined filters; a "nothing matched" message (not an error); the trace id in each row links to `/traces?trace_id=...`.
  - **Tests first**: `tests/integration/test_frontend_static.py` extended: `logs.html` has the three filter controls, the empty-state text and the link pattern; `logs.js` builds the query exactly as the API expects (text test). Owner checks the Story 3 steps in the quickstart; finding one engagement's entries by trace id in under 10 seconds is a manual timing check in the owner checklist.
  - **Covers**: FR-008, SC-013
  - **Depends**: T064, T060

**Checkpoint (US3)**.

---

## Phase 9: User Story 4 - View an engagement as a timed trace (Priority: P4)

**Goal**: Trace view with eight timed steps and a consistent total.

**Independent test**: Trigger an engagement, open its trace, confirm eight steps in order with durations that add up to the total.

- [ ] T066 [US4] Add `GET /api/traces` and `GET /api/traces/{trace_id}` to `api/routes_read.py` (list newest first, detail with always eight spans, `TRACE_NOT_FOUND` as 404).
  - **Tests first**: `tests/integration/test_api_traces.py`: after an engagement the trace has eight spans in order, `total_us` equals last end minus first start, `simulated` true; a denied engagement shows `esp32_ack` `DENIED` with the reason and `effector_action` `NOT_RUN` "no action"; a dropped-reply engagement shows `TIMEOUT` and the step is present; unknown id gives 404 `TRACE_NOT_FOUND`; the id from `/api/logs` opens the same engagement; over 100% of a batch of 20 test engagements the durations add up to the total.
  - **Covers**: FR-009, SC-014, SC-013
  - **Depends**: T049, T053, T056

- [ ] T067 [US4] Create `frontend/traces.html` and `frontend/js/traces.js`: trace list, and `?trace_id=` detail showing the eight steps in order with start, duration and status, the denial reason on the acknowledgement step, "no action" on the effector step, "incomplete" or "timed out" for unfinished steps, the total, and a "simulated" label on made-up timings.
  - **Tests first**: `tests/integration/test_frontend_static.py` extended: `traces.html` and `traces.js` contain the eight step names in order, the "simulated" label, and the incomplete/timed-out wording (text tests). Owner checks Story 4 steps from the quickstart in the browser.
  - **Covers**: FR-009, SC-014
  - **Depends**: T066, T060

**Checkpoint (US4)**.

---

## Phase 10: User Story 5 - View metrics on an Observability page (Priority: P5)

**Goal**: Counters, gauges and p50/p95 latency on one page, with an honest empty state.

**Independent test**: Run engagements, compare the page figures with an independent calculation on the same samples.

- [ ] T068 [US5] Add `GET /api/metrics` and `GET /api/metrics/{name}/series` to `api/routes_read.py` (counters with labels, gauges, distributions with `count`, `p50`, `p95`, `method: nearest-rank`; a distribution with no samples is omitted; series as `[[ts_us, value], ...]`).
  - **Tests first**: `tests/integration/test_api_metrics.py`: run several engagements (cooldown respected via the fake clock); p50 and p95 equal an independent calculation on the same stored samples (100% of cases in a table of sample sets); a denied request raises `fire_requests_denied{reason}` by exactly one; fresh database returns no distributions (empty state, not zeros); each metric has a name and unit; series respects `since_us`.
  - **Covers**: FR-010, FR-011, SC-015
  - **Depends**: T049, T053, T056

- [ ] T069 [US5] Create `frontend/observability.html` and `frontend/js/observability.js`: counters, gauges and p50/p95 tables with names and units, Chart.js lines for gauges, an empty state when there are no samples, and the "simulated" label on `e2e_latency_simulated`.
  - **Tests first**: `tests/integration/test_frontend_static.py` extended: page contains the empty-state wording, units and the simulated label (text tests); owner compares figures against the API output in the quickstart Story 5 steps.
  - **Covers**: FR-010, FR-011, SC-015
  - **Depends**: T068, T060

**Checkpoint (US5)**: all seven stories complete.

---

## Phase 11: Polish and cross-cutting checks

- [ ] T070 Create `tests/architecture/test_boundaries.py`: (a) `deterrent.simulator` imports no `fastapi`, `observability`, `api`, `composition`, `link` or `engagement`; (b) only `simulator/arbitration.py` can produce a GRANTED result and only `simulator/device.py` switches the effector, found by scanning source for the grant result constant and effector setter; (c) no module under `api/`, `link/`, `engagement/`, `health/`, `observability/` or `frontend/` contains a grant path, the string GRANTED is only read from replies; (d) only `composition.py` imports both `simulator` and `observability`; (e) `tests/` and `protocol/` are not imported by `src/`.
  - **Tests first**: the test file is the deliverable; also a negative test on a temporary source tree where a fake `api` module imports the effector setter and the check fails.
  - **Covers**: SC-019, FR-021, FR-044
  - **Depends**: T056, T053, T029

- [ ] T071 Create `tests/integration/test_restart_and_storage.py`: restart persistence (logs, traces, metrics and the simulator's zones are still there after the store and device are rebuilt on the same folders; the laptop side never opens `zone_store.sqlite3`), storage-fault run (full and locked; live websocket keeps going; warning within 10 s; zero crashes; the simulator's safety decisions are unchanged during the fault), and simulator zone storage full or locked rejects the upload with the old zones intact.
  - **Tests first**: the test file is the deliverable (tests first means written before any fix it exposes).
  - **Covers**: FR-012, FR-013, FR-036, FR-037, FR-030, SC-016
  - **Depends**: T056, T055, T045, T023

- [ ] T072 Create `tests/integration/test_privacy.py`: no log, trace, metric or API response contains image data or secret-named fields; `git status --short` shows no `data/` or `*.sqlite3`; loopback bind only; no network call is made by the application (test with sockets blocked).
  - **Tests first**: the test file is the deliverable.
  - **Covers**: FR-014, FR-012
  - **Depends**: T071

- [ ] T073 Create the slow-marked test `tests/integration/test_health_soak.py` (`pytest -m slow`): a simulated 5-minute health run on the FakeClock with the real collector schedule and mock sensors, one GPU hang and one temperature outage; asserts no refresh gap above 4 s and every other slot on schedule (SC-001, SC-003). Register the `slow` marker in `pyproject.toml`.
  - **Tests first**: the test is the deliverable.
  - **Covers**: SC-001, SC-003, SC-002
  - **Depends**: T061

- [ ] T074 Write `docs/platform-core-modules.md`: one plain-language paragraph per module (what it does, what it never does, which tests prove it), the safety-authority split diagram in words, and a short "how to explain this in an interview" list. No code.
  - **Tests first**: `tests/unit/test_docs_modules.py`: every Python module under `src/deterrent/` is mentioned by path in the document.
  - **Covers**: FR-038, SC-018
  - **Depends**: T070

- [ ] T075 Run the whole quickstart (`specs/001-platform-core/quickstart.md`) and record the measured numbers for SC-001 to SC-022 (all 22) in a short results table in the task report; list any criterion not yet measured and why. Tell the owner about two quickstart corrections: (1) the "SC-001 to SC-019" line in section 6 must read "SC-001 to SC-022"; (2) the Story 6 command that names `tests/unit/test_zones.py` must name `tests/unit/test_zones_*.py` (the zone tests are split across `test_zones_upload.py`, `test_zones_commit.py` and `test_zones_store.py`).
  - **Tests first**: `uv run ruff check`, `uv run ruff format --check` and the whole `uv run pytest` including `-m slow` pass; each quickstart edge check table row is run by hand and ticked.
  - **Covers**: FR-043, FR-045, SC-001, SC-002, SC-003, SC-004, SC-005, SC-016, SC-017, SC-018, SC-019, SC-020, SC-021, SC-022
  - **Depends**: T073, T072, T074, T069, T067, T065, T063, T062

---

## Dependencies and execution order

```text
T001 -> T002, T003, T004
T001,T003,T004 -> T005..T011 (T005 and T010 parallel)
Protocol core -> T012..T029 (strict order, with T015a -> T015b, T019a -> T019b and T025a -> T025b; T024 can be written in parallel with T021..T023 once T020 is done)
Later tasks that depended on a task now split into "a" and "b" halves depend on the "b" half: T016 and T034 on T015b; T020, T021 and T040 on T019b; T026 and T035 on T025b
T029 + T030..T043 (T031 parallel with T030)
T044..T049 (T046, T047, T048 parallel after T044) -> T050..T056
Phase 6..10 (US1..US5) depend on T055/T056; US3, US4, US5 are independent of each other once T060 exists
T070..T075 last
```

Story completion order: US6, US7, then US1, US2, US3, US4, US5. After Phase 5, US1 to US5 are independent apart from sharing `frontend/index.html` (US1 then US2) and `api/routes_read.py` (do US3, US4 and US5 one at a time to avoid file clashes).

## Parallel opportunities

- T004, T005, T010 (different files, same early dependency).
- T012 with T013 (no shared file).
- T024 (pure function) while T021 to T023 are being done, once T020 is finished.
- T031 with T030.
- T046, T047, T048 after T044.
- T050 with T044 to T049 (only needs T029).
- T057 can start any time after T001.
- T063 with T062 is not parallel (same files); T063 is marked [P] only for its dependency on T061.
- The split halves are never parallel: T015b, T019b and T025b each need their "a" half finished first (same file, `device.py`).
- Never run two tasks in the same session: CLAUDE.md requires one task, then a report.

## Implementation strategy

1. **MVP = the safety core**: finish Phases 1 to 4 (T001 to T043). That alone proves the central claim: the simulator is the only fire authority, the protocol is written down, and the vectors pass.
2. Add Phase 5 and US1 (T044 to T061) to get the first end-to-end chain (collect, deliver, display).
3. Add US2 to US5 one at a time; each ends at a checkpoint the owner can check in a browser.
4. Polish (T070 to T075) closes the architecture, privacy and measurement criteria.

## Assumptions and decisions for the owner to confirm

- **Story order** departs from spec priority order on purpose (your instruction).
- **Runner paths** `tests/contract/vector_runner.py` and `tests/contract/simulator_adapter.py` are not named by the plan; chosen to sit next to the contract tests.
- **Frontend JavaScript is not executed by pytest** (the stack has no JavaScript test tool and adding Playwright or Node would change the stack). Timing of the Overview (SC-001 to SC-005) is measured at the websocket and API level; browser behaviour is checked by the owner's manual steps. Say if you want a browser-test tool added as a separate decision.
- **Chart.js** must be downloaded once (T060); the task asks before doing it.
- **`uv add`** installs project dependencies only inside the project; nothing global.
- **SC-018's "new reader" half** is covered by an automated independent-encoder test (T043) plus the owner's own reading check; a real new reader is manual.

## Coverage table

| Item | Covered by tasks |
|------|------------------|
| FR-001 | T057, T058, T059, T061 |
| FR-002 | T057, T058, T059, T061 |
| FR-003 | T062, T063 |
| FR-004 | T054, T055, T062 |
| FR-005 | T007, T016, T062 |
| FR-006 | T055, T060, T061 |
| FR-007 | T046, T052, T063, T064 |
| FR-008 | T049, T064, T065 |
| FR-009 | T047, T053, T066, T067 |
| FR-010 | T048, T049, T068, T069 |
| FR-011 | T012, T016, T048, T052, T053, T058, T068, T069 |
| FR-012 | T014, T044, T045, T071, T072 |
| FR-013 | T044, T045, T054, T055, T071 |
| FR-014 | T046, T054, T072 |
| FR-015 | T008, T009, T015a, T017, T021, T025a, T027, T029, T051, T056 |
| FR-016 | T005, T006, T008, T015a, T015b, T034, T050 |
| FR-017 | T016, T017, T018 |
| FR-018 | T003, T017 |
| FR-019 | T018, T028 |
| FR-020 | T018, T019b, T040 |
| FR-021 | T012, T024, T025a, T028, T051, T052, T053, T056, T070 |
| FR-022 | T008, T024, T025a, T035 |
| FR-023 | T007, T009, T024, T033, T035, T036 |
| FR-024 | T019a, T020, T037, T051 |
| FR-025 | T024, T025a, T025b, T035, T041, T051 |
| FR-026 | T026, T029, T037 |
| FR-027 | T019a, T019b, T026, T040, T041 |
| FR-028 | T009, T016, T018, T019a, T019b |
| FR-029 | T015b, T019a, T034, T050 |
| FR-030 | T014, T023, T054, T071 |
| FR-031 | T005, T021, T022, T023, T039 |
| FR-032 | T010, T021, T022, T039 |
| FR-033 | T022, T023, T039 |
| FR-034 | T003, T011 |
| FR-035 | T023, T037, T039 |
| FR-036 | T003, T013, T014, T023, T071 |
| FR-037 | T013, T014, T022, T039, T071 |
| FR-038 | T005, T006, T007, T008, T009, T030, T041, T043, T074 |
| FR-039 | T028, T031, T032, T034, T035 |
| FR-040 | T030, T032, T033, T042 |
| FR-041 | T033, T034, T035, T036, T037, T038, T039, T040, T041, T042 |
| FR-042 | T028, T030, T031, T032, T043 |
| FR-043 | T001, T002, T004, T029, T052, T056, T075 |
| FR-044 | T004, T012, T013, T050, T051, T057, T070 |
| FR-045 | T001, T002, T004, T075 |
| FR-046 | T010, T011 |
| FR-047 | T053, T056 |
| FR-048 | T020, T024, T026, T036, T037, T038, T041 |
| FR-049 | T024, T025b, T026, T027, T036, T038, T041 |
| FR-050 | T027, T038, T056 |
| SC-001 | T058, T059, T061, T073, T075 |
| SC-002 | T059, T061, T073, T075 |
| SC-003 | T058, T059, T061, T073, T075 |
| SC-004 | T055, T062, T075 |
| SC-005 | T055, T060, T075 |
| SC-006 | T006, T015a, T034, T050 |
| SC-007 | T015b, T034, T050 |
| SC-008 | T024, T035, T036, T042 |
| SC-009 | T024, T025a, T025b, T026, T035 |
| SC-010 | T026, T037, T040 |
| SC-011 | T022, T023, T039 |
| SC-012 | T023, T037, T039 |
| SC-013 | T049, T064, T065, T066 |
| SC-014 | T047, T053, T066, T067 |
| SC-015 | T048, T049, T068, T069 |
| SC-016 | T045, T055, T071, T075 |
| SC-017 | T032, T033, T034, T035, T036, T037, T038, T039, T040, T041, T042, T075 |
| SC-018 | T030, T043, T074, T075 |
| SC-019 | T070, T075 |
| SC-020 | T020, T024, T025b, T026, T037, T038, T075 |
| SC-021 | T024, T025b, T026, T027, T038, T075 |
| SC-022 | T027, T038, T075 |
