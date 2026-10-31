# System Scope: Autonomous Feral Pigeon Deterrent System (v1.1)

## Goal
An autonomous turret that detects perched or landing feral pigeons up to 50 ft (15 m) using a standard camera, confirms them with a telephoto camera, aims and fires a visible deterrent, and measures both its engineering performance and its deterrence effect.

## States
BOOT -> HOMING -> DISARMED -> (key on + dashboard Arm) -> SCANNING -> CANDIDATE (wide camera, low threshold) -> SLEW -> CONFIRM (telephoto, high threshold) -> TRACK -> DETER -> COOLDOWN -> SCANNING
ZONE_SETUP: manual jogging to draw no-fire zones; effector hard-disabled.
Any state -> SAFE/ERROR on fault, lost heartbeat, person detected, key off, limit or zone violation.

## Hardware (v1)
- Turret head, triangular layout: Camera Module 3 standard (~66 deg horizontal), HQ Camera with 25 mm lens, laser effector (5 mW in development; 50 mW for approved field trials)
- Raspberry Pi 5 (2 GB): capture, encode, stream (one stream at a time, 12-15 fps), command relay, health reporting, maximum focus on higher resolution with a minimum threshold of 12 fps.
- ESP32, 2x TMC2208 drivers, 2 steppers (motion trasmision medium not decided(Belt or geared yet to be decided)), homing switches, base accelerometer
- Safety hardware: key switch (in series with laser power, state read by ESP32), hardware on-time cutoff, emergency stop, power indicator LED
- Pan travel 240 deg, tilt travel 80 deg
- Capsule enclosure with a fixed dome (not on the moving mechanism); pedestal and wall-mount stands with the same pickup points
- One external cable (power); Wi-Fi to the laptop
- Mechanism built once: small test pedestal first, then the same mechanism moves into the enclosure
- Laptop (i5-12450HX, RTX 3050 6 GB): detection, tracking, decisions, dashboard, storage

## Software components
1. ESP32 firmware: step generation with acceleration profiles, homing, soft limits, watchdog, effector driver with interlocks, mirrored no-fire zones
2. Pi services: camera capture and streaming, serial bridge, health telemetry
3. Laptop core: video ingest, detector, tracker, engagement state machine, scan policy, safety gate
4. Dashboard: Overview (live feed with detection overlay, pan/tilt angles, state, logs, laptop and Pi health), Detections and Accuracy, Activity (heat map, time of day), Deterrence Log, Observability (metrics, logs, traces), Calibration, Safety (arm status, no-fire zones)
5. Data and observability: SQLite, structured logs, one trace per engagement

## No-fire zones
- Drawn on a dashboard page: jog pan/tilt with the telephoto crosshair (coarse and fine), drop points with a key, polygon closes automatically to point 1 after the final polygon point.
- Each polygon is independent; delete a whole polygon; no editing (recreate instead).
- Stored in pan/tilt motor angles relative to the homed zero, never in camera pixels. Overlay on video is drawn from calibration, but firing checks never use the camera.
- Safety margin around each zone (0.2 deg plus measured aiming error). Minimum 3 points; self-crossing polygons rejected; effector hard-disabled during ZONE_SETUP.
- Enforced on the laptop and again on the ESP32 from a mirrored copy using actual motor angle.
- Zones belong to a mounting ID (accelerometer); a different mounting makes them unverified and firing stays blocked until confirmed.
- The device re-homes periodically and checks for position drift; drift disarms it and logs a fault.

## Scan policy
Step-and-stare (move, settle, capture, detect). 70% of dwell targets from the heat map, 30% random. Heat map stored in pan/tilt angles, normalised by observation time per region.

## Calibrations
1. Wide-camera lens distortion; 2. wide-camera pixel to turret angle; 3. telephoto-to-effector alignment at operating distance; 4. homing zero on every boot. Repeat 1-3 for each new installation or stand.

## Feature roadmap (software-first, hardware purchased as needed)
- 001 Platform core (no hardware): repo structure, laptop backend skeleton, dashboard Overview shell, laptop health panel, logs/metrics/traces foundation, SQLite, ESP32 simulator, serial protocol definition
- 002 Vision on recorded footage (no hardware): replay harness, detector, overlay on Overview, dataset tooling, labeling and accuracy log
- 003 Motion core (needs ESP32, steppers, drivers): firmware, homing, jog, Pi bridge, manual aim from dashboard
- 004 Streaming and calibration (needs Pi 5, cameras): capture, stream switching, latency tracing, the four calibrations
- 005 Engagement loop: state machine, step-stare scan, cue and slew, telephoto confirmation, closed-loop centering
- 006 Safety and effector: arm key and logic, person inhibit, no-fire zone page, hardware interlock tests, varied effector patterns
- 007 Analytics: detection log, heat map, activity by hour, deterrence log, trace viewer, 70/30 scan policy
- 008 Field experiment: alternating on/off schedule, sham events, statistical analysis, report

## Success metrics
Tier 1 (must pass; thresholds set after the first baseline): detection precision and recall on a labeled held-out set at 30-50 ft; end-to-end latency p50/p95 over Wi-Fi; aiming error in degrees; frame rate; Pi CPU and temperature; multi-day uptime; 100% interlock test pass rate.
Tier 2 (reported honestly): pigeon presence during on versus off periods; departure time after real versus sham events; statistical significance.

## Wi-Fi acceptance test (provisional)
30-minute run at the install site: p95 frame-to-command latency of 400 ms or less, and no gap over 1 s more than once. Failing it triggers the switch to Ethernet. Threshold re-baselined after feature 004.

## Constraints
Budget 30,000 INR (owner approval to exceed). Perched and landing pigeons only. Field trials at dawn or dusk with housing society approval. Wavelength-rated eye protection whenever the 50 mW module is powered.

## Out of scope (v1)
Birds in flight, other species, multiple turrets, cloud, mobile app, access from outside the home network.

## Risks
Laser weak in bright daylight; pigeons getting used to the deterrent; small-target detection; Wi-Fi latency spikes; dome optics; stepper step loss; budget; 3 hours a day of build time.

## Open decisions
1. Device placement relative to perch height (sets elevation limits)
2. Dome optical test (camera distortion, laser reflection) when the dome arrives
3. Pan-tilt requirements sheet (payload, slew, acceleration, torque margin) before CAD
4. Video transport and messaging protocol (decided in feature plans)
5. Detector model and version (decided in the feature 002 plan)