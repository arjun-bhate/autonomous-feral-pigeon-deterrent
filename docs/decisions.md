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
- D-33 [DECIDED] A fire request is granted only if all are true: arm key on, dashboard armed, homed, heartbeat alive, beam inside elevation limits and outside every no-fire zone plus margin, zone store valid, not in zone setup, on-time limits not exceeded, and a recent person-clear status. Otherwise it is denied with a reason.
- D-34 [DECIDED] Person inhibit: the laptop detects people and every heartbeat carries a person-clear status. The ESP32 fails closed if the status is missing. Every fire request carries a time-to-live.
- D-35 [DECIDED] Hardware: a key switch and a hardware on-time cutoff in series with laser power; an emergency stop; the ESP32 reads the key state; an indicator light shows when the laser has power.
- D-36 [DECIDED] Laser: 5 mW or less for development and calibration. The 50 mW module is used only in housing-society-approved field trials at dawn or dusk, with wavelength-rated eye protection whenever it is powered. A beam expander was declined by the owner.
- D-37 [OPEN] Fallback if the laser proves ineffective: a visible light beacon or strobe (below 2 flashes per second, never aimed at windows). Water spray was considered and not chosen.
- D-38 [DECIDED] No-fire zones: drawn on a dashboard page by jogging the telephoto crosshair (coarse and fine) and dropping points with a key; the polygon closes automatically to point 1. Each polygon is independent and deleted as a whole. There is no editing, so a zone is recreated instead. Zones are stored only on the ESP32, in motor angles relative to the homed zero, never in camera pixels. The video overlay is never used for a safety decision.
- D-39 [DECIDED] Zone rules: at least 3 points; self-crossing polygons are rejected; uploads are atomic with a checksum; safety margin is 0.5 degrees plus the measured aiming error; a corrupted zone store blocks all firing.
- D-40 [PROVISIONAL] After the heartbeat is lost the device goes to a safe/error state and never re-arms by itself.

## F. Dashboard, data and experiment
- D-41 [DECIDED] A comprehensive multi-page dashboard. Overview: live video with the detection overlay, pan and tilt angles, system state, recent log, and laptop and Pi health. Other pages: Detections and Accuracy, Activity (heat map and time of day), Deterrence Log, Observability, Calibration, Safety.
- D-42 [DECIDED] Scan policy: 70 percent of the time at heat-map hotspots, 30 percent random areas. The heat map is stored in pan/tilt angles, normalised by observation time, over a rolling 72-hour window. The raw detection log is kept.
- D-43 [DECIDED] Census sweep: a fixed sweep (same angles, same order, same dwell, same schedule, never firing) is the only measure of pigeon presence in the on/off comparison. Why: the adaptive scan would otherwise bias the count.
- D-44 [DECIDED] Deterrence experiment: alternating on and off periods, sham "would have fired" events during off periods, and a statistical test, reported whether the result is positive or negative. Detection accuracy is checked against human-labelled samples.
- D-45 [DECIDED] Four calibrations: wide-camera lens distortion, wide-camera pixel to turret angle, telephoto-to-deterrent alignment, and homing zero. The first three are repeated for each installation.
- D-46 [DECIDED] Privacy: raw footage and datasets are never committed to Git, published images and videos have people blurred, and all data stays local.

## G. Feature 001 placeholder values (simulated device only)
- D-47 [PROVISIONAL] Heartbeat 200 ms; watchdog 1 s; person-clear hold-off 1 s; fire time-to-live 500 ms; on-time cap 2 s per shot and 10 s per minute; simulated aiming error 0.2 degrees; up to 8 zones of 16 points. Confirmed as written by the owner on 2026-10-31. The 2 s per-shot cap is longer than the owner's earlier idea of about 1 s and can be lowered later.

## Open decisions
1. Where the device sits relative to the perch height, which sets the elevation limits.
2. The dome's optical test (camera distortion, laser reflection) when it arrives.
3. The pan-tilt requirements sheet (payload, speed, acceleration, torque margin) before CAD.
4. Homing sensor type, and the maximum delay allowed for periodic re-homing.
5. Census interval, angle list, dwell time and counting rule.
6. Video transport and message protocol details (decided in the feature plans).
7. Detector model and version (decided in the feature 002 plan).
8. Whether ROS 2 is needed (D-23).
9. Whether the 2 s per-shot cap (D-47) should be lowered.