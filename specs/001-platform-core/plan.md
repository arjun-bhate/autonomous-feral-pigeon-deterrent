# Implementation Plan: Platform Core (No Hardware)

**Branch**: `001-platform-core` | **Date**: 2026-10-31 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-platform-core/spec.md`

## Summary

Feature 001 builds the software foundation on the owner's laptop with no hardware attached. It has four parts:

1. A **FastAPI backend** that reads laptop health, stores structured logs, traces and metrics in SQLite, and pushes live updates to a browser dashboard over one WebSocket.
2. A **simulated ESP32**: a small, pure state machine that speaks a defined binary protocol, keeps its own zone store, and is the only place a fire decision is made.
3. A **written protocol** and **machine-readable scenario vectors** that the simulator passes now and the real firmware must pass later.
4. A **static dashboard** (plain HTML, JavaScript and Chart.js, no build step) with Overview, Logs, Traces and Observability pages.

The central design idea, in plain words: the simulator is written like firmware (no web code, no database code, no clock of its own), and everything else talks to it only through bytes on a link. That keeps the safety logic in one small place, makes it testable with a fake clock, and makes it replaceable by the real ESP32 later without changing the laptop side.

Every design choice and its plain-language reason is in [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12, managed with uv (fixed by the constitution). Frontend: plain HTML and JavaScript (ES modules, no bundler).

**Primary Dependencies** (exact versions are chosen by `uv add` at implementation time and pinned in `uv.lock`):
- Runtime: `fastapi`, `uvicorn[standard]` (gives WebSockets), `pydantic-settings` (config), `psutil` (CPU, RAM), `nvidia-ml-py` (NVIDIA GPU load and temperature).
- Dev: `pytest`, `pytest-asyncio`, `httpx` (needed by FastAPI's test client), `ruff`, `hypothesis` (property tests for framing and geometry).
- Frontend (vendored file, not installed): Chart.js, copied into `frontend/vendor/` so the dashboard works offline.
- Not added in this feature: PyTorch, Ultralytics, OpenCV (they belong to feature 002 onward).

**Storage**: SQLite, two separate database files, both under the git-ignored `data/` folder:
- `data/observability.sqlite3`: logs, traces, spans, metric events (laptop side).
- `data/device/zone_store.sqlite3`: the simulated ESP32's zone memory. The laptop code never opens it.

**Testing**: pytest, tests written before implementation for every task (constitution I). Unit, contract (scenario vectors), integration (API and WebSocket with the simulator), and architecture (import-boundary) tests. No hardware needed.

**Target Platform**: Windows 11 laptop (PowerShell, uv). CI also runs on Linux, which is why sensors are mocked in tests.

**Project Type**: Web application, single Python package serving a static frontend (one process).

**Performance Goals**: Health refresh at most 2 s (we sample every 1 s); simulator state change visible on the Overview within 1 s; first data within 3 s of page load; reconnect within 5 s; effector cut within 100 ms of simulated time.

**Constraints**: Everything local (loopback bind, no cloud); no secrets or footage in records; the live views and the simulator's safety decisions must never wait on disk; no hardware-specific code in this feature.

**Scale/Scope**: One user, one simulated device, 8 zones of up to 16 points, a few thousand log lines per day, about 5 MB per day of stored metrics. Four dashboard pages.

No `NEEDS CLARIFICATION` items remain. Items that the owner should confirm are listed in [research.md](research.md#items-for-owner-confirmation).

## Constitution Check

*GATE: passed before Phase 0 research. Re-checked after Phase 1 design (result below).*

| Principle | How this plan satisfies it | Status |
|-----------|---------------------------|--------|
| I. Spec-driven, one task at a time | This plan follows the approved spec. Tests-first, one commit per task (only when asked), own branch `001-platform-core`. `/speckit-tasks` and `/speckit-analyze` come next. No code is written in this step. | Pass |
| II. Reporting and explainability | Each design choice has a plain-words reason in research.md. Modules are small and named for one job so each can be explained. The CLAUDE.md report follows every task. | Pass |
| III. Safety in layers, ESP32 authority | `deterrent.simulator` is the only package that can produce a "granted" reply or switch the simulated effector on. Laptop-side code gets bytes in and bytes out and has no grant path. A documented priority order picks exactly one denial reason. The effector is re-checked on every tick and every message. Heartbeat loss, person inhibit, TTL, zone rules, soft limits and corrupted-store blocking are all inside the simulator. An architecture test enforces the boundary (SC-019). Key, hardware cutoff, e-stop and eye-safety rules are hardware items outside this feature. | Pass |
| IV. Homing | The simulator boots into HOMING, refuses motion and arming until homed, and records simulated drift as a metric. Real sensors, periodic re-homing and the drift threshold belong to feature 003. | Pass (scoped) |
| V. Observability by default | Structured JSON logs, an eight-span trace per engagement, counters, gauges and p50/p95 distributions, all in local SQLite and shown on the dashboard. | Pass |
| VI. Testable without hardware | Camera, network, serial and sensor interfaces sit behind abstractions with mocks. The simulator speaks the same protocol as the future firmware including zones and arbitration. Zone geometry has unit and property tests. The replay harness is feature 002. | Pass |
| VII. Measured, honest results | Percentiles use a documented method (nearest-rank) and are checked against an independent calculation. Simulated timings are labelled `simulated` in storage and on screen. | Pass |
| VIII. Privacy | Loopback only, `data/` ignored by Git, a logging filter rejects image data and known secret fields. | Pass |
| Technology Stack | Python 3.12, uv, FastAPI with WebSockets, HTML/JavaScript with Chart.js, SQLite, ruff, pytest, GitHub Actions. Nothing outside the stack is added except small helper libraries (psutil, nvidia-ml-py, pydantic-settings, hypothesis), which are not stack changes. | Pass |

**Post-design re-check**: Passed. The one new risk found during design is the test-only state-injection interface that scenario vectors need. It is recorded under Complexity Tracking with its guard.

## Project Structure

### Documentation (this feature)

```text
specs/001-platform-core/
├── plan.md              # This file
├── research.md          # Phase 0: every design choice, plain words, alternatives
├── data-model.md        # Phase 1: entities, fields, storage tables, state machine
├── quickstart.md        # Phase 1: how to run and check the feature end to end
├── contracts/
│   ├── protocol.md          # Draft of the ESP32 protocol (promoted to protocol/PROTOCOL.md in the implementation)
│   ├── scenario-vectors.md  # File format of the machine-readable vectors and the adapter interface
│   └── dashboard-api.md     # REST, WebSocket and developer-tool endpoints
└── tasks.md             # Phase 2 (/speckit-tasks) - NOT created by this command
```

### Source Code (repository root)

```text
pyproject.toml               # uv project, ruff and pytest settings
uv.lock                      # exact dependency versions (committed)
.python-version              # 3.12
.gitignore                   # adds data/, *.sqlite3, __pycache__
config/
└── default.toml             # every tunable value, with units (timings, limits, zone limits)

protocol/                    # Shared by the simulator now and the ESP32 firmware later
├── PROTOCOL.md              # The written protocol (single copy; promoted from contracts/protocol.md)
├── vector-schema.json       # JSON Schema for vector files
├── reason-codes.json        # Permanent denial-reason code table (name to code); never reassigned
└── vectors/
    ├── fire_arbitration.json
    ├── zones.json
    ├── framing_and_sequence.json
    └── timing.json

src/deterrent/
├── config.py                # Settings loaded from config/default.toml and environment
├── clock.py                 # Clock interface, SystemClock, FakeClock (tests)
├── composition.py           # The ONLY place that wires simulator, link, storage and API together
├── protocol/                # Pure protocol code, no I/O: codes, message layouts, COBS framing, CRC
│   ├── codes.py
│   ├── messages.py
│   ├── framing.py
│   └── crc.py
├── geometry/
│   └── polygon.py           # Integer point-in-polygon with margin, self-crossing test
├── simulator/               # The simulated ESP32. Pure logic, no FastAPI, no observability imports
│   ├── device.py            # State machine, message handling, tick
│   ├── arbitration.py       # The fire decision (the only grant path)
│   ├── zones.py             # Upload session and zone rules
│   ├── zone_storage.py      # ZoneStorage interface + SQLite implementation + mock
│   ├── events.py            # DeviceEventSink interface (how the device reports what happened)
│   ├── control.py           # SimulatorControl: test-only inputs (key switch, corrupt store)
│   └── runner.py            # Async wrapper: ticks the device, reads/writes the link
├── link/
│   ├── base.py              # DeviceLink interface (send bytes, receive bytes)
│   ├── inprocess.py         # Link to the in-process simulator
│   ├── mock.py              # Fault-injecting link (corrupt, duplicate, drop, delay)
│   └── client.py            # DeviceClient: sequence numbers, heartbeats, request/reply, time offset
├── health/
│   ├── sensors.py           # SensorProvider interface, real (psutil, NVML) and mock providers
│   └── collector.py         # Reads sensors on a schedule with per-sensor timeouts
├── observability/
│   ├── logging.py           # JSON log records, trace context, filter for secrets
│   ├── tracing.py           # Trace and Span objects
│   ├── metrics.py           # Counters, gauges, latency distributions, nearest-rank percentiles
│   ├── store.py             # SQLite writer thread, memory buffer, retry, storage warning
│   ├── schema.sql           # Table definitions
│   └── queries.py           # Log filters, trace lookup, metric summaries
├── engagement/
│   └── simulated.py         # Developer-triggered simulated engagement (8 spans)
└── api/
    ├── app.py               # create_app() factory, lifespan, static files
    ├── live.py              # WebSocket hub: snapshot, events, tick
    ├── routes_read.py       # Health, device, logs, traces, metrics, system status
    └── routes_dev.py        # Developer tools (only when dev tools are enabled)

frontend/
├── index.html               # Overview
├── logs.html
├── traces.html
├── observability.html
├── css/app.css
├── js/{live.js, overview.js, logs.js, traces.js, observability.js}
└── vendor/chart.umd.min.js

tests/
├── unit/                    # protocol, geometry, arbitration, zones, metrics, collector, store
├── contract/                # Runs every file in protocol/vectors/ against the simulator; reason-code lock test
├── integration/             # API + WebSocket + simulator + store together, fake clock where possible
└── architecture/            # Import-boundary and "no grant path outside the simulator" checks

.github/workflows/ci.yml     # ruff + pytest on every push (constitution requirement)
```

**Structure Decision**: One Python package (`deterrent`) with one sub-package per job, serving a static `frontend/` folder, plus a top-level `protocol/` folder that is deliberately outside the Python package because the future C++ firmware will read the same files. Only `composition.py` knows about every part; all other modules depend on small interfaces. This is the smallest layout that still lets the architecture test prove the safety boundary.

### Suggested build order (input for `/speckit-tasks`)

This is guidance for the next step, not the task list itself. Each line is meant to become one or a few small tasks, each starting with its tests.

1. Project skeleton: uv project, ruff, pytest, CI, config loader, clock.
2. Protocol core: CRC, COBS framing, message layouts, codes (with property tests).
3. Geometry: self-crossing and point-in-polygon with margin (unit and property tests).
4. Simulator core: state machine, homing, motion, key, arm/disarm, status.
5. Simulator zones: upload session, store, persistence, corruption handling.
6. Simulator arbitration: all denial reasons in priority order, continuous checking, watchdog.
7. Protocol document, vector schema and vector runner; vectors written alongside steps 2 to 6.
8. Observability store: logs, traces, metrics, resilience to full or locked storage.
9. Health collector with mock sensors and per-sensor timeouts.
10. Link, DeviceClient, simulator runner, simulated engagement.
11. API and WebSocket.
12. Frontend pages.
13. Architecture tests and final end-to-end pass of the quickstart.

## Complexity Tracking

| Violation / Risk | Why Needed | Simpler Alternative Rejected Because |
|------------------|------------|--------------------------------------|
| Test-only state-injection interface (`apply_initial` in the vector adapter; a test-build-only message in future firmware) | Vectors must start from conditions such as "armed, key off, store corrupted, 1.9 s of on-time already used" that are slow or impossible to reach through normal commands. | Reaching every starting state through real commands would make vectors long, timing-dependent and unable to cover corrupted stores. Guard: the interface can only set inputs and state, never grant a fire; in firmware it must be excluded from production builds by a compile-time flag, and a vector asserts that it is rejected when the flag is off. |
