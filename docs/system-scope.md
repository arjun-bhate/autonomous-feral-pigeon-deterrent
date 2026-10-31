# System Scope: Autonomous Feral Pigeon Deterrent System (v1.3)

## Goal
An autonomous turret that detects perched or landing feral pigeons up to 50 ft (15 m), confirms them with a telephoto camera, aims a visible deterrent, and measures both its engineering performance and its deterrence effect.

## Command path
Laptop -> Wi-Fi -> Raspberry Pi 5 -> USB serial -> ESP32.
The laptop sends high-level commands (aim at angle, jog, upload zone, request fire). The Pi relays them and does not make safety decisions. The ESP32 is the authority for motion, homing, no-fire zones and every fire decision, and it replies with granted/denied and a reason.

## States
BOOT -> HOMING -> DISARMED -> (key on + dashboard Arm) -> SCANNING -> CANDIDATE (wide camera, low threshold) -> SLEW -> CONFIRM (telephoto, high threshold) -> TRACK -> DETER -> COOLDOWN -> SCANNING
Periodic HOMING (see Homing) and CENSUS sweeps (see Census sweep) run only in safe states.
ZONE_SETUP: manual jogging to draw no-fire zones; effector hard-disabled by the ESP32.
Any state -> SAFE/ERROR on fault, lost heartbeat, person detected, key off, limit or zone violation, or homing drift.

## Hardware (v1)
- Turret head, triangular layout: Camera Module 3 standard (~66 deg horizontal), HQ Camera with 25 mm lens, laser effector (5 mW or less in development; 50 mW for approved field trials)
- Raspberry Pi 5 (2 GB): capture, encode, stream (one stream at a time, 12-15 fps), command relay, health reporting, maximum focus on higher resolution with a minimum threshold of 12 fps.
- ESP32 (motion, homing, zones, fire decisions), 2x TMC2208 drivers, 2 steppers (belt reduction about 4:1), homing sensors on both axes (type decided in feature 003)
- Safety hardware: key switch (in series with laser power, state read by the ESP32), hardware on-time cutoff, emergency stop, power indicator LED
- Pan travel 240 deg, tilt travel 80 deg
- Capsule enclosure with a fixed dome (not on the moving mechanism); pedestal and wall-mount stands with the same pickup points
- One external cable (power); Wi-Fi to the laptop
- Mechanism built once: small test pedestal first, then the same mechanism moves into the enclosure
- Laptop (i5-12450HX, RTX 3050 6 GB): detection, tracking, person inhibit, engagement logic, dashboard, storage

## Software components
1. ESP32 firmware: step generation with acceleration profiles, homing (boot and periodic), soft limits, watchdog, effector driver with interlocks, no-fire zone store and engine, fire arbitration
2. Pi services: camera capture and streaming, serial bridge, health telemetry
3. Laptop core: video ingest, detector, tracker, engagement state machine, scan policy, census scheduler, person inhibit
4. Dashboard: Overview (live feed with detection overlay, pan/tilt angles, state, logs, laptop and Pi health), Detections and Accuracy, Activity (heat map, time of day), Deterrence Log, Observability (metrics, logs, traces), Calibration, Safety (arm and key status, zones read back from the ESP32)
5. Data and observability: SQLite, structured logs, one trace per engagement

## Homing
- Homing zero is the only automatic recalibration: it runs on every boot and every 10 minutes.
- Periodic re-homing runs only in DISARMED, or between scan dwells in SCANNING. It never runs during CONFIRM, TRACK, DETER or ZONE_SETUP. If due during an engagement, it waits until the engagement ends (maximum deferral set in the plan). The effector is disabled while homing. If a re-home and a census sweep are both due, the re-home runs first so the census angles are accurate.
- Approach procedure for each axis: seek the sensor at moderate speed, back off, then approach slowly from the same direction every time (cancels backlash and switch hysteresis).
- Every re-home compares the expected position with the position found at the sensor. That difference (drift, in degrees) is logged as a metric and the zero is updated. Drift above the configured threshold disarms the device and raises a fault. This is the only step-loss detection, because the TMC2208 has no stall detection.
- Homing sensor type is OPEN. Contact (micro-switch) and non-contact (optical or Hall) are both acceptable. The choice is made in feature 003 from a measured repeatability test: at least 100 consecutive homes per candidate, logging the position found each time, with a target repeatability of 0.05 deg or better (to be validated). At one re-home per 10 minutes an axis sees roughly 52,000 homing cycles a year, so any contact switch must be rated for the cycle life.

## No-fire zones
- Drawn on a dashboard page: jog pan/tilt with the telephoto crosshair (coarse and fine toggle), drop points with a key, and the polygon closes automatically to point 1.
- Each polygon is independent. A zone is deleted as a whole. No editing: recreate instead.
- Zones are stored on the ESP32 (persistent memory) as pan/tilt motor angles relative to the homed zero, never in camera pixels. The laptop holds no authoritative copy. The dashboard displays what the ESP32 reports back and draws the overlay on the video from calibration; the overlay is never used for any safety decision.
- Zone commands: UPLOAD_ZONE (points then commit with a checksum, atomic: a partial upload is discarded), DELETE_ZONE(id), LIST_ZONES, CLEAR_ZONES.
- Creation rules: minimum 3 points, self-crossing polygons rejected, maximum number of zones and points set in the plan (suggested 8 zones of 16 points).
- The ESP32 makes every fire decision. FIRE_REQUEST is granted only if all are true: key on; armed; homed; Pi heartbeat alive; beam direction (motor angles plus stored effector offset) inside elevation limits and outside every zone expanded by the safety margin; zone store valid; not in ZONE_SETUP; on-time and rate caps not exceeded. Otherwise FIRE_DENIED with a reason.
- The ESP32 keeps checking while the effector is on and cuts it immediately if the beam enters a margin region or any condition lapses.
- Safety margin: 0.5 deg plus measured aiming error. A corrupted zone store blocks all firing.
- The laptop still owns person inhibit (vision) and the engagement logic, and may pre-check zones for convenience, but this is not a safety layer.
- Relocation: with no accelerometer the device cannot detect being moved, so relocation is a deliberate dashboard step. "Relocate" deletes all zones on the ESP32, marks calibrations 1-3 stale, and blocks arming until they are redone and zones are redrawn or "no zones required" is confirmed.

