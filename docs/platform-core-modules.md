# Platform core: a plain-language guide to every module

This guide explains each Python module of feature 001 (the platform core): what it does, what it never does, and which tests prove it. It contains no code. Paths are from the repository root. A *module* is one `.py` file; a *package* is a folder of modules.

Words used throughout:

- The **simulator** is a stand-in for the real ESP32 (the small computer that will drive the motors and the effector). It lives in `src/deterrent/simulator/`.
- The **effector** is the thing that deters a pigeon (a laser or similar). In this feature it is only a yes/no flag inside the simulator.
- The **laptop side** is everything else: the web server, the dashboard, the logging, the health sensors.
- A **trace** is the recorded timeline of one engagement, made of eight steps.

## The safety authority split

The most important idea in this system is who is allowed to decide what.

Only the simulated ESP32 decides whether the effector may switch on, and inside it only one function, `decide_fire` in `src/deterrent/simulator/arbitration.py`, makes that decision. That function checks fourteen conditions in a fixed order (key switch, armed, homed, no person, zone rules, cooldown and so on). If any one fails, the answer is "denied" with the first failing reason. Only when every condition passes does the simulator's device module switch the flag on and answer "granted".

The laptop side can only ask. It sends a fire request over the link and then reads what the device answered. It cannot grant a request, it cannot set the effector flag, and it cannot make a reply up. The web API, the developer tools, the simulated engagement, the dashboard pages and the health code all sit on this side. If the laptop stops sending its "I am alive" heartbeat, the device protects itself: it cuts the effector and goes to a safe state without being told to.

Two more rules keep the split honest. The simulator never imports the web framework, the logging code or the wiring code, so it cannot be influenced by them. And only one module, `src/deterrent/composition.py`, connects the simulator to the logging system; it does that by handing the simulator a small reporting object, never the other way round. The architecture test `tests/architecture/test_boundaries.py` reads the source code as a syntax tree and fails if any of these rules is broken, so the rule does not depend on anyone remembering it.

What the dashboard shows is therefore always a report of what the device said, never a decision the dashboard made.

## The packages

**`src/deterrent/`** is the top-level package. Its own modules are the clock, the settings, and the wiring that joins everything. What it never does: contain any fire decision. Proven by: `tests/architecture/test_boundaries.py`.

**`src/deterrent/api/`** is the web side: the server application, the read-only data endpoints, the developer tools and the live channel to the browser. What it never does: grant a fire or switch the effector, or import the simulator. Proven by: `tests/architecture/test_boundaries.py`.

**`src/deterrent/engagement/`** holds the simulated engagement used to produce traces without a camera. What it never does: decide a fire; it only reports the device's answer. Proven by: `tests/integration/test_engagement.py`.

**`src/deterrent/geometry/`** holds the exact whole-number polygon maths used for no-fire zones. What it never does: use floating point, so a point is never "nearly" on an edge. Proven by: `tests/unit/test_polygon_crossing.py`.

**`src/deterrent/health/`** reads the laptop's own sensors (CPU, GPU, memory, temperatures). What it never does: let one slow sensor hold up another. Proven by: `tests/unit/test_collector.py`.

**`src/deterrent/link/`** is the pipe that carries bytes between the laptop and the device. What it never does: understand messages or make decisions. Proven by: `tests/unit/test_link.py`.

**`src/deterrent/observability/`** records and reads logs, traces and metrics. What it never does: block the code that is doing real work while the disk is slow. Proven by: `tests/unit/test_store_resilience.py`.

**`src/deterrent/protocol/`** defines the exact bytes of every message between laptop and device. What it never does: contain any decision logic; it only names, packs and unpacks. Proven by: `tests/unit/test_framing.py`.

**`src/deterrent/simulator/`** is the simulated ESP32 and everything it needs. What it never does: reach outward to the web, the logs or the database, or accept a command that skips the fire decision. Proven by: `tests/architecture/test_boundaries.py`.

## The web side (`api`)

**`src/deterrent/api/app.py`** builds the web application, starts the platform when the server starts and stops it cleanly, serves the dashboard pages and their static files, and gives every error the same shape. It only answers requests addressed to `127.0.0.1` or `localhost`. Never: listen on anything but the loopback address, or let a web page on another site talk to it. Proven by: `tests/integration/test_api_basic.py`, `tests/integration/test_privacy.py`.

**`src/deterrent/api/errors.py`** defines the single error shape every response uses, a code and a message. Never: leak internal details in a message. Proven by: `tests/integration/test_api_basic.py`.

**`src/deterrent/api/live.py`** is the live channel: one WebSocket per browser over which the server pushes the snapshot, device changes, health, new log lines, storage warnings and a once-a-second tick. It sends only what is held in memory and gives each browser its own queue. Never: wait for the disk, let one slow browser delay another, or act on anything a browser sends. Proven by: `tests/integration/test_live.py`, `tests/integration/test_device_panel.py`, `tests/integration/test_restart_and_storage.py`.

**`src/deterrent/api/routes_dev.py`** provides the developer tools that let the owner and the tests drive the simulator: start and stop it, turn the key, set whether a person is in view, send a protocol command, run a simulated engagement, break a sensor or the storage. They exist only while the development setting is on; otherwise every one answers "not found". Never: contain a "grant" or "force" option, create a reply, or import the simulator. Proven by: `tests/integration/test_api_dev.py`, `tests/architecture/test_boundaries.py`.

**`src/deterrent/api/routes_read.py`** holds the read-only endpoints (health, device status, system status, logs, traces, metrics) and the routes that serve the dashboard pages. History is read through a read-only database connection on a worker thread. Never: change anything, or let a slow database slow the live channel; an unreadable database gives a clear "unavailable" error. Proven by: `tests/integration/test_api_basic.py`, `tests/integration/test_api_logs.py`, `tests/integration/test_api_traces.py`, `tests/integration/test_api_metrics.py`, `tests/integration/test_health_api.py`.

## Wiring, time and settings (top level)

**`src/deterrent/clock.py`** supplies the time. Every timing rule works on one millisecond clock that wraps around after about 49 days, like the real device's. Code asks a clock object instead of the operating system, so tests can use a fake clock and move time by hand. Never: read the system time anywhere else in timing rules. Proven by: `tests/unit/test_clock.py`.

**`src/deterrent/config.py`** loads the settings from `config/default.toml`, lets an environment variable override one value, and refuses to start on nonsense such as a negative timeout or a shot shorter than the protocol minimum. Values fixed by the protocol are constants that cannot be configured. Never: read secrets or a hidden settings file. Proven by: `tests/unit/test_config.py`.

**`src/deterrent/composition.py`** is the one place that connects the parts: the logging store, the simulator with its link and client, the health collector and the engagement. It adapts the simulator's reports into log lines and counters, and keeps the device's live status in memory. Never: decide a fire, or let the simulator reach the logging code directly. Proven by: `tests/integration/test_composition.py`, `tests/architecture/test_boundaries.py`.

## Simulated engagement (`engagement`)

**`src/deterrent/engagement/simulated.py`** plays one pass of "see a pigeon, decide, ask the device, the device answers". The first four steps are made-up timings labelled simulated; the rest are measured. It sends one fire request through the normal client and records the device's answer as an eight-step trace. Never: decide a fire, report a grant for anything but a granted reply, or retry on its own. Proven by: `tests/integration/test_engagement.py`, `tests/architecture/test_boundaries.py`.

## Geometry (`geometry`)

**`src/deterrent/geometry/polygon.py`** checks that a zone polygon is usable (no repeated points, real area, no crossing edges) and whether a point lies inside it or too close to its edge, all with whole-number arithmetic. Never: use floating point or round. Proven by: `tests/unit/test_polygon_crossing.py`, `tests/unit/test_polygon_margin.py`.

## Laptop health (`health`)

**`src/deterrent/health/sensors.py`** defines where the CPU, GPU, memory and temperature numbers come from: real providers using the psutil and NVIDIA libraries, mock providers the tests can break, and a wrapper that lets the developer tools make a sensor fail. A sensor that cannot be read reports "nothing", which is normal on many laptops. Never: raise an error out of a read or invent a number. Proven by: `tests/unit/test_sensors.py`, `tests/integration/test_health_api.py`.

**`src/deterrent/health/collector.py`** reads each sensor once a second in its own worker thread and waits at most one second. A reading is fresh or "unavailable"; the previous number is never reused. A stuck sensor is not asked again until its read finishes, and every ten seconds the available readings are saved as gauges. Never: let one sensor delay another, pile up stuck threads, or read from the database to answer the dashboard. Proven by: `tests/unit/test_collector.py`, `tests/integration/test_health_soak.py`.

## The link (`link`)

**`src/deterrent/link/base.py`** defines what a link is: something that moves bytes in both directions. Later it will be a serial port or network socket to the real ESP32 without changing the code above it. Never: know about messages, state or decisions. Proven by: `tests/unit/test_link.py`.

**`src/deterrent/link/inprocess.py`** is the link used today: two in-memory queues between the laptop code and the simulator, inside one program. Never: change, delay or reorder bytes. Proven by: `tests/unit/test_link.py`.

**`src/deterrent/link/mock.py`** wraps a link so a test can corrupt, duplicate, drop or delay the next message in either direction, to show the protocol copes. Never: touch traffic unless a test asks. Proven by: `tests/unit/test_link.py`, `tests/integration/test_engagement.py`.

**`src/deterrent/link/client.py`** is the laptop's side of the conversation: it numbers messages, matches each reply to its request, waits a limited time, sends the heartbeat that keeps the device's watchdog satisfied, and tracks the device's clock offset. Never: grant or approve a fire request, invent a reply, or retry a fire request automatically. Proven by: `tests/unit/test_client.py`, `tests/architecture/test_boundaries.py`.

## Logs, traces and metrics (`observability`)

**`src/deterrent/observability/logging.py`** makes every log line a structured record with a time, component, severity, message and optional trace id. It removes secrets and image data before anything is stored and keeps a short in-memory list for the live dashboard. Never: store a password, a token, an image, or anything that looks like one. Proven by: `tests/unit/test_logging.py`, `tests/integration/test_privacy.py`.

**`src/deterrent/observability/metrics.py`** records counters, gauges and samples with units, and computes p50 and p95 by the nearest-rank method. With no samples there is no answer, never a zero. Never: accept a metric without a unit or with the wrong labels. Proven by: `tests/unit/test_metrics.py`, `tests/integration/test_api_metrics.py`.

**`src/deterrent/observability/queries.py`** asks the database read-only questions: filtered and paged logs, trace lists and details, metric summaries and series. Never: change the database or be used by the live paths. Proven by: `tests/unit/test_queries.py`, `tests/integration/test_api_logs.py`.

**`src/deterrent/observability/store.py`** is the observability database. Callers put records on an in-memory queue and return at once; one writer thread saves them in batches. If the disk is full or locked it keeps recent records in memory, raises a warning, and recovers when the disk does. Never: make important code wait for the disk, crash on a disk error, or touch the device's zone database. Proven by: `tests/unit/test_store_basic.py`, `tests/unit/test_store_resilience.py`, `tests/integration/test_restart_and_storage.py`.

**`src/deterrent/observability/tracing.py`** builds the eight-step trace of an engagement in memory and writes it once when complete. A step that never happened is kept as "not run" or "timeout", never left out. Never: allow overlapping steps or a total that disagrees with the steps. Proven by: `tests/unit/test_tracing.py`, `tests/integration/test_api_traces.py`.

## The protocol (`protocol`)

**`src/deterrent/protocol/codes.py`** lists every number and name on the wire: message types, results, reject codes, the twelve device states, fault bits and the fire-denial reasons with their permanent codes. Never: reuse or change a permanent code. Proven by: `tests/unit/test_codes.py`, `tests/contract/test_reason_codes.py`.

**`src/deterrent/protocol/crc.py`** computes the checksums that detect damaged data: one protects every frame, another protects a stored zone. Never: accept data whose checksum does not match. Proven by: `tests/unit/test_crc.py`.

**`src/deterrent/protocol/example.py`** produces the worked byte-level example in the protocol document from the real encoder, so the document cannot go stale. Never: contain a hand-typed byte sequence. Proven by: `tests/unit/test_protocol_doc.py`.

**`src/deterrent/protocol/framing.py`** packs a message into a frame with byte stuffing and a checksum so that zero marks the end unambiguously, and parses frames back. Never: pass on a frame that is too long, too short or fails its checksum. Proven by: `tests/unit/test_framing.py`.

**`src/deterrent/protocol/messages.py`** defines each message as a small data class and turns it into bytes and back, checking lengths and field ranges as the protocol says. It contains the constructors for replies. Never: decide anything; it only represents what someone else decided. Proven by: `tests/unit/test_messages_host.py`, `tests/unit/test_messages_device.py`, `tests/contract/test_hand_built_messages.py`.

## The simulated ESP32 (`simulator`)

**`src/deterrent/simulator/arbitration.py`** is the one place a fire request is decided. A pure function checks the fourteen conditions in priority order and returns the first failing reason, or nothing, meaning granted. A second function re-checks the conditions while a shot runs. Never: read the clock, a file or any hidden state, or change anything. Proven by: `tests/unit/test_arbitration.py`, `tests/contract/test_vectors.py`.

**`src/deterrent/simulator/control.py`** is how the outside world turns the arm key, and it holds the test-only interface that puts the device into an unusual starting situation. Never: switch the effector on, inject the firing state, or work unless the device was built to allow injection. Proven by: `tests/unit/test_control_inject.py`, `tests/architecture/test_boundaries.py`.

**`src/deterrent/simulator/device.py`** is the simulated ESP32 itself: it receives bytes, checks them step by step, runs the state machine (boot, homing, disarmed, scanning, firing and so on), moves the head, enforces the heartbeat watchdog and cuts the effector the moment a safety condition lapses. Never: switch the effector on without a grant from the decision function, or reach the web, logs or database. Proven by: `tests/unit/test_device_boot.py`, `tests/unit/test_device_arm.py`, `tests/unit/test_device_fire.py`, `tests/unit/test_device_fire_stop.py`, `tests/unit/test_device_interlocks.py`, `tests/unit/test_device_intake.py`, `tests/unit/test_device_motion.py`, `tests/unit/test_device_watchdog.py`.

**`src/deterrent/simulator/events.py`** is the small reporting interface through which the device says what happened, such as a state change, a rejected message or the effector switching. Whoever builds the device decides what the reports do. Never: write logs or touch a database itself. Proven by: `tests/unit/test_device_events.py`.

**`src/deterrent/simulator/person.py`** answers two timing questions about the camera's "no person in view" reports: is the latest report too old to trust, and has the yard been clear for long enough. It uses only the time the device received a report, and a silent laptop counts as "person present". Never: trust a timestamp written by the laptop. Proven by: `tests/unit/test_person_timers.py`.

**`src/deterrent/simulator/runner.py`** runs the device inside the asynchronous program: one task reads bytes from the link and hands them to the device, another ticks the device clock about every ten milliseconds. Never: decide anything or alter the bytes. Proven by: `tests/integration/test_sim_runner.py`.

**`src/deterrent/simulator/zone_storage.py`** is where the device keeps its stored no-fire zones: a memory version that tests can make full, locked, unreadable or corrupt, and a file version standing in for the ESP32's flash memory. Each write is all-or-nothing. Never: share a file with the logging database or leave a half-written zone. Proven by: `tests/unit/test_zone_storage_mock.py`, `tests/unit/test_zone_storage_sqlite.py`, `tests/integration/test_restart_and_storage.py`.

**`src/deterrent/simulator/zones.py`** handles a zone upload in three steps (begin, points, commit) in a temporary buffer, and on commit runs nine ordered checks before anything is stored. It also recomputes whether the stored zones are still valid every time. Never: store a zone that fails a check, keep an unfinished upload, or trust a cached "valid" flag. Proven by: `tests/unit/test_zones_upload.py`, `tests/unit/test_zones_commit.py`, `tests/unit/test_zones_store.py`.

## How to explain this in an interview

1. **One sentence.** "I built the laptop platform for a pigeon deterrent: a dashboard, logging and a simulated controller, where only the controller can decide to fire."
2. **The key design rule.** "Safety decisions live in one pure function inside the simulated ESP32. The laptop can only ask and read the answer, and a test reads the source code as a syntax tree to prove nothing else can grant or switch the effector."
3. **Fail closed.** "If the laptop goes quiet, a person might be in view, a zone store is damaged or a key is off, the answer is no. A silent laptop is treated as 'a person is present'."
4. **Never wait for the disk.** "Logging goes to an in-memory queue and a writer thread. When I made the disk full or locked in a test, the live view kept going, a warning appeared, and the safety decisions came out identical."
5. **One sensor cannot hurt another.** "Every laptop sensor is read in its own thread with a one-second limit, so a hung GPU only blanks its own tile. A five-minute simulated run proves the others stay on schedule."
6. **Honest numbers.** "Percentiles use the nearest-rank method, an empty set shows 'no samples' rather than zero, and made-up timings are labelled simulated everywhere."
7. **Tests drive the design.** "Each task started with a failing test. Time is a fake clock, so timing rules are tested exactly, and the protocol is checked against vectors written independently of the code."
8. **Privacy by construction.** "The server binds only to the loopback address, makes no network calls, and logs strip secrets and image data. A test with network access blocked proves it."
9. **What is not done.** "No real camera, motors or Pi yet: the simulator and the in-memory link stand in, behind interfaces that the real hardware can replace without changing the code above them."
