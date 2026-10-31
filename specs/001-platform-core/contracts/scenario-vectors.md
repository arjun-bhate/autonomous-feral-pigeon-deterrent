# Scenario Vector Format (draft v0.1)

**Status**: Design draft. The implementation creates `protocol/vector-schema.json` (JSON Schema for this format) and the vector files under `protocol/vectors/`.

**Purpose**: a rule book of "given this starting situation and these messages, the device must reply and end up exactly like this". The simulator passes every vector now; the future firmware must pass the same files unchanged, using only a different adapter.

## 1. Files

| File | Content |
|------|---------|
| `protocol/vectors/framing_and_sequence.json` | corrupted, truncated, unknown-type, duplicate, stale and out-of-order messages |
| `protocol/vectors/fire_arbitration.json` | one vector per denial reason (only that condition fails), priority-order vectors, grant, continuous-check cuts |
| `protocol/vectors/zones.json` | every zone rejection reason, atomicity, partial upload, delete, list, clear, corrupted store |
| `protocol/vectors/timing.json` | one vector per timing rule |
| `protocol/reason-codes.json` | not a vector file: the permanent denial-reason code table (name to number), used by the runner to resolve names and guarded by the checks in section 8 |

Every vector file also records `protocol_version` (the protocol version it was written for). Vectors name reasons (for example `REQUEST_EXPIRED`) and never write reason numbers; priority vectors name rungs.

Coverage rule (FR-041, SC-017): a test fails if any denial reason, zone rejection code or timing rule in `contracts/protocol.md` has no vector whose `covers` names it. The same test also fails if any row of the "Leaving ZONE_SETUP" table (protocol.md 5.5, tags `setup:IN`, `setup:EXIT_1` to `setup:EXIT_8`) or any rule of protocol.md 5.2 and 5.6 (tags `key:*`) has no vector.

## 2. A vector

Fields:

| Field | Meaning |
|-------|---------|
| `id` | unique, e.g. `fire.deny.key_off` |
| `description` | one sentence in plain words |
| `covers` | list of tags: `deny:KEY_OFF`, `zone:ZONE_SELF_CROSSING`, `timing:watchdog_ms`, `framing:BAD_CRC`, `seq:DUPLICATE` |
| `config` | overrides of named timing and limit values (section 8 of the protocol) |
| `initial` | starting conditions (below) |
| `steps` | ordered list of timed steps |
| `expect` | final expectations |

`initial` (all optional; unlisted fields take the "booted, homed, disarmed, key off, no zones, heartbeat fresh and clear" default):
`state`, `homed`, `armed`, `key_on`, `pan_cdeg`, `tilt_cdeg`, `heartbeat_age_ms` (or `no_heartbeat`), `person_clear`, `person_clear_age_ms`, `clear_streak_ms` (how long the current clear period has lasted; the default is longer than `person_reengage_delay_ms`, so the re-engage delay has already elapsed), `zones` (list of `{zone_id, points}`), `zone_store_corrupt`, `zone_storage_unreadable`, `on_time_used_last_60s_ms`, `fault_mask`, `last_seq`.

`steps`, each with `at_ms` (time on the device clock since the start of the vector) and one `action`:

| action | Fields |
|--------|--------|
| `send` | `message` (see below), optional `expect_reply` |
| `send_raw` | `hex` (exact wire bytes, for corruption tests), optional `expect_reply` (`none` allowed) |
| `set_key` | `on` |
| `inject` | any subset of the `initial` fields, applied mid-vector (test-only, same guard as `initial`), e.g. `zone_store_corrupt` while a shot is running |
| `advance` | nothing; time moves to `at_ms`, device ticks |

`message`: `{ "type": "FIRE_REQUEST", "seq": 7, "fields": { "issued_at_ms": 1000, "ttl_ms": 500, ... } }`. The runner turns this into wire bytes with the reference encoder, so authors do not hand-compute CRCs. A `send_raw` step is used when the test is about the bytes themselves.