## Scan policy and heat map
- Step-and-stare (move, settle, capture, detect). 70% of dwell targets from the heat map, 30% random.
- The heat map is stored in pan/tilt angles and normalised by observation time per region, and covers a rolling 72-hour window (hourly buckets; older buckets are dropped from the map).
- Until enough observation time exists in a region, or during the first 72 hours, the policy uses a uniform sweep for that share.
- The raw detection log is kept in SQLite; only the scan-policy heat map is windowed. The Activity page can show longer history.

## Census sweep
- Purpose: an unbiased headcount for the on/off experiment. The adaptive scan policy and engagements change where and how often the turret looks, so their counts cannot be compared between "deterrent on" and "deterrent off" periods.
- Every N minutes (default 15; set in the plan) the turret runs a fixed sweep: the same list of pan/tilt angles covering the whole perch area, the same order, the same dwell time, regardless of the heat map. It only detects and counts. It never fires.
- Census sweeps run on the same schedule in "on" and "off" periods. If one is due during an engagement it is deferred until the engagement ends, and every deferral is logged.
- The counting rule (to avoid counting one bird twice across overlapping views) is defined in the plan.
- Census counts, and only census counts, are the primary measure of pigeon presence in the experiment. Scan-policy and engagement detections are secondary.

## Calibrations
1. Wide-camera lens distortion; 2. wide-camera pixel to turret angle; 3. telephoto-to-effector alignment at operating distance: run at setup and after every Relocate. 4. Homing zero: automatic on every boot and every 10 minutes (see Homing).

## Feature roadmap (software-first, hardware purchased as needed)
- 001 Platform core (no hardware): repo structure, laptop backend skeleton, dashboard Overview shell, laptop health panel, logs/metrics/traces foundation, SQLite, ESP32 simulator (including zone store and fire arbitration), serial protocol definition
- 002 Vision on recorded footage (no hardware): replay harness, detector, overlay on Overview, dataset tooling, labeling and accuracy log
- 003 Motion core (needs ESP32, steppers, drivers, sample homing sensors): firmware, boot and periodic homing with drift metric, homing repeatability test and sensor decision, jog, Pi bridge, manual aim, zone engine (point-in-polygon with margin, unit-tested without hardware)
- 004 Streaming and calibration (needs Pi 5, cameras): capture, stream switching, resolution tuning against the 12 fps floor, latency tracing, calibrations 1-3
- 005 Engagement loop: state machine, step-stare scan, census sweep, cue and slew, telephoto confirmation, closed-loop centering
- 006 Safety and effector: arm key and logic, person inhibit, no-fire zone page, relocation procedure, hardware interlock tests, varied effector patterns
- 007 Analytics: detection log, heat map (72 h window), activity by hour, deterrence log, census analysis, trace viewer, 70/30 scan policy
- 008 Field experiment: alternating on/off schedule, sham events, census-based comparison, statistical analysis, report

## Success metrics
Tier 1 (must pass; thresholds set after the first baseline): detection precision and recall on a labeled held-out set at 30-50 ft; end-to-end latency p50/p95 over Wi-Fi; aiming error in degrees; sustained stream frame rate (at least 12 fps) at the chosen resolution; Pi CPU and temperature; homing repeatability and drift per re-home; multi-day uptime; 100% interlock test pass rate.
Tier 2 (reported honestly): pigeon presence during on versus off periods, measured by the census sweep; departure time after real versus sham events; statistical significance.

## Wi-Fi acceptance test (provisional)
30-minute run at the install site: p95 frame-to-command latency of 400 ms or less, and no gap over 1 s more than once. Failing it triggers the switch to Ethernet. Threshold re-baselined after feature 004.

## Stream resolution rule
The stream resolution is the highest setting that sustains at least 12 fps, keeps the Pi below its thermal throttling point, and passes the Wi-Fi acceptance test. It is measured and chosen in feature 004.

## Constraints
Budget 30,000 INR (owner approval to exceed). Perched and landing pigeons only. Field trials at dawn or dusk with housing society approval. Wavelength-rated eye protection whenever the 50 mW module is powered.

## Out of scope (v1)
Birds in flight, other species, multiple turrets, cloud, mobile app, access from outside the home network, automatic mount detection.

## Risks
Laser weak in bright daylight; pigeons getting used to the deterrent; small-target detection; Wi-Fi latency spikes (including late-arriving fire requests: command time-to-live defined in the feature 006 plan); dome optics; stepper step loss; forgetting the Relocate step; Pi 2 GB encoding limits at higher resolution; census sweeps taking time away from scanning; budget; 3 hours a day of build time.

## Open decisions
1. Device placement relative to perch height (sets elevation limits)
2. Dome optical test (camera distortion, laser reflection) when the dome arrives
3. Pan-tilt requirements sheet (payload, slew, acceleration, torque margin) before CAD
4. Homing sensor type (contact vs non-contact), decided in feature 003 by measured repeatability; maximum deferral time for periodic re-homing
5. Maximum number of zones and points per zone
6. Census interval, angle list, dwell time and counting rule
7. Video transport and messaging protocol (decided in feature plans)
8. Detector model and version (decided in the feature 002 plan)