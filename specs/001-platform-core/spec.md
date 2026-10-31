# Feature Specification: Platform Core (No Hardware)

**Feature Branch**: `001-platform-core` (not yet created; see report)

**Created**: 2026-10-31

**Status**: Draft

**Input**: User description: "Feature 001: Platform core (no hardware). The software foundation of the pigeon deterrent system, running on the owner's laptop with no hardware attached. A software simulator stands in for the ESP32 and speaks the same command protocol the real device will use later. Includes the dashboard Overview shell with laptop health, structured logs, traces, metrics, local storage, the simulated ESP32 (full command set, zone store, fire arbitration), and a written protocol definition with machine-readable scenario test vectors."

## Overview

Every later feature (vision, motion, streaming, safety) builds on this one. It delivers four things:

1. A **dashboard Overview** that shows the laptop's health and a labelled shell for everything that arrives later.
2. A **foundation for seeing what the system does**: structured logs, traces of engagements, and metrics, all stored locally.
3. A **simulated ESP32** that behaves exactly as the real device must, including every fire decision.
4. A **written protocol** plus **scenario test vectors** that the simulator passes today and the real firmware must pass later.

The safety rule that shapes the whole feature: **the simulated ESP32 is the only place a fire decision is made.** The dashboard and the laptop never decide to fire. This is the same authority split the real hardware will have, so the safety logic is proven in software before any laser exists.

## Clarifications

### Session 2026-10-31

No open clarifications. Every gap was resolved with a documented default (see Assumptions). Values that will be tuned later (timings, limits) are named as configurable defaults, not hard-coded decisions.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - See laptop health live (Priority: P1)

The operator opens the dashboard Overview in a browser and sees the laptop's CPU load, GPU load, RAM use and temperatures update on their own, so they know the machine doing the detection work is healthy. Any value that cannot be read (for example no GPU sensor, or no temperature sensor on this laptop) shows the word "unavailable" in that slot; the rest of the page keeps working.

**Why this priority**: It is the smallest slice that proves the whole chain works end to end (collect, deliver, display), and it gives immediate value. Everything else on the Overview sits next to it.

**Independent Test**: Start the system on a laptop with nothing attached, open the Overview, and watch the values change. Then make a sensor unreadable (using a test stand-in) and confirm that slot reads "unavailable" while the others continue to update.

**Acceptance Scenarios**:

1. **Given** the system is running, **When** the operator opens the Overview, **Then** CPU load, GPU load, RAM use and temperatures are shown with units, and each refreshes at least every 2 seconds without the operator reloading the page.
2. **Given** the GPU sensor cannot be read, **When** the Overview is open, **Then** the GPU slot reads "unavailable", the other values keep updating, and the page does not show an error or freeze.
3. **Given** temperature sensors are not exposed by this laptop, **When** the Overview is open, **Then** the temperature slots read "unavailable" and nothing else is affected.
4. **Given** a value was readable and then becomes unreadable, **When** the next refresh happens, **Then** that slot changes to "unavailable" rather than continuing to show the old number as if it were current.

---

### User Story 2 - Overview shell with live simulator state (Priority: P2)

The operator sees the full Overview layout with clearly labelled placeholders for what later features will fill: the live camera feed with detection overlay, a recent-log list, and a Pi health panel that reads "not connected". Pan angle, tilt angle and system state already show live values coming from the simulated ESP32. The system state shown is one of: BOOT, HOMING, DISARMED, SCANNING, CANDIDATE, SLEW, CONFIRM, TRACK, DETER, COOLDOWN, ZONE_SETUP, SAFE/ERROR.

**Why this priority**: It fixes the layout the owner will build on, and it is the first place the simulator becomes visible to the operator. It depends on nothing but the simulator and the dashboard.

**Independent Test**: Open the Overview with the simulator running. Confirm each placeholder is labelled and says what will appear there. Command the simulator to move and change state (using the developer tooling from Story 6) and confirm pan, tilt and state follow within the stated time.

**Acceptance Scenarios**:

1. **Given** the Overview is open, **When** the operator looks at the page, **Then** the camera feed area is labelled as a placeholder for the live feed with detection overlay, and the Pi health panel reads "not connected".
2. **Given** the simulator is running, **When** the simulator's pan, tilt or state changes, **Then** the Overview shows the new value within 1 second.
3. **Given** the simulator reports a state, **When** the state is shown, **Then** it is one of the twelve named states and never a blank or an unnamed value.
4. **Given** the simulator is stopped, **When** the Overview is open, **Then** pan, tilt and state show "unavailable" (not the last known value) and the operator can tell the simulator is not running.
5. **Given** log entries exist, **When** the Overview is open, **Then** the recent-log list shows the newest entries and updates as new ones arrive.

---

### User Story 3 - Browse and filter structured logs (Priority: P3)

The operator opens a logs view and filters entries by component, by severity and by trace id, so they can follow what the system did and, by choosing one trace id, see everything that belonged to one engagement.

**Why this priority**: Logs are the first tool for understanding any behaviour. They are needed before the trace and metric views are useful, and they are available from day one for every component.

**Independent Test**: Run a simulated engagement, open the logs view, filter by one component, then by one severity, then by the engagement's trace id, and confirm each filter returns exactly the matching entries.

**Acceptance Scenarios**:

1. **Given** several components have logged, **When** the operator filters by one component, **Then** only that component's entries are shown.
2. **Given** entries of mixed severity exist, **When** the operator filters by a severity, **Then** only entries of that severity are shown.
3. **Given** a simulated engagement produced a trace id, **When** the operator filters by that trace id, **Then** exactly the entries belonging to that engagement are shown, in time order.
4. **Given** filters are combined, **When** applied, **Then** an entry must match all of them to appear.
5. **Given** no entries match, **When** filters are applied, **Then** the view says that nothing matched (not an error).

---

### User Story 4 - View an engagement as a timed trace (Priority: P4)

The operator opens the trace view for a simulated engagement and sees it as a sequence of timed steps: capture, encode, network, inference, decision, command, ESP32 acknowledgement, effector action. Each step shows when it started and how long it took, so latency can be broken down hop by hop.

**Why this priority**: The system's key engineering claim is end-to-end latency. The breakdown has to exist before real components feed it. In this feature the steps are produced by a simulated engagement.

**Independent Test**: Trigger a simulated engagement and open its trace. Confirm all eight steps appear in order, with durations, and that the total equals the end-to-end time.

**Acceptance Scenarios**:

1. **Given** a simulated engagement has run, **When** the operator opens its trace, **Then** the eight named steps appear in order, each with a start time and a duration.
2. **Given** a trace is shown, **When** the operator reads the total, **Then** it equals the time from the start of the first step to the end of the last, and no step overlaps in a way that makes the total inconsistent.
3. **Given** a fire request in the engagement was denied, **When** the trace is viewed, **Then** the ESP32 acknowledgement step shows the denial and its reason, and the effector action step shows that no action happened.
4. **Given** a step did not complete (for example the acknowledgement never arrived), **When** the trace is viewed, **Then** that step is shown as incomplete or timed out instead of being omitted.
5. **Given** the operator picks a trace id from the logs view, **When** they open the trace, **Then** it is the same engagement.

---

### User Story 5 - View metrics on an Observability page (Priority: P5)

The operator opens an Observability page and sees metrics of three kinds: counters (things that happened, such as fire denials by reason), gauges (current values, such as pan angle or CPU load) and latency distributions that show the median (p50) and the 95th percentile (p95).

**Why this priority**: It completes the observability foundation (logs, traces, metrics) and gives the owner the p50/p95 view that the success metrics rely on.

**Independent Test**: Run simulated engagements, open the page, and compare the counter, gauge and p50/p95 figures against values computed independently from the same recorded data.

**Acceptance Scenarios**:

1. **Given** simulated engagements have run, **When** the operator opens the page, **Then** counters, gauges and latency distributions are shown, each with a name and unit.
2. **Given** latency samples exist, **When** the distribution is shown, **Then** p50 and p95 are displayed and match an independent calculation on the same samples.
3. **Given** a fire request was denied, **When** the page is viewed, **Then** a denial counter for that reason has increased by one.
4. **Given** no samples exist yet, **When** the page is opened, **Then** it shows an empty state, not zeros presented as real measurements.

---

### User Story 6 - Run a simulated ESP32 with the full command set (Priority: P6)

The developer starts a simulated ESP32 that accepts every command the real device will, validates each one, and replies exactly as the real device must. It holds its own state, angles, faults, key state and zone store, and it alone decides whether a fire request is granted or denied, always giving a specific reason when it denies.

The commands are: home, move, jog, heartbeat, arm and disarm (with key state), upload zone (points, then a commit with a checksum), delete zone, list zones, clear zones, and fire request.

Heartbeats carry a person-clear status. Fire requests carry a time-to-live. A fire request is granted only if every condition holds; otherwise it is denied with a specific reason.

**Why this priority**: This is the safety-critical heart of the feature and the reason the protocol is proven in software. It is listed after the operator views only because it is developer-facing; Stories 2, 4 and 5 all show its output. It is independently testable with no dashboard at all.

**Independent Test**: Run the simulator on its own and drive it with test messages. Check every command's reply, every denial reason, and the zone rules, with no dashboard, camera, Pi or motor present.

**Acceptance Scenarios**:

1. **Given** the simulator starts, **When** it boots, **Then** it is in BOOT, moves to HOMING, completes homing and ends in DISARMED. It is never armed on boot.
2. **Given** the simulator is homing, **When** a move, jog, arm or fire request arrives, **Then** it is refused with a specific reason and no angle changes. After homing completes, motion commands work.
3. **Given** it is homed and disarmed, **When** a valid move or jog arrives, **Then** it is accepted, the angles change within the soft angle limits, and the new angles are reported. A move beyond the soft limits is refused with a specific reason.
4. **Given** it is homed and the key is on, **When** the dashboard arm command arrives, **Then** it is armed. With the key off, arming is refused with a specific reason.
5. **Given** all fire conditions hold, **When** a fire request arrives, **Then** it is granted and the simulated effector turns on.
6. **Given** any one condition fails, **When** a fire request arrives, **Then** it is denied with the reason for that condition (see the Fire Denial Reasons in the requirements).
7. **Given** the latest heartbeat reports a person, or no person-clear status has arrived within the hold-off time, **When** a fire request arrives, **Then** it is denied with reason PERSON_NOT_CLEAR.
8. **Given** a fire request whose time-to-live has passed by the time it arrives, **When** it is processed, **Then** it is denied with reason REQUEST_EXPIRED.
9. **Given** the simulated effector is on, **When** any fire condition lapses (heartbeat lost, person reported, key turned off, beam enters a margin region, on-time limit reached), **Then** the effector is cut immediately and the event is logged and counted.
10. **Given** the heartbeat stops, **When** the watchdog time passes, **Then** motion stops, the effector is off, and the state becomes SAFE/ERROR with a fault reported.
11. **Given** a valid zone upload (points, then commit with a correct checksum), **When** committed, **Then** the zone is stored whole and appears in the list.
12. **Given** an upload that is incomplete, has a wrong checksum, has fewer than 3 points, or self-crosses, **When** committed or abandoned, **Then** nothing is stored, the previously stored zones are unchanged, and a specific error is returned.
13. **Given** zones exist, **When** a delete-zone, list-zones or clear-zones command arrives, **Then** it acts on whole zones only, and the reply reflects the resulting store.
14. **Given** the simulated zone store is corrupted, **When** any fire request arrives, **Then** it is denied with reason ZONE_STORE_INVALID until the store is cleared and valid again.
15. **Given** any state, **When** the simulator is asked for its status, **Then** it reports its state, pan and tilt angles, key state, armed flag, homed flag, active faults and zone-store validity.

---

### User Story 7 - Written protocol and scenario test vectors (Priority: P7)

The developer has a written protocol definition covering message types, framing, sequence numbers, checksums, error and denial codes, and timing rules (heartbeat interval, heartbeat-loss watchdog, person-clear hold-off, command and fire-request time-to-live). It also has a machine-readable file of scenario test vectors (zone cases and fire-arbitration cases, each with the expected outcome). The simulator implements the protocol and passes every vector today. The future firmware must implement the same protocol and pass the same vectors.

**Why this priority**: It makes the simulator and the future firmware provably interchangeable. Without it, "same protocol" is only a hope.

**Independent Test**: Run every vector against the simulator and confirm all pass. Then read the protocol document alone and confirm a reader can build a valid message and predict the reply for any vector without reading the simulator.

**Acceptance Scenarios**:

1. **Given** the protocol document, **When** a reader looks up any message type, **Then** its fields, framing, sequence number rule, checksum rule and every possible reply and error code are defined.
2. **Given** the vector file, **When** each vector is run against the simulator, **Then** its actual outcome equals its expected outcome, and all vectors pass.
3. **Given** the vector file, **When** inspected, **Then** it contains at least one vector for every fire-denial reason, every zone rejection reason, and every timing rule.
4. **Given** the vectors are machine-readable, **When** the future firmware test harness reads them, **Then** it needs no changes to the vectors, only an adapter to send messages to its own device.
5. **Given** a new rule is added to the protocol, **When** it is added, **Then** the document and vectors change together and a vector covers it.

---

### Edge Cases

**Laptop health**
- GPU or temperature sensors are unavailable: the slot reads "unavailable"; the page and other values are unaffected (Story 1).
- A sensor read is slow or hangs: the rest of the page still refreshes on time; that slot shows "unavailable" for the refresh in which it did not respond.

**Dashboard connection**
- The dashboard disconnects (browser closed, laptop sleeps, backend restarts) and reconnects: on reconnect it shows current values again without a manual reload, and shows a clear "disconnected" indicator while the link is down. Stored logs, traces and metrics are not lost by a disconnect.
- Several dashboard tabs are open at once: each shows the same live values.

**Protocol messages**
- Corrupted message (bad checksum, bad framing, truncated, unknown type): rejected, never acted on, counted and logged. Every corrupted message is rejected.
- Duplicated message (same sequence number repeated): processed at most once; a duplicate gets the same reply as the original and causes no second action. A duplicated fire request never causes two grants.
- Out-of-order message (older sequence number arriving after a newer one): rejected as stale, not applied.
- A fire request that arrives late (Wi-Fi delay): denied as expired.

**Heartbeat**
- Heartbeat lost: watchdog stops motion and effector, state becomes SAFE/ERROR. A restored heartbeat does not silently re-arm; the device returns to DISARMED and must be re-armed deliberately.
- Heartbeats arrive but all report a person, or stop carrying a clear status: fire requests are denied, and the effector is cut if on.

**Zones**
- Partial zone upload (connection lost between points and commit): the partial upload is discarded; the stored zones are unchanged; a later fresh upload starts clean.
- Commit with a wrong checksum: rejected; nothing stored.
- Zone with 2 or fewer points, or self-crossing edges: rejected with a specific reason.
- A new upload begins while another is in progress: defined and tested behaviour (the earlier partial upload is discarded), never a mixed zone.
- Store full (maximum zones or points reached): upload rejected with a specific reason; existing zones untouched.
- Deleting an unknown zone id: specific error, store unchanged.
- Corrupted zone store at start or detected at run time: all firing blocked.

**Fire requests in the wrong state**
- While HOMING: denied (device not homed); no motion.
- While DISARMED: denied (not armed).
- While in ZONE_SETUP: denied; the effector is hard-disabled in this state.
- While in SAFE/ERROR: denied with the active fault.
- Several conditions fail at once: exactly one reason is returned, chosen by a documented priority order, and the test for each reason makes only that condition fail.

**Storage**
- Local storage full or locked: the system keeps running, shows a visible storage warning on the dashboard, keeps recent entries in memory, retries, and logs the problem. It never crashes or blocks the live views, and it never blocks the simulator's safety decisions.
- Simulated device zone-store storage full or locked: the upload is rejected with a storage error and the previous zones remain intact.

## Requirements *(mandatory)*

### Functional Requirements

**Laptop health and Overview**

- **FR-001**: The system MUST read and display the laptop's CPU load, GPU load, RAM use and temperatures on the Overview, refreshed automatically at least every 2 seconds.
- **FR-002**: A value that cannot be read MUST be shown as "unavailable"; it MUST NOT cause an error, freeze the page or show a stale number as current.
- **FR-003**: The Overview MUST show labelled placeholders for: the live camera feed with detection overlay, pan angle, tilt angle, system state, a recent-log list, and a Pi health panel reading "not connected".
- **FR-004**: Pan angle, tilt angle and system state on the Overview MUST show live values from the simulated ESP32, and "unavailable" when the simulator is not running.
- **FR-005**: The system state shown MUST be one of BOOT, HOMING, DISARMED, SCANNING, CANDIDATE, SLEW, CONFIRM, TRACK, DETER, COOLDOWN, ZONE_SETUP, SAFE/ERROR.
- **FR-006**: The dashboard MUST show a visible connection indicator, recover automatically after a disconnect, and show current values again without a manual reload.

**Observability foundation**

- **FR-007**: Every component MUST emit structured logs with timestamp, component, severity and, where applicable, trace id and span id.
- **FR-008**: The logs view MUST filter by component, severity and trace id, alone or combined.
- **FR-009**: A simulated engagement MUST be recorded as one trace with eight steps in order: capture, encode, network, inference, decision, command, ESP32 acknowledgement, effector action. Each step MUST record a start time and a duration, and an incomplete step MUST be shown as such.
- **FR-010**: The system MUST record counters, gauges and latency distributions, and the Observability page MUST show p50 and p95 for latency distributions.
- **FR-011**: Metrics MUST at least include: laptop CPU/GPU/RAM/temperatures, simulated end-to-end latency, fire requests granted and denied (by reason), interlock events, rejected protocol messages (by cause) and simulated homing drift.
- **FR-012**: Logs, traces and metrics MUST be stored locally and survive a restart of the system, and all of it MUST stay on the laptop.
- **FR-013**: When local storage is full or locked, the system MUST keep running, show a visible warning, retry, and log the fault. It MUST NOT crash, block live views or affect the simulator's safety decisions.
- **FR-014**: Log, trace and metric records MUST NOT contain secrets or raw camera footage.

**Simulated ESP32 and safety authority**

- **FR-015**: A software simulator of the ESP32 MUST accept the full command set: home, move, jog, heartbeat, arm, disarm, upload zone (points then commit), delete zone, list zones, clear zones, and fire request.
- **FR-016**: The simulator MUST validate every message (framing, checksum, sequence number, known type, field ranges) and MUST reject any invalid message without acting on it.
- **FR-017**: The simulator MUST start in DISARMED after booting and homing, and MUST NOT accept motion or arming before homing completes.
- **FR-018**: The simulator MUST enforce soft angle limits (pan 240 degrees, tilt 80 degrees of travel) and refuse moves beyond them.
- **FR-019**: The simulator MUST accept a simulated key state as an input that a developer or test can set, and MUST report the key state.
- **FR-020**: Arming MUST require the key on and the device homed; otherwise it MUST be refused with a specific reason. Disarming MUST always be accepted.
- **FR-021**: The simulator MUST be the only component that decides whether a fire request is granted. The dashboard, the laptop backend and any other component MUST NOT contain a code path that grants or performs a fire, and the laptop side MUST treat the simulator's reply as the only authority.
- **FR-022**: A fire request MUST be granted only when ALL of these are true: the key is on; the device is armed; the device is homed; the heartbeat is alive; the latest heartbeat reports the person clear and arrived within the hold-off time; the request's time-to-live has not expired; the beam direction is within the elevation limits and outside every stored zone expanded by the safety margin; the zone store is valid; the device is not in ZONE_SETUP; and the per-shot and per-minute on-time limits are not exceeded. Otherwise it MUST be denied with a specific reason.
- **FR-023**: Fire Denial Reasons: the protocol MUST define a distinct, named reason for each failed condition, at least: KEY_OFF, NOT_ARMED, NOT_HOMED, HEARTBEAT_LOST, PERSON_NOT_CLEAR, REQUEST_EXPIRED, ELEVATION_LIMIT, IN_ZONE_MARGIN, ZONE_STORE_INVALID, ZONE_SETUP, ON_TIME_LIMIT, RATE_LIMIT, FAULT_ACTIVE. When more than one fails, exactly one reason is returned according to a documented priority order.
- **FR-024**: Every heartbeat MUST carry a person-clear status, a sequence number and a timestamp. The simulator MUST treat a missing or old person-clear status as "person present" (fail closed) once the hold-off time has passed.
- **FR-025**: Every fire request MUST carry a time-to-live; the simulator MUST deny any request that arrives after its time-to-live has expired with REQUEST_EXPIRED.
- **FR-026**: While the simulated effector is on, the simulator MUST keep checking the fire conditions and cut the effector at once when any of them lapses, logging the event and counting it as an interlock event.
- **FR-027**: A heartbeat watchdog MUST stop motion and switch the effector off when the heartbeat is lost, and move the state to SAFE/ERROR with a reported fault. Recovery MUST NOT re-arm automatically.
- **FR-028**: The simulator MUST report its state, pan and tilt angles, key state, armed flag, homed flag, zone-store validity and active faults on request and when they change.
- **FR-029**: The simulator MUST process a duplicated message at most once (same reply, no second action) and MUST reject an out-of-order (stale) message.

**Zones**

- **FR-030**: Zones MUST be stored only in the simulated ESP32, as pan/tilt angles relative to the homed zero. They MUST NOT be stored in camera pixels, and the laptop MUST NOT hold an authoritative copy; the dashboard shows what the simulator reports back.
- **FR-031**: A zone upload MUST consist of points followed by a commit carrying a checksum. It MUST be atomic: either the whole zone is stored or nothing changes. An incomplete, abandoned or checksum-failed upload MUST be discarded.
- **FR-032**: A zone MUST have at least 3 points. A zone whose edges cross each other MUST be rejected. Each rejection MUST return a specific reason.
- **FR-033**: Each zone MUST be independent and deleted as a whole. Zones MUST NOT be editable; they are recreated.
- **FR-034**: The safety margin around each zone MUST be 0.5 degrees plus the measured aiming error. In this feature the aiming error is a configurable simulated value.
- **FR-035**: A corrupted zone store MUST block all firing until it is cleared or restored to a valid state.
- **FR-036**: The zone store MUST persist across a simulator restart (to model the real device's persistent memory), be held separately from the laptop's observability data, and be limited to a maximum number of zones and points per zone, with the limit configurable.
- **FR-037**: If the simulated device's zone storage is full or locked, the upload MUST be rejected with a specific reason and the existing zones MUST stay intact.

**Protocol and test vectors**

- **FR-038**: A written protocol definition MUST exist in the repository covering message types, framing, sequence numbers, checksums, all error and denial codes, and timing rules (heartbeat interval, heartbeat-loss watchdog, person-clear hold-off, command and fire-request time-to-live).
- **FR-039**: A machine-readable file of scenario test vectors MUST exist, covering zone cases and fire-arbitration cases, each with its input messages, device starting conditions and expected outcome.
- **FR-040**: The simulator MUST pass every vector. A change to the protocol MUST change the document and the vectors together.
- **FR-041**: The vectors MUST contain at least one case for every fire-denial reason, every zone rejection reason, and every timing rule.
- **FR-042**: The vectors and protocol document MUST be written so that the future firmware can use them unchanged.

**Testability and boundaries**

- **FR-043**: The whole feature MUST run and be tested on the laptop with no hardware attached.
- **FR-044**: Every hardware-facing interface (camera, network, serial link to the device, laptop sensors) MUST sit behind an abstraction with a mock implementation that tests can control.
- **FR-045**: Automated tests MUST be written before the implementation of each task, and safety logic MUST have automated tests.
- **FR-046**: Zone geometry (point-in-polygon with margin, and self-crossing detection) MUST be unit-tested.
- **FR-047**: A developer-triggered simulated engagement MUST exist to produce traces, logs and metrics, so that Stories 3 to 5 can be exercised without a camera. It MUST obtain its fire decision only from the simulator.

### Key Entities

- **Health reading**: one measurement of CPU load, GPU load, RAM use or a temperature, with a time and a state (available or unavailable).
- **Log entry**: a structured record with timestamp, component, severity, message and, where applicable, trace id and span id.
- **Trace**: one engagement, identified by a trace id, made of ordered steps with a total duration.
- **Step (span)**: one timed part of a trace (capture, encode, network, inference, decision, command, ESP32 acknowledgement, effector action), with start, duration and outcome.
- **Metric**: a named counter, gauge or latency distribution, with unit and, for distributions, p50 and p95.
- **Simulated device**: the stand-in ESP32, holding state, pan/tilt angles, key state, armed flag, homed flag, faults, effector status, on-time usage and the zone store.
- **Command message / reply**: a framed message with type, sequence number, payload and checksum, and the device's reply (accepted, rejected with an error code, granted, or denied with a reason).
- **Heartbeat**: a periodic message carrying a person-clear status, a sequence number and a timestamp.
- **Fire request**: a request to fire with a sequence number, a time-to-live and the beam direction; always answered granted or denied.
- **Zone**: an independent polygon of at least 3 pan/tilt points, with an id and a checksum, stored only in the simulated device.
- **Zone store**: the set of zones plus its validity flag.
- **Scenario test vector**: a recorded case with starting conditions, input messages and expected outcome.
- **Protocol definition**: the written rules that the simulator and the future firmware must both follow.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Laptop health values on the Overview refresh at least every 2 seconds, measured over a 5-minute run with no missed refresh longer than 4 seconds.
- **SC-002**: The Overview shows data within 3 seconds of the operator loading the page.
- **SC-003**: When any one health sensor is unreadable, 100% of the other values keep updating on schedule and the slot reads "unavailable" (tested for GPU and for temperatures).
- **SC-004**: A change in the simulator's pan, tilt or state appears on the Overview within 1 second.
- **SC-005**: After the dashboard link drops and returns, the Overview shows current values again within 5 seconds with no manual reload.
- **SC-006**: 100% of corrupted, truncated and unknown-type protocol messages in the test set are rejected without changing device state.
- **SC-007**: 100% of duplicated messages cause no second action, and 100% of out-of-order messages are rejected.
- **SC-008**: Every fire-denial reason in the protocol has at least one automated test in which only that condition fails and the reply names that reason.
- **SC-009**: In every test where any fire condition fails, 0 fire requests are granted, and in every test where all conditions hold, the request is granted.
- **SC-010**: With the effector on, 100% of tests that lapse a condition (heartbeat lost, person reported, key off, margin entered, on-time limit) cut the effector within 100 milliseconds of simulated time.
- **SC-011**: 100% of partial, wrong-checksum, too-few-point and self-crossing zone uploads leave the stored zones unchanged.
- **SC-012**: A corrupted zone store results in 0 granted fire requests across the full test run.
- **SC-013**: The operator can find all log entries of one engagement by trace id in under 10 seconds, and filters return exactly the matching entries in 100% of test cases.
- **SC-014**: A trace of a simulated engagement shows all eight steps with durations that add up to the shown total, in 100% of test engagements.
- **SC-015**: The p50 and p95 values on the Observability page match an independent calculation on the same samples in 100% of test cases.
- **SC-016**: When local storage is full or locked, the live views keep working and a warning appears within 10 seconds, with 0 crashes in the test run.
- **SC-017**: 100% of scenario test vectors pass against the simulator, and the vectors include at least one case for every denial reason, zone rejection reason and timing rule.
- **SC-018**: The full automated test suite passes on the owner's laptop with no hardware attached, and a new reader can build a valid message from the protocol document alone and predict the simulator's reply for a sample of 5 vectors.
- **SC-019**: 0 code paths outside the simulated ESP32 can grant or perform a fire, verified by a review and an automated check of the laptop and dashboard code.

## Assumptions

- **Users**: The only user is the owner, working on one Windows 11 laptop, with the dashboard open in a browser on the same laptop. No login is needed because nothing is reachable from outside the home network (constitution Out of Scope).
- **No Pi in this feature**: The simulator stands in for both the ESP32 and, for protocol purposes, the Pi relay. The laptop talks to the simulator directly. The "Pi heartbeat alive" condition is modelled as "laptop heartbeat alive", as the heartbeat carries the person-clear status. The relay path through the Pi is built in later features.
- **Fire requests come only from test and developer tooling**: In this feature no component decides to fire. Fire requests are issued by automated tests and by a developer-triggered simulated engagement, and the simulator is the only decider. There is no Arm or Fire button on the Overview; the arm and key interface belongs to the Safety page in feature 006.
- **Simulated key and effector**: The key state is an input that tests or a developer set, and the effector is a simulated on/off state with on-time tracking. No real laser, key or emergency stop exists.
- **Configurable defaults (provisional)**: Heartbeat interval 200 ms; heartbeat-loss watchdog 1 s; person-clear hold-off 1 s; fire-request time-to-live 500 ms; per-shot on-time 2 s; per-minute on-time 10 s; simulated aiming error 0.2 degrees. These are starting values, set in the protocol document, tunable without changing the protocol, and re-baselined in features 003 and 006. Every value is named in the document so tests can set it.
- **Zone limits**: Defaults of 8 zones and 16 points per zone, taken from the suggestion in docs/system-scope.md. The final limits remain open decision 5 and are set in the plan.
- **Aiming error**: Real aiming error is measured in a later feature. Here it is a configurable constant so the margin rule (0.5 degrees plus aiming error) is already tested.
- **Elevation limits**: The elevation limits depend on device placement (open decision 1). Here they are configurable simulated limits.
- **Homing**: The simulator's homing is a simulated delay with a simulated drift value. The sensor type and real procedure belong to feature 003. Periodic re-homing every 10 minutes is outside this feature, but the simulated drift metric exists.
- **Zone persistence**: The simulated zone store is saved in storage kept apart from the laptop's observability data, so it behaves like the device's own persistent memory and the laptop never reads it directly.
- **Retention**: Logs, traces and metrics are kept locally with no deletion in this feature. A retention policy comes later with the analytics feature.
- **Metrics and engagement**: The simulated engagement uses made-up timings for the camera and detection steps, because no camera or detector exists yet. These timings are labelled as simulated everywhere they are shown.
- **Sensors**: Laptop temperature readings may be partly or wholly unavailable on some laptops, so "unavailable" is a normal, expected display state, not an error.
- **Technology**: The technology stack is fixed by the constitution and chosen in the plan. This spec deliberately names none.

## Out of Scope

Real camera video, detection, the Raspberry Pi, real motors, calibration, scan policy, census sweeps, the no-fire zone drawing page, the Safety page, and analytics pages beyond logs, traces and metrics.

## Constitution Compliance

| Principle | How this spec satisfies it |
|-----------|----------------------------|
| I. Spec-driven, one task at a time | This is the approved-spec step before plan and tasks. Tests-first is required (FR-045). The feature uses its own branch. |
| II. Reporting and explainability | The report follows CLAUDE.md. The protocol and vectors are written so the owner can explain each module (FR-038 to FR-042). |
| III. Safety in layers, ESP32 authority | The simulator alone decides fire (FR-021, FR-022). Default DISARMED (FR-017). Full arbitration list and specific denial reasons (FR-022, FR-023). Fail-closed person inhibit and time-to-live (FR-024, FR-025). Continuous checking and effector cut (FR-026). Heartbeat watchdog and soft limits (FR-018, FR-027). Zone rules: stored only on the device, angles, atomic checksum, minimum 3 points, self-crossing rejected, 0.5 degrees plus aiming error margin, corrupted store blocks firing (FR-030 to FR-037). Laptop-side checks are never a safety layer (FR-021). The key and cut-off hardware, emergency stop and eye-safety rules are not in scope here. |
| IV. Homing | The simulator homes on boot and cannot arm or move before it (FR-017). Simulated drift is a metric (FR-011). Real homing, periodic re-homing and sensor choice belong to feature 003. |
| V. Observability by default | Structured logs for every component (FR-007), eight-step traces (FR-009), p50/p95 metrics (FR-010, FR-011), local storage and dashboard views (FR-012). |
| VI. Testable without hardware | Mocks for every hardware-facing interface (FR-044), a simulator speaking the protocol with zone store and arbitration (FR-015 to FR-037), tests run on the laptop (FR-043), zone geometry unit-tested (FR-046). The replay harness belongs to feature 002. |
| VII. Measured, honest results | Success criteria are measured numbers; simulated timings are labelled as simulated (Assumptions). Census and deterrence evaluation are out of scope. |
| VIII. Privacy | All data stays local (FR-012); no footage or secrets in records (FR-014). |