`expect_reply`: `{ "result": "DENIED", "code": "KEY_OFF", "data": { ... } }` or `"none"`.

`expect` (final): any of `state`, `pan_cdeg`, `tilt_cdeg`, `armed`, `homed`, `effector_on`, `zones` (list of ids), `zone_store_valid`, `fault_mask`, `events` (ordered list of device events such as `EFFECTOR_EVENT cut cause=PERSON_NOT_CLEAR` with the time it must occur by, e.g. `within_ms: 100`), `counters` (e.g. `rejected.BAD_CRC: 1`).

## 3. Illustrative example (shape only)

```json
{
  "id": "fire.deny.key_off",
  "description": "Only the key is off; everything else would allow firing.",
  "covers": ["deny:KEY_OFF"],
  "config": {},
  "initial": { "state": "SCANNING", "homed": true, "armed": true, "key_on": false },
  "steps": [
    { "at_ms": 0, "action": "send",
      "message": { "type": "FIRE_REQUEST", "seq": 1,
                   "fields": { "issued_at_ms": 0, "ttl_ms": 500, "intended_pan_cdeg": 12000,
                               "intended_tilt_cdeg": 4000, "duration_ms": 500 } },
      "expect_reply": { "result": "DENIED", "code": "KEY_OFF" } }
  ],
  "expect": { "effector_on": false }
}
```

Note: this start state is unreachable through normal commands (armed with the key off): turning the key off from an armed state sends the device to SAFE_ERROR (protocol.md 5.2). That is exactly why `initial` exists, and why it is test-only. In this vector the request arrives at `at_ms` 0, before any tick or key event could move the device out of the injected state, so rung 5 is the only failing condition.

## 4. The adapter interface (all a firmware harness must implement)

```text
DeviceAdapter
  reset(config)            -> start a fresh device with these named values
  apply_initial(initial)   -> put the device in the starting situation (test-only)
  send(bytes)              -> list of reply/event frames received before the next step
  advance_to(at_ms)        -> let device time reach at_ms (simulator: FakeClock; firmware: real waiting)
  set_key(on)              -> change the key input
  read_status()            -> the status block
  read_zone_ids()          -> zone ids stored (via ZONE_LIST)
```

The runner is written once (in this repository) and owns the encoder/decoder, the comparison and the report. A firmware harness supplies only the adapter. Timing-sensitive vectors use the `config` section to shorten waits where firmware allows.

## 5. Rules for authors

1. In each denial vector exactly one condition fails; every other condition is explicitly satisfied in `initial`.
2. A priority vector fails two or more conditions on purpose and expects the reason with the lower priority number (higher rung). Priority vectors name rungs (for example rung 7 over rung 8); the reply is checked against the reason's name, which the runner resolves to its permanent code.
3. Every vector is deterministic: no real clock, no randomness.
4. New protocol rule means a new vector in the same change.

## 6. Vectors required by the ZONE_SETUP exits and the key-off rule

Each vector below needs `initial` conditions from section 2 and is deterministic. "Store unchanged" means a `ZONE_LIST` step after the event returns the same zone ids and CRCs as before, and "upload gone" means the next `ZONE_POINT` or `ZONE_COMMIT` is rejected `ZONE_NO_UPLOAD`. All go in `protocol/vectors/zones.json` (setup) or `protocol/vectors/timing.json` / `fire_arbitration.json` (key and recovery) as noted.

### 6.1 ZONE_SETUP table (file `zones.json`)

| Vector id | Table row | What it does | Must show |
|-----------|-----------|--------------|-----------|
| zone.setup.in.begin_accepted | IN | One stored zone; `ZONE_BEGIN` from DISARMED, homed | state ZONE_SETUP; fire request now denied `ZONE_SETUP`; stored zones unchanged |
| zone.setup.exit1.commit_ok | 1 | BEGIN, all points, correct CRC-32 | zone listed with its CRC; state DISARMED; upload gone |
| zone.setup.exit2.commit_bad_checksum | 2 | Wrong CRC-32 | `ZONE_BAD_CHECKSUM`; store unchanged; state DISARMED; upload gone |
| zone.setup.exit2.commit_self_crossing | 2 | Bow-tie polygon | `ZONE_SELF_CROSSING`; store unchanged; state DISARMED |
| zone.setup.exit2.commit_incomplete | 2 | Commit with a point missing | `ZONE_INCOMPLETE`; store unchanged; state DISARMED |
| zone.setup.exit2.commit_storage_error | 2 | Storage mocked as full or locked | `ZONE_STORAGE_ERROR`; store unchanged; state DISARMED |
| zone.setup.exit3.idle_timeout | 3 | Two points, then silence past `zone_upload_timeout_ms` (config shortened) | state DISARMED; store unchanged; next `ZONE_POINT` gets `ZONE_NO_UPLOAD`; `advance` to just before the timeout shows state still ZONE_SETUP |
| zone.setup.exit4.disarm_aborts | 4 | Two points, then `DISARM` | reply OK; state DISARMED; store unchanged; upload gone |
| zone.setup.exit5.key_off | 5 | Two points, then `set_key off` | state SAFE_ERROR; fault KEY_OFF; armed false; upload gone; store unchanged |
| zone.setup.exit6.heartbeat_lost | 6 | Two points, then no heartbeat past `watchdog_ms` | state SAFE_ERROR; fault HEARTBEAT_LOST; upload gone; store unchanged |
| zone.setup.exit7.begin_replaces | 7 | BEGIN id 5 with two points, then BEGIN id 6 | state ZONE_SETUP; a commit for id 6 succeeds; points of id 5 never appear; no mixed zone |
| zone.setup.exit8.begin_rejected_from_disarmed | 8 | `ZONE_BEGIN` with 2 points from DISARMED | `ZONE_TOO_FEW_POINTS`; state still DISARMED (not entered) |
| zone.setup.exit8.begin_rejected_inside_setup | 8 | Upload in progress, then a rejected BEGIN (store full variant or too few points) | state still ZONE_SETUP; the earlier upload continues and its commit succeeds |
| zone.setup.nonexit.other_messages | not-exit list | In ZONE_SETUP: `ARM`, `HOME`, `FIRE_REQUEST`, `ZONE_LIST`, `MOVE`, `ZONE_POINT` with a bad index | `WRONG_STATE` for ARM and HOME, DENIED `ZONE_SETUP` for fire, bad index `ZONE_BAD_INDEX`; state and upload unchanged |
| zone.setup.begin_rejected_when_armed | IN guard | `ZONE_BEGIN` while SCANNING | `WRONG_STATE`; state unchanged |
| zone.setup.idle_timer_only_upload_messages | `timing:zone_upload_timeout_ms` | `zone_upload_timeout_ms` 5000; heartbeats every 200 ms throughout. `ZONE_BEGIN` at t = 0, `ZONE_POINT` index 0 at t = 3000, then at t = 4000 to 7000 a `STATUS_REQUEST`, `ZONE_LIST`, `MOVE` and a stale-seq message every 1000 ms | Still ZONE_SETUP at t = 7900 (only the point at 3000 reset the timer); state DISARMED at t = 8100; next `ZONE_POINT` gets `ZONE_NO_UPLOAD` |

### 6.2 Key off and SAFE_ERROR recovery (files `fire_arbitration.json` and `timing.json`)

| Vector id | Rule | What it does | Must show |
|-----------|------|--------------|-----------|
| key.off.in_zone_setup | key off, any state | Same as `zone.setup.exit5.key_off`; tagged `key:ANY_STATE` | see above |
| key.off.while_armed | key off, any state | State SCANNING, armed, key on; `set_key off` | state SAFE_ERROR; armed false; fault_mask has KEY_OFF; one `interlock` count with cause KEY_OFF; state is NOT DISARMED |
| key.off.while_firing | key off + effector cut | A granted shot is running (state DETER); `set_key off` | `EFFECTOR_EVENT cut cause=KEY_OFF` within 100 ms; effector off; state SAFE_ERROR |
| key.off.in_disarmed | one rule for every state | DISARMED, key on; `set_key off` | state SAFE_ERROR (same result as in the armed case) |
| key.off.at_boot_is_not_an_event | trigger is the change | Boot with key already off, homing completes | state DISARMED, no fault; `ARM` rejected `KEY_OFF` |
| key.recover.key_on_clears | recovery | After `key.off.while_armed`: `set_key on` with heartbeats continuing | state DISARMED; armed false; fault_mask 0; `FIRE_REQUEST` denied `NOT_ARMED` |
| key.recover.not_rearmed | never re-arms | Same, then wait 5 s | still DISARMED, armed false; only an `ARM` message arms (then state SCANNING) |
| key.recover.heartbeat_alone_not_enough | key fault needs the key | After key off: heartbeats continue, key stays off | state stays SAFE_ERROR; `FIRE_REQUEST` denied `KEY_OFF`; `ARM` rejected `KEY_OFF` |
| key.recover.key_alone_not_enough | heartbeat fault needs a heartbeat | After heartbeat loss: key on, no heartbeats | state stays SAFE_ERROR |
| key.recover.both_faults | one recovery rule | Key off and heartbeat lost together; key back on first, heartbeat later | stays SAFE_ERROR until both conditions are true, then DISARMED |
| key.recover.key_on_but_fault_unclear | priority note | Key back on while another fault bit (injected, non-clearing) is set | stays SAFE_ERROR; `FIRE_REQUEST` denied `FAULT_ACTIVE` |
| key.disarm_in_safe_error | DISARM semantics | In SAFE_ERROR (key off): `DISARM` | reply OK; state stays SAFE_ERROR |
| disarm.safe_error_heartbeat_lost_stays | `disarm:SAFE_ERROR_NO_CHANGE` | SAFE_ERROR entered by heartbeat loss (key on); `DISARM` with no heartbeat since | reply OK; state stays SAFE_ERROR; fault_mask still has HEARTBEAT_LOST; armed false |
| disarm.during_homing_continues | `disarm:HOMING_NO_CHANGE` | `homing_duration_ms` 3000; boot at t = 0; heartbeats every 200 ms; `DISARM` at t = 1000; `advance` to 2900 and 3100 | reply OK at t = 1000; state HOMING at t = 1000 and t = 2900 (homing not abandoned); state DISARMED at t = 3100 with `homed` true, armed false, fault_mask 0; exactly one homing-drift event |

Tags to use in `covers`: `setup:IN`, `setup:EXIT_1` .. `setup:EXIT_8`, `setup:NONEXIT`, `key:ANY_STATE`, `key:EDGE_NOT_LEVEL`, `key:RECOVERY`, `key:NEVER_REARMS`, `key:FAULT_PRIORITY`.

## 7. Person inhibit, denials-not-faults, store validity and reserved faults

Ladder numbers are those in protocol.md section 6 (14 rungs). Unless a vector says otherwise it starts from "homed, armed, SCANNING, key on, store empty, heartbeats clear and fresh, clear period older than the re-engage delay", with a `FIRE_REQUEST` of 500 ms whose TTL has not expired. "No state change" means `expect` lists the same `state`, `armed` true, `fault_mask` 0. Files: `fire_arbitration.json` (all person, zone, elevation and store vectors), `timing.json` (the two timing tags), `framing_and_sequence.json` is unchanged.

Timing note for authors: with the default values the status hold-off and the heartbeat watchdog are both 1000 ms, so a silent laptop is reported first as `HEARTBEAT_LOST` (rung 1). Vectors that need a stale-but-not-lost status set `person_clear_holdoff_ms` below `watchdog_ms` in `config`.

### 7.1 Person reported (fire inhibit, not a fault)

| Vector id | Covers | What it does | Must show |
|-----------|--------|--------------|-----------|
| fire.deny.person_reported | `deny:PERSON_NOT_CLEAR`, `person:NO_STATE_CHANGE` | Latest heartbeat has `person_clear` 0; fire request | DENIED `PERSON_NOT_CLEAR` (rung 7); state, armed and `fault_mask` unchanged (no fault bit) |
| fire.cut.person_while_on | `person:CUT`, `timing:person_clear_holdoff_ms` | A granted shot is running (DETER); a heartbeat with `person_clear` 0 arrives | `EFFECTOR_EVENT cut cause=PERSON_NOT_CLEAR` within 100 ms; effector off; state SCANNING (shot ended), armed still true; `fault_mask` 0; not SAFE_ERROR; one interlock count |
| person.cut_shot_not_resumed | `person:NO_RESUME` | After the cut above the person is gone and time advances past the delay with heartbeats clear; then a new request | Effector stays off with no new request and no further `EFFECTOR_EVENT on`; a new `FIRE_REQUEST` after the delay is GRANTED and starts a new shot |
| person.stale_status | `deny:PERSON_NOT_CLEAR`, `timing:person_clear_holdoff_ms` | Config: hold-off 300, watchdog 1000. Clear heartbeats stop for 500 ms (status stale, heartbeat not lost); request during the gap | DENIED `PERSON_NOT_CLEAR`; no SAFE_ERROR |
| person.stale_restarts_delay | `person:RESTART`, `timing:person_reengage_delay_ms` | Same config with delay 2000. After the gap a clear heartbeat arrives at T; requests at T + 1999 and T + 2000 | T + 1999: DENIED `PERSON_CLEAR_DELAY`; T + 2000: GRANTED. The first clear period (before the gap) does not count |
| person.boot_no_status | `deny:HEARTBEAT_LOST` | Booted, no heartbeat accepted yet | DENIED `HEARTBEAT_LOST` (rung 1 first); after the first clear heartbeat, `PERSON_CLEAR_DELAY` until the delay |

### 7.2 Re-engage delay: exact boundary

| Vector id | Covers | What it does | Must show |
|-----------|--------|--------------|-----------|
| fire.deny.person_clear_delay | `deny:PERSON_CLEAR_DELAY`, `timing:person_reengage_delay_ms` | Person reported until t = 0, then clear heartbeats every 200 ms from t = 0 (first accepted clear at t = 0); request at t = delay - 1 | DENIED `PERSON_CLEAR_DELAY` (rung 8); no state change |
| person.delay.boundary_exact | `person:BOUNDARY` | Same, request at t = delay exactly (the heartbeat for that instant is processed first) | GRANTED, effector on, state DETER |
| person.delay.reappear_restarts | `person:RESTART` | Clear from t = 0; at t = 3000 a heartbeat with `person_clear` 0; at t = 3200 clear again. Requests at 3100, at 3200 + delay - 1, at 3200 + delay | 3100: `PERSON_NOT_CLEAR`; 3200 + delay - 1: `PERSON_CLEAR_DELAY`; 3200 + delay: GRANTED. The period before t = 3000 does not count |
| person.delay.zero_disables | `person:BOUNDARY` | Config `person_reengage_delay_ms` 0, first clear heartbeat then an immediate request | GRANTED (no `PERSON_CLEAR_DELAY`) |
| fire.priority.person_not_clear_over_delay | `priority:rung7_over_rung8` | Person reported and delay not elapsed | `PERSON_NOT_CLEAR` |
| fire.priority.delay_over_expired | `priority:rung8_over_rung9` | Delay not elapsed and TTL expired | `PERSON_CLEAR_DELAY` |

### 7.3 Zone-margin and elevation: denials, not faults

| Vector id | Covers | What it does | Must show |
|-----------|--------|--------------|-----------|
| fire.deny.in_zone_margin | `deny:IN_ZONE_MARGIN`, `denial:NO_FAULT` | Direction inside a stored zone; request | DENIED `IN_ZONE_MARGIN` (rung 12); no state change; `fault_mask` 0. Then `MOVE` outside the margin and a new request is GRANTED, proving nothing was latched |
| fire.deny.elevation_limit | `deny:ELEVATION_LIMIT`, `denial:NO_FAULT` | Config elevation max 4000, tilt 6000 | DENIED `ELEVATION_LIMIT` (rung 11); no state change; `fault_mask` 0 |
| fire.cut.zone_margin_while_on | `interlock:IN_ZONE_MARGIN` | Shot running; `MOVE` into a zone margin | Cut cause `IN_ZONE_MARGIN` within 100 ms; effector off; state SCANNING, armed true; `fault_mask` 0 |
| fire.ttl.not_rechecked_while_on | `interlock:NO_TTL_RECHECK` | `ttl_ms` 500, `duration_ms` 2000, `per_shot_ms` 2000; heartbeats clear every 200 ms; request issued at t = 0 and arriving at t = 0 is GRANTED; `advance` to t = 600, then to t = 1900, then to t = 2000 | At t = 600 and t = 1900: `effector_on` true, state DETER, no `EFFECTOR_EVENT cut`, zero interlock events. At t = 2000: `EFFECTOR_EVENT normal end`, state SCANNING. Nothing is cut with cause `REQUEST_EXPIRED` |
| fire.cut.elevation_while_on | `interlock:ELEVATION_LIMIT` | Shot running; `MOVE` beyond the elevation limit but inside the soft limit | Cut cause `ELEVATION_LIMIT`; state SCANNING, armed true; `fault_mask` 0 |

### 7.4 Zone-store validity (derived)

| Vector id | Covers | What it does | Must show |
|-----------|--------|--------------|-----------|
| store.empty_is_valid | `store:EMPTY_VALID` | No zones | Status `zone_store_valid` true; a request that passes all else is GRANTED |
| fire.deny.zone_store_invalid | `deny:ZONE_STORE_INVALID`, `store:NOT_FAULT_ACTIVE` | `zone_store_corrupt` true, all else passes | DENIED `ZONE_STORE_INVALID` (rung 10), **not** `FAULT_ACTIVE`; `fault_mask` 0; `zone_store_valid` false |
| store.invalid_cut_while_on | `interlock:ZONE_STORE_INVALID` | Shot running; `inject` `zone_store_corrupt` | Cut cause `ZONE_STORE_INVALID`; state SCANNING, armed true; `fault_mask` 0 |
| store.clear_recovers | `store:CLEAR_RECOVERS` | State DISARMED, store corrupt; `ZONE_CLEAR`; then `ARM`; then a request | `ZONE_CLEAR` OK; `zone_store_valid` true; request GRANTED |
| store.stays_invalid_without_clear | `store:NO_AUTO_CLEAR` | Store corrupt (bytes do not match their checksums); time advances 10 s with heartbeats; request | Still DENIED `ZONE_STORE_INVALID`; `zone_store_valid` false |
| store.unreadable_then_readable | `store:UNREADABLE`, `store:READABLE_AGAIN` | One valid zone stored. Request; `inject` `zone_storage_unreadable` true; request; `inject` `zone_storage_unreadable` false, **no** `ZONE_CLEAR`; request | While unreadable: DENIED `ZONE_STORE_INVALID`, `zone_store_valid` false (and a shot already running is cut with that cause). After readable with matching checksums: `zone_store_valid` true and a request that passes everything else is GRANTED, with no `ZONE_CLEAR` sent |
| store.unreadable_then_corrupt_stays_invalid | `store:UNREADABLE`, `store:NO_AUTO_CLEAR` | Zone stored; `inject` `zone_storage_unreadable` true; `inject` `zone_store_corrupt` true; then `inject` `zone_storage_unreadable` false; request; then (state DISARMED) `ZONE_CLEAR` and a new request after `ARM` | After readable again the bytes still fail their checksums: DENIED `ZONE_STORE_INVALID`. After `ZONE_CLEAR`: `zone_store_valid` true and the new request is GRANTED |

### 7.5 Live key level, and reserved fault bits

| Vector id | Covers | What it does | Must show |
|-----------|--------|--------------|-----------|
| fire.deny.key_off | `deny:KEY_OFF`, `key:LIVE_LEVEL` | Injected: armed, SCANNING, key off, **no** on-to-off change ever seen; request at t = 0 | DENIED `KEY_OFF` (rung 5), not `FAULT_ACTIVE`; no fault latched by the denial |
| fault.reserved_bits_stay_clear | `fault:RESERVED_CLEAR` | One long run: boot, home, arm, a granted shot, a person cut, a zone denial, a corrupt store and `ZONE_CLEAR`, key off and back on, heartbeat loss and recovery | At every step `fault_mask` has neither bit 2 (`HOMING_FAILED`) nor bit 3 (`INTERNAL`) |
| fire.deny.fault_active_reserved | `deny:FAULT_ACTIVE` | Injected `fault_mask` with bit 3 (`INTERNAL`) set (test-only, it cannot occur in this feature) | DENIED `FAULT_ACTIVE` (rung 2) with the mask in the data |

`covers` tags added by this section (`store:UNREADABLE`, `store:READABLE_AGAIN` also): `person:NO_STATE_CHANGE`, `person:CUT`, `person:NO_RESUME`, `person:RESTART`, `person:BOUNDARY`, `priority:rung7_over_rung8`, `priority:rung8_over_rung9`, `denial:NO_FAULT`, `disarm:HOMING_NO_CHANGE`, `disarm:SAFE_ERROR_NO_CHANGE`, `interlock:*` (including `interlock:NO_TTL_RECHECK`), `store:*`, `fault:RESERVED_CLEAR`, `key:LIVE_LEVEL`, and `deny:PERSON_CLEAR_DELAY`, which joins the denial coverage rule in section 1. The coverage test also requires a vector for `timing:person_reengage_delay_ms` and `timing:person_clear_holdoff_ms`, and `timing:zone_upload_timeout_ms`.

## 8. Reason-code table checks (permanent codes)

The permanent denial-reason codes (protocol.md section 6.1) are guarded by a coverage rule that runs with the vectors. It reads `protocol/reason-codes.json`, the table in protocol.md, and a locked snapshot kept with the tests (`tests/contract/reason_codes_lock.json`, the name-to-code pairs of the released protocol version). It fails if any of these is false:

1. **Unique**: no two reasons share a code and no two share a name; every code is an integer from 1 to 255.
2. **Document and file agree**: the Code column of the ladder table in protocol.md and `reason-codes.json` list exactly the same name-to-code pairs.
3. **Unchanged**: every pair in the locked snapshot appears in the table with the same name and the same code. A changed, reassigned or removed pair fails. A new reason is accepted only if its code is the next unused number above every locked code; the lock is then updated by appending the pair. Changing what an existing code means requires a new protocol version (MAJOR), and the check fails if the table changed meaning without `protocol_version` being raised.
4. **Priority is independent of code**: a test reorders the priorities of a copy of the ladder and confirms every code stays the same.
5. **Every reason has a vector**: each reason name has at least one `deny:<NAME>` vector (the existing denial coverage rule), and the runner compares the numeric `code` byte of every DENIED reply with the number that `reason-codes.json` gives for the expected reason name.
6. **Priority is not mistaken for code**: the denial vectors for reasons whose code differs from their priority (`PERSON_CLEAR_DELAY`, `REQUEST_EXPIRED`, `ZONE_STORE_INVALID`, `ELEVATION_LIMIT`, `IN_ZONE_MARGIN`, `ON_TIME_LIMIT`, `RATE_LIMIT`) carry the tag `codes:DIFFERS_FROM_PRIORITY`, so a device that put the priority position on the wire fails them.

Because vectors never contain reason numbers, a deliberate code change touches one place (the table, the file and the lock) and the vectors keep working.
