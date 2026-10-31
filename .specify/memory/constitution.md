<!--
SYNC IMPACT REPORT (temporary; remove before committing)
Version change: (unratified template) -> 1.0.0
Modified principles: none (initial ratification; all template placeholders replaced)
Added sections:
  - Core Principles I-VIII (spec-driven workflow, reporting, layered safety, homing,
    observability, hardware-free testability, honest results, privacy)
  - Technology Stack; System Constraints; Out of Scope (v1); Governance
Removed sections: none (template placeholder sections SECTION_2/SECTION_3 became the
  Technology Stack and System Constraints sections; Out of Scope added)
Source check: input compared with docs/system-scope.md (v1.3) and CLAUDE.md; no conflicts found.
  Reconciled, not conflicting: "one commit per task" (Principle I) is applied together with
  CLAUDE.md's "do not commit unless asked".
Follow-up TODOs: none. Open decisions that the constitution deliberately leaves to feature
  plans: homing sensor type (feature 003), max zones/points, census parameters, max re-home
  deferral, YOLO version pin, video transport/protocol.
-->
# Autonomous Feral Pigeon Deterrent System Constitution

A portfolio-grade robotics project. The words MUST, MUST NOT, SHOULD and SHOULD NOT are used as
in RFC 2119: MUST/MUST NOT are non-negotiable; SHOULD/SHOULD NOT may be departed from only with
a written justification in the relevant spec or plan.

## Core Principles

### I. Spec-Driven, One Task at a Time
- No production code MUST be written without an approved spec, plan and task list for the
  feature.
- Exactly one task MUST be implemented per session, after which work MUST stop.
- Every task MUST ship with automated tests written before the implementation. A task is done
  only when its tests pass AND the owner completes its manual exit checklist.
- Each task MUST end in one commit using Conventional Commits. Commits are made only when the
  owner asks.
- Each feature MUST live on its own branch and merge to main by pull request only after
  `/speckit-analyze` shows no critical issues.

Rationale: small, verified, reviewable steps keep a safety-relevant system understandable.

### II. Reporting and Explainability
- After every prompt or task, a report in the format defined in CLAUDE.md MUST be produced.
  The file list in the report MUST come from git output (`git status --short`,
  `git diff --stat`), never from memory.
- Every report MUST explain, in plain language, what changed, in which file, and why.
- Code MUST be written and explained so that the owner can explain every module in an
  interview.

### III. Safety Is Enforced in Layers, with the ESP32 as the Authority
Command path and authority:
- The command path is Laptop -> Wi-Fi -> Raspberry Pi -> USB serial -> ESP32. The Pi relays
  commands and MUST NOT make safety decisions.
- The default state on every boot MUST be DISARMED. Homing MUST complete before arming.
- The ESP32 MUST make every fire decision.

Fire arbitration:
- A fire request MUST be granted only when ALL of the following are true: the physical arm
  key is on; the dashboard is armed; the device is homed; the Pi heartbeat is alive; the beam
  direction is inside the elevation limits and outside every no-fire zone expanded by the
  safety margin; the zone store is valid; the device is not in ZONE_SETUP; and the per-shot
  and per-minute on-time limits are not exceeded. Otherwise it MUST be denied with a reason.
- While the effector is on, the ESP32 MUST keep checking these conditions and MUST cut the
  effector the moment any of them lapses.

Laptop responsibilities and person inhibit:
- The laptop owns person detection and engagement logic. Every heartbeat it sends MUST carry a person-clear status with a sequence number and timestamp.
- The ESP32 MUST fail closed on person inhibit: it MUST deny a fire request, and cut the effector if it is on, whenever the latest heartbeat reports a person, or no person-clear status has arrived within the configured hold-off time. A silent laptop therefore means no firing.
- Every fire request MUST carry a time-to-live. The ESP32 MUST deny any request that arrives after its time-to-live has expired.
- Laptop-side zone checks are a convenience only and MUST NOT be treated as a safety layer.

Other ESP32 enforcement:
- A heartbeat watchdog MUST stop the motors and switch the effector off when the heartbeat is
  lost.
- Soft angle limits MUST be enforced, and no motion MUST occur before homing.
- A corrupted zone store MUST block all firing.

Hardware:
- A key switch and a hardware on-time cutoff MUST be wired in series with effector power.
- An emergency stop MUST be fitted, and the ESP32 MUST read the key state.

No-fire zones:
- Zones MUST be drawn on a dashboard page by jogging the telephoto crosshair.
- Each polygon is independent and MUST be deleted as a whole. Zones MUST NOT be edited;
  they are recreated instead.
- Zones MUST be stored ONLY on the ESP32, as pan/tilt motor angles relative to the homed
  zero, and MUST NEVER be stored in camera pixels.
- Uploads MUST be atomic and verified by checksum. Polygons MUST have at least 3 points, and
  self-crossing polygons MUST be rejected.
- The safety margin MUST be 0.5 degrees plus the measured aiming error.
- The video overlay is drawn from calibration and MUST NOT be used for any safety decision.

Relocation:
- There is no accelerometer. Relocation MUST be a deliberate dashboard step that deletes all
  zones, marks calibrations stale and blocks arming until they are redone.

Laser power and eye safety:
- Development and calibration MUST use a laser of 5 mW or less.
- The 50 mW module MUST be fitted only for field trials approved by the housing society, at
  dawn or dusk, and wavelength-rated eye protection MUST be worn whenever it is powered.

Verification:
- Safety logic MUST have automated tests (laptop and firmware) and a hardware exit checklist.
- No code path MAY be written that can fire the effector without every interlock above.

