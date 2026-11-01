# Decision Log: Autonomous Feral Pigeon Deterrent System

Started 2026-10-30. This file records every project decision and the reason for it. Add new entries at the bottom of the right section. Never delete an entry. If a decision changes, mark the old one SUPERSEDED and add a new one.

Status labels: DECIDED = confirmed by the owner. PROVISIONAL = placeholder, easy to change. OPEN = not yet settled.

## A. Goal and approach
- D-01 [DECIDED] Build the autonomous pigeon deterrent as the main portfolio project: detect, aim, deter, and measure. Considered and set aside for now: an instrumented-hardware project (sensor rig, exoskeleton). Why: the idea came from the owner's own building, and it covers vision, embedded control, mechanism and measured results. Note: it serves robotics-software roles better than pure hardware roles.
- D-02 [DECIDED] The project must produce measured results, not only a demo. Engineering metrics and the deterrence outcome are reported honestly, even if the deterrent does not work well.
- D-03 [PROVISIONAL] About 3 hours of build time per day. Target: full demo in late January, with a December milestone of detection on recorded footage and bench motion. Not explicitly confirmed by the owner.

## B. Process and tools
- D-04 [DECIDED] Use Spec-Driven Development with GitHub Spec Kit: constitution, then spec, plan, tasks, and implementation one task at a time, testing each task. Why: a structured path that is easy to explain to an interviewer.
- D-05 [DECIDED] Claude Code in VS Code does the coding. The claude.ai Project was used for planning and review. From 2026-10-31 the owner works mainly in Claude Code.
- D-06 [DECIDED] Public GitHub repository named autonomous-feral-pigeon-deterrent (full name kept). Why: it is the portfolio shown to recruiters.
- D-07 [DECIDED] One branch per feature, one commit per task, Conventional Commit messages, and commits only when the owner asks.
- D-08 [DECIDED] Every Claude Code reply follows the report format in CLAUDE.md. The owner compares the report's file list with the Source Control panel before committing.
- D-09 [DECIDED] One task per session, tests written first, and a fresh chat for each planning command and each task.
- D-10 [DECIDED] Tools: Python 3.12 managed with uv; Spec Kit 1.2.5 with the Claude integration (commands are written /speckit-name); PowerShell script policy set to RemoteSigned for the current user. Why Python 3.12: Python 3.15 could not build one of Spec Kit's packages.
- D-11 [DECIDED] The constitution and docs/system-scope.md are the source of truth. A conflict stops work and is reported to the owner.
- D-12 [DECIDED] Features are built software-first: 001 platform core, 002 vision on recorded footage, 003 motion core, 004 streaming and calibration, 005 engagement loop, 006 safety and effector, 007 analytics, 008 field experiment. Why: the software sets the requirements for the hardware, and hardware is bought as late as possible.
- D-13 [DECIDED] The owner must be able to explain every module. Claude Code explains each task in plain words and this log records each decision.

## C. Architecture
- D-14 [DECIDED] Roles: Raspberry Pi 5 (2 GB) captures, encodes, streams and relays; the laptop does detection, tracking and the dashboard; the ESP32 does real-time motion and safety decisions.
- D-15 [DECIDED] Command path: Laptop, Wi-Fi, Raspberry Pi, USB serial, ESP32.
- D-16 [DECIDED] Cameras: Raspberry Pi Camera Module 3 standard (about 66 degrees) for scanning, and the HQ Camera with a 25 mm lens for confirming and fine aiming. Why not the Wide module: too few pixels on a pigeon at 50 ft.
- D-17 [DECIDED] Both cameras and the deterrent are mounted on the turret in a triangular layout. The telephoto crosshair is aligned with the deterrent.
- D-18 [DECIDED] Engagement logic: the wide camera finds candidates at a low threshold and slews the turret; the telephoto confirms at a high threshold and fine-aims; fire only on telephoto confirmation.
- D-19 [DECIDED] Streaming: one stream at a time (wide while scanning, telephoto while engaging), 12 to 15 fps. The resolution is the highest one that sustains at least 12 fps within the Pi's thermal limits, tuned in feature 004.
- D-20 [DECIDED] Wi-Fi first with a single power cable. Ethernet only if the latency acceptance test fails (provisional: p95 latency of 400 ms or less, re-baselined in feature 004).
- D-21 [DECIDED] v1 scope: perched and landing pigeons only. Design range 50 ft (15 m); the real perches are 30 to 40 ft away.
- D-22 [DECIDED] Observability: metrics, logs and traces, with one trace per engagement, stored in SQLite and shown on the dashboard.
- D-23 [PROVISIONAL] ROS 2 is not in the v1 stack (plain Python with FastAPI). Revisit before feature 005 if job targets need it. Changing the stack needs a constitution amendment.

## D. Motion and mechanical
- D-24 [DECIDED] Two stepper motors with TMC2208 drivers; the ESP32 generates the pulses with acceleration profiles; about 4:1 belt reduction. The TMC2208 has no stall detection.
- D-25 [DECIDED] Pan travel 240 degrees, tilt travel 80 degrees.
- D-26 [DECIDED] Homing on every boot and every 10 minutes, only in safe states, with drift logged on each re-home. Drift above the threshold disarms the device. It is the only automatic recalibration.
- D-27 [OPEN] Homing sensor type (contact or non-contact), decided in feature 003 by a measured repeatability test of at least 100 homes per candidate.
- D-28 [DECIDED] Capsule enclosure with a fixed dome mounted on the enclosure, not on the moving mechanism. The dome is a trial and its optics are checked when it arrives. Two stands (pedestal and wall mount) share the same pickup points.
- D-29 [DECIDED] The mechanism is built once: a small test pedestal first, then the same mechanism moves into the enclosure. The mechanical design comes after the software has set the requirements.
- D-30 [DECIDED] No accelerometer. Relocation is a deliberate dashboard step that deletes all zones, marks calibrations stale and blocks arming until they are redone.
- D-31 [DECIDED] Budget: 30,000 INR (Pi 5 about 8,800; vision system about 11,000; about 11,000 for everything else). Exceeding it only as a last resort with the owner's approval.

## E. Safety
- D-32 [DECIDED] The ESP32 is the final authority on every fire decision. The Pi only relays. The default state on every boot is DISARMED, and homing must finish before arming.
- D-33 [DECIDED] (per-minute part superseded by D-76) A fire request is granted only if all are true: arm key on, dashboard armed, homed, heartbeat alive, beam inside elevation limits and outside every no-fire zone plus margin, zone store valid, not in zone setup, on-time limits not exceeded, and a recent person-clear status. Otherwise it is denied with a reason.
- D-34 [DECIDED] Person inhibit: the laptop detects people and every heartbeat carries a person-clear status. The ESP32 fails closed if the status is missing. Every fire request carries a time-to-live.
- D-35 [DECIDED] Hardware: a key switch and a hardware on-time cutoff in series with laser power; an emergency stop; the ESP32 reads the key state; an indicator light shows when the laser has power.
- D-36 [DECIDED] Laser: 5 mW or less for development and calibration. The 50 mW module is used only in housing-society-approved field trials at dawn or dusk, with wavelength-rated eye protection whenever it is powered. A beam expander was declined by the owner.
- D-37 [OPEN] Fallback if the laser proves ineffective: a visible light beacon or strobe (below 2 flashes per second, never aimed at windows). Water spray was considered and not chosen.
- D-38 [DECIDED] No-fire zones: drawn on a dashboard page by jogging the telephoto crosshair (coarse and fine) and dropping points with a key; the polygon closes automatically to point 1. Each polygon is independent and deleted as a whole. There is no editing, so a zone is recreated instead. Zones are stored only on the ESP32, in motor angles relative to the homed zero, never in camera pixels. The video overlay is never used for a safety decision.
- D-39 [DECIDED] Zone rules: at least 3 points; self-crossing polygons are rejected; uploads are atomic with a checksum; safety margin is 0.5 degrees plus the measured aiming error; a corrupted zone store blocks all firing.
- D-40 [SUPERSEDED by D-51] After the heartbeat is lost the device goes to a safe/error state and never re-arms by itself.

## F. Dashboard, data and experiment
- D-41 [DECIDED] A comprehensive multi-page dashboard. Overview: live video with the detection overlay, pan and tilt angles, system state, recent log, and laptop and Pi health. Other pages: Detections and Accuracy, Activity (heat map and time of day), Deterrence Log, Observability, Calibration, Safety.
- D-42 [DECIDED] Scan policy: 70 percent of the time at heat-map hotspots, 30 percent random areas. The heat map is stored in pan/tilt angles, normalised by observation time, over a rolling 72-hour window. The raw detection log is kept.
- D-43 [DECIDED] Census sweep: a fixed sweep (same angles, same order, same dwell, same schedule, never firing) is the only measure of pigeon presence in the on/off comparison. Why: the adaptive scan would otherwise bias the count.
- D-44 [DECIDED] Deterrence experiment: alternating on and off periods, sham "would have fired" events during off periods, and a statistical test, reported whether the result is positive or negative. Detection accuracy is checked against human-labelled samples.
- D-45 [DECIDED] Four calibrations: wide-camera lens distortion, wide-camera pixel to turret angle, telephoto-to-deterrent alignment, and homing zero. The first three are repeated for each installation.
- D-46 [DECIDED] Privacy: raw footage and datasets are never committed to Git, published images and videos have people blurred, and all data stays local.

## G. Feature 001 placeholder values (simulated device only)
- D-47 [PROVISIONAL](on-time caps superseded by D-76 and D-79) Heartbeat 200 ms; watchdog 1 s; person-clear hold-off 1 s; fire time-to-live 500 ms; on-time cap 2 s per shot and 10 s per minute; simulated aiming error 0.2 degrees; up to 8 zones of 16 points. Confirmed as written by the owner on 2026-10-31. The 2 s per-shot cap is longer than the owner's earlier idea of about 1 s and can be lowered later.

## H. Feature 001 plan decisions (2026-10-31)
- D-48 [DECIDED] Person detected is a fire inhibit, not a fault. While the latest heartbeat reports a person, the ESP32 denies fire requests and cuts the effector if it is on. The device keeps its state and keeps scanning. No fault is latched. Why: the constitution says deny and cut and names no fault state, and a person walking past should not force a manual recovery.
- D-49 [DECIDED] Re-engage delay: after a person is no longer reported, fire stays denied until person-clear has been reported continuously for a set delay. The ESP32 measures it on its own clock. A new person report, or a missing or stale status, restarts it. When it ends, the inhibit lifts and a new fire request can be granted if every other check passes. The device never fires on its own and never resumes a cut shot. This is separate from the person-clear hold-off in D-47, which limits how old a status may be. Why: gives the person time to leave the area, and stops rapid on/off cycling if the detector flickers.
- D-50 [PROVISIONAL] The re-engage delay is 5 seconds, configurable. Chosen by the owner without field data. Revisit in features 006 and 008.
- D-51 [DECIDED] No condition creates a latched fault in feature 001. A denied fire request is a normal reply that names one reason and changes no state. If the effector is on when a condition lapses, it is cut and the same reason is reported. Heartbeat loss and key off still move the device to SAFE_ERROR (FR-027 and the scope document), but recovery is automatic when the cause ends and the device lands in DISARMED, never re-arming by itself. Whether homing failure, homing drift or an internal error should latch is decided in feature 003. Supersedes D-40. Why: safety comes from staying DISARMED until the operator arms, not from remembered faults.
- D-52 [DECIDED] In the scope document, "zone violation" and "limit violation" mean a fire request denied because the beam would be inside a no-fire zone plus margin or outside the elevation limits. It is a denial, not a fault. Every denial names exactly one reason, chosen by a fixed priority order, and each reason names one cause.
- D-53 [DECIDED] docs/system-scope.md is amended to v1.5: person detected and limit or zone violation are removed from the SAFE/ERROR triggers and described as denials. Why: the plan and the scope document must not disagree (D-11).
- D-54 [DECIDED] Key off goes to SAFE_ERROR from every state, including ZONE_SETUP, as the scope document says. The trigger is the change from on to off. Recovery is automatic when the key is back on, landing in DISARMED. Fire arbitration also reads the live key level at request time. Why: one rule for every state, because exceptions in a safety state machine are where bugs hide.
- D-55 [DECIDED] DISARM is the only way to abort a zone upload; there is no ZONE_ABORT message. A partial upload lives in a staging buffer and is discarded on every exit except an accepted ZONE_COMMIT. A rejected ZONE_BEGIN has no effect. Why: DISARM is already always accepted, there are fewer message types, and fire decisions read only the validated zone store.
- D-56 [SUPERSEDED by D-61] ZONE_STORE_INVALID is computed from the stored zones each time (checksum check), not kept as a fault bit. It clears when ZONE_CLEAR leaves a valid store; an empty store is valid. Why: one source of truth, so the denial reason can always be reported.
- D-57 [DECIDED] The fault bits HOMING_FAILED and INTERNAL are reserved for feature 003. Nothing sets them in feature 001, and a test confirms they stay clear.
- D-58 [DECIDED] Feature 001 plan choices confirmed: up to 8 zones of 16 points; binary frames with COBS framing and CRC-16, angles in hundredths of a degree; a fixed denial priority order with one reason per denial; the ESP32 judges a fire by its own measured angles, not the direction in the request; zone upload timeout 5 s, simulated homing 3 s, acknowledgement timeout 500 ms; percentiles by nearest rank; test-only state injection for scenario vectors, compiled out of production firmware. Why: fixed sizes and orders make behaviour predictable and testable, and the device never trusts the sender for a safety check.
- D-59 [OPEN] Key switch debouncing and handling of very short key glitches on real hardware. Decided in feature 006.

## I. Feature 001 plan refinements (2026-10-31)
- D-60 [DECIDED] Denial reason codes are permanent. Each reason has a fixed numeric code that never changes, and the priority order is stored separately. Codes are never reused, a new reason gets the next unused number, and adding or reordering rungs never changes an existing code. PERSON_CLEAR_DELAY has code 14; the other 13 keep their earlier values. Why: the protocol is a contract between the Python simulator now and the ESP32 firmware later, and a code that changes when the priority order changes would make the two sides silently disagree.
- D-61 [DECIDED] Zone store validity is computed from the stored bytes each time. An unreadable store counts as invalid. If it becomes readable again and every checksum matches, it is valid again without ZONE_CLEAR. Corrupted bytes stay invalid until ZONE_CLEAR leaves a valid store. An empty store is valid. Supersedes D-56. Why: one source of truth, and a store that passes its checksums is valid by definition.
- D-62 [PROVISIONAL] The person-clear hold-off and the heartbeat watchdog both default to 1000 ms. With equal values a silent laptop shows HEARTBEAT_LOST first, and the stale-status denial PERSON_NOT_CLEAR is reachable only when the hold-off is below the watchdog (tests lower it). Both outcomes fail closed. Re-tune in feature 004 with measured Wi-Fi jitter.
- D-63 [DECIDED] Timing boundaries, all on the device's own clock: a person-clear status is fresh at an age of exactly the hold-off and stale 1 ms later; a fire request is denied before the re-engage delay has elapsed and may be granted at exactly the delay.
- D-64 [DECIDED] A clear period starts at the receive time of the first accepted clear heartbeat after a person was present. A person report, a stale status, boot, heartbeat loss and SAFE_ERROR recovery restart it.
- D-65 [DECIDED] A cut caused by a person ends the shot (DETER to SCANNING) (partly superseded by D-78: now goes to COOLDOWN). The device stays armed, and a cut shot is never resumed without a new fire request.
- D-66 [PROVISIONAL] A re-engage delay of 0 disables the delay. Allowed for now; whether production configuration may use 0 is decided in feature 006.

## J. Feature 001 plan additions by Claude Code, confirmed by the owner (2026-10-31)
- D-67 [DECIDED] Reason codes live in a machine-readable file (protocol/reason-codes.json) with a locked snapshot (tests/contract/reason_codes_lock.json). The lock test compares the table against the snapshot, not against the file itself, and a new reason is accepted only at the next unused number. Why: an accidental renumbering fails the build instead of silently breaking the contract.
- D-68 [DECIDED] The protocol has a document-level version (0.1), recorded in the scenario vector files. The permanence rule applies in principle to every wire identifier (message types, state codes, fault bits, reject codes), but only the reason-code table has a lock test for now.
- D-69 [DECIDED] The dashboard API shows denial reasons by name only. Numeric codes and priorities never appear there.
- D-70 [DECIDED] A running effector is also cut when the zone store becomes unreadable, through the general invalid-store rule.

## K. Feature 001 protocol fixes (2026-10-31)
- D-71 [DECIDED] The request time-to-live is a precondition checked once when a fire request arrives (FR-025), not an invariant re-checked during a shot. While the effector is on, the device re-checks priorities 1 to 12 except 9 (REQUEST_EXPIRED). FR-026 in spec.md was reworded by the owner to say so. Why: re-checking it would cut every valid shot longer than the remaining TTL and log a false interlock event.
- D-72 [DECIDED] DISARM is accepted in every state and replies OK. In BOOT, HOMING and SAFE_ERROR it changes nothing else; in every other state it moves the device to DISARMED, cuts the effector and discards any partial zone upload. Why: homing always runs to completion, and DISARM stays always accepted (FR-020).
- D-73 [DECIDED] granted_on_ms in the FIRE_REQUEST GRANTED reply is a u32 in the device time base. Only ZONE_BEGIN, ZONE_POINT and ZONE_COMMIT reset the zone-upload idle timer.
- D-74 [DECIDED] Line endings are pinned to LF with .gitattributes (* text=auto eol=lf). Why: a Windows checkout would otherwise convert files to CRLF and break byte-exact tests such as the reason-code lock.
- D-75 [DECIDED] Design questions are raised only if they block the current task; otherwise they are logged as open items and settled by the task's test. Why: documents had reached the point of diminishing returns.

## L. Shot control changes (2026-10-31)
- D-76 [DECIDED] The constitution is amended to v1.1.0. Principle III now requires "the per-shot on-time limit is not exceeded and the post-shot cooldown has elapsed" instead of a per-minute limit. Rationale: the per-minute budget limited total laser time but did not match the owner's use. Eye safety rests on the person-clear rule, no-fire zones, the cooldown and the hardware on-time cutoff, which remains a required second layer. Supersedes the per-minute rule in D-33 and D-47.
- D-77 [DECIDED] The per-minute budget and the RATE_LIMIT denial are removed. Code 13 is retired and never reused. Next unused code is 16.
- D-78 [PROVISIONAL] Cooldown: after any shot ends (normal end, FIRE_STOP, or a cut that does not enter SAFE_ERROR) fire is denied with COOLDOWN_ACTIVE (code 15) for cooldown_ms, default 5000, measured on the device clock. The timer survives DISARM, ARM and SAFE_ERROR recovery; only boot clears it. A shot ending in SAFE_ERROR starts no cooldown. A person-caused cut now goes to COOLDOWN, which supersedes the DETER-to-SCANNING wording in D-65.
- D-79 [DECIDED] duration_ms in FIRE_REQUEST is required and is the maximum on-time for the shot, chosen on the dashboard from 500 to 5000 ms in 500 ms steps. The device checks the range (500 to per_shot_ms, default 5000), not the step. Below 500 is FIELD_RANGE; above per_shot_ms is ON_TIME_LIMIT, checked once at admission. Closes open item 9.
- D-80 [DECIDED] New message FIRE_STOP (type 0x21), accepted in every state without arbitration. It can only switch the effector off and start the cooldown; it never turns anything on or extends a shot. Why: stopping is the safe direction. The laptop decides when a pigeon is deterred; the ESP32 still enforces the maximum if the laptop fails.
- D-81 [DECIDED] Manual mode (jog and fire from the dashboard) is wanted for calibration and field testing. Manual fire goes through normal arbitration, including the person-clear rule. Jogging is feature 003, the fire control is feature 006.
- D-82 [PROVISIONAL] system-scope.md is v1.6: the hardware on-time cutoff must be set at or above per_shot_ms.