Rationale: a laser aimed outdoors is a hazard to people, pets and neighbours. Independent
layers, with the final authority on the device that moves the beam, prevent any single
software, network or operator error from causing harm.

### IV. Homing
- Homing zero MUST run automatically on every boot and every 10 minutes. It is the only
  automatic recalibration.
- Periodic homing MUST run only in DISARMED or between scan dwells, and MUST NOT run during
  CONFIRM, TRACK, DETER or ZONE_SETUP. The effector MUST be disabled while homing.
- Each axis MUST approach its sensor the same way every time: find, back off, then approach
  slowly from one direction.
- Every re-home MUST log the drift between the expected and found position as a metric. Drift
  above the configured threshold MUST disarm the device and raise a fault.
- The homing sensor type (contact or non-contact) is an open decision. It MUST be settled in
  feature 003 by a measured repeatability test of at least 100 consecutive homes per
  candidate.

Rationale: stepper drivers here have no stall detection, so re-homing drift is the only
step-loss detector and the basis for trusting stored zone angles.

### V. Observability by Default
- Every component MUST emit structured JSON logs with timestamp, component, severity and,
  where applicable, trace_id and span_id.
- Every engagement MUST be a trace with spans for capture, encode, network, inference,
  decision, command, ESP32 acknowledgement and effector action.
- Metrics MUST include frame rate, end-to-end latency (p50/p95), CPU/GPU/RAM/temperatures for
  the laptop and Pi, detection counts, homing drift and interlock events.
- Observability data MUST be stored locally (SQLite in v1) and viewable in the dashboard.

### VI. Testable Without Hardware
- Camera, network and serial interfaces MUST sit behind abstractions with mock
  implementations.
- A replay harness MUST run the vision pipeline on recorded footage.
- A software ESP32 simulator MUST speak the same protocol as the real firmware, including the
  zone store and fire arbitration.
- Most automated tests MUST run on the laptop with no hardware attached.
- Zone geometry (point-in-polygon with margin) MUST be unit-tested.

### VII. Measured, Honest Results
- Engineering metrics MUST include: precision/recall on a labeled held-out set, end-to-end
  latency, aiming error in degrees, sustained frame rate, homing repeatability, uptime and
  interlock correctness.
- Deterrence MUST be evaluated with alternating on/off periods, sham "would-have-fired"
  events during off periods, and a statistical test. Results MUST be reported whether
  positive or negative.
- Presence for the on/off comparison MUST be measured ONLY by the census sweep: a fixed list
  of pan/tilt angles in a fixed order with a fixed dwell time, run on a fixed schedule in both
  on and off periods, detect-and-count only, never firing. Deferred sweeps MUST be logged.
  Adaptive scan and engagement detections are secondary evidence.
- Accuracy MUST be computed against human-labeled samples.
- The scan-policy heat map MUST be stored in pan/tilt angles, normalised by observation time
  per region, over a rolling 72-hour window. The raw detection log MUST be kept.

### VIII. Privacy
- Raw footage and datasets MUST NOT be committed to Git.
- Published images and videos MUST have people blurred.
- All data MUST stay local.

## Technology Stack

Changes to this stack require a constitution amendment.

- **Laptop**: Windows 11; Python 3.12 managed with uv; PyTorch with CUDA (RTX 3050);
  Ultralytics YOLO (version pinned in the plan); OpenCV; FastAPI with WebSockets; dashboard
  frontend in HTML/JavaScript with Chart.js unless a plan justifies more; SQLite.
- **Raspberry Pi 5 (2 GB)**: Raspberry Pi OS 64-bit; Picamera2 with the OS-provided Python.
  Its role MUST be limited to capture, encoding, streaming, command relay and health
  reporting. It MUST serve one video stream at a time (wide while scanning, telephoto while
  engaging), favour the highest resolution that sustains a minimum of 12 fps, and be tuned by
  measurement within the Pi's thermal limits.
- **ESP32**: C++ with PlatformIO (Arduino framework); hardware-timed step generation with
  acceleration profiles; TMC2208 drivers; USB serial link to the Pi with framed messages,
  sequence numbers and checksums; persistent storage for zones.
- **Quality**: ruff for lint and format; type hints on public functions; pytest; GitHub
  Actions MUST run lint and tests on every push.

## System Constraints

- The total hardware budget is 30,000 INR; exceeding it requires explicit owner approval.
- There MUST be one external cable (power). The laptop-Pi link is Wi-Fi; Ethernet is adopted
  only if the defined latency acceptance test fails.
- Pan travel is 240 degrees and tilt travel is 80 degrees. Design range is 50 ft (15 m).
  Scope is limited to perched and landing pigeons.
- The dome MUST be fixed to the enclosure, not to the moving mechanism. The mechanism is built
  once and moved from a test pedestal into the enclosure.

## Out of Scope (v1)

Birds in flight, other species, multiple turrets, cloud services, a mobile app, access from
outside the home network, and automatic mount detection.

## Governance

- This constitution and docs/system-scope.md are the source of truth. If a request or document
  conflicts with them, work MUST stop and the conflict be reported to the owner.
- Amendments require owner approval, a version bump and a short written rationale.
- Versioning follows semantic versioning: MAJOR for backward-incompatible principle removals
  or redefinitions; MINOR for a new principle or section or materially expanded guidance;
  PATCH for clarifications and wording.
- Every spec and plan MUST state which principles it satisfies. Pull request review and
  `/speckit-analyze` MUST verify compliance, and any deviation MUST be justified in writing.
- CLAUDE.md provides runtime working rules for the coding assistant and MUST remain consistent
  with this constitution.

**Version**: 1.0.0 | **Ratified**: 2026-10-31 | **Last Amended**: 2026-10-31