## Open decisions
1. Where the device sits relative to the perch height, which sets the elevation limits.
2. The dome's optical test (camera distortion, laser reflection) when it arrives.
3. The pan-tilt requirements sheet (payload, speed, acceleration, torque margin) before CAD.
4. Homing sensor type, and the maximum delay allowed for periodic re-homing.
5. Census interval, angle list, dwell time and counting rule.
6. Video transport and message protocol details (decided in the feature plans).
7. Detector model and version (decided in the feature 002 plan).
8. Whether ROS 2 is needed (D-23).
9. [CLOSED by D-79] Whether the 2 s per-shot cap (D-47) should be lowered.
10. Key switch debouncing and short glitches (D-59), decided in feature 006.
11. Whether homing failure, homing drift or an internal error should latch, decided in feature 003.
12. Whether the 5 s re-engage delay (D-50) is right, to be checked with field data in features 006 and 008.
13. Whether the hold-off and watchdog defaults (D-62) should differ, decided in feature 004.
14. Whether a re-engage delay of 0 (D-66) is allowed in production configuration, decided in feature 006.
15. Whether the wire format needs a protocol version field so a version mismatch is detected at run time (D-68), decided in feature 003.
16. Exact behaviour of the zone-upload idle timer at the 5000 ms boundary, decided when firmware conformance matters (feature 003).
17. DISARM during BOOT has no scenario vector yet; add one in the first task that touches DISARM.
18. Whether production may use cooldown_ms 0 (D-78), decided in feature 006.
19. Calibration 3 (feature 004) may need a minimal manual fire control before feature 006's Safety page (D-81).
20. Whether the laptop may extend a deterrence run by sending repeated fire requests while a pigeon stays visible (feature 005); each request passes full arbitration and the cooldown applies.
21. Whether the dashboard may show both camera streams at once (a low-resolution, low-frame-rate second stream for viewing only). This needs a constitution amendment because the Technology Stack says the Pi serves one video stream at a time. Decided in feature 004 after measuring Pi CPU load, temperature and Wi-Fi load.
22. Whether the dashboard pages need an automated browser-test tool. Feature 001 has none (JavaScript is checked by text tests and manual steps), and adding one changes the stack. Decided before feature 004.
23. Heartbeat loss during HOMING: protocol.md 5.3 applies the watchdog in every state after the first accepted heartbeat, while data-model.md excludes BOOT, HOMING and SAFE_ERROR. Leaning to the protocol.md rule. Settle before T019a, then make protocol.md, data-model.md, T019a and one vector agree (analyze F1).
24. The SC-019 architecture test (T070) must match grant constructors and effector setters, not the word GRANTED, and must add the two checks research.md R22 promises. Settle before T070 (analyze F2).
25. T007 must state that name and code uniqueness is per table (denial reasons versus reject codes), and the retired-code-13 check applies to the denial table only. Settle before T007 (analyze F3).
26. Analyze findings settled inside the task that touches them: T006 whether the 66-byte oversize limit includes the delimiter and a check for a decoded frame above 64 bytes (F12); T016 add T003 to Depends (F13); T003 and T056 the PIGEON_DEV_TOOLS default (F9); T008 and T025a the FIRE_REQUEST reply in BOOT (F8); T017 and T019b what HOME does and key-off during HOMING (F7); T038 and T042 the stop:STATE_DETER tag and the unreachable table row (F6); T053 an outcome for a REJECTED reply (F4).