"""Tests for the two person timers (T020): status hold-off and re-engage delay."""

from deterrent.protocol.messages import Heartbeat, host_frame
from deterrent.simulator.person import PersonTimers, evaluate_person
from support.device_harness import Harness

HOLDOFF = 1000
DELAY = 5000


def timers() -> PersonTimers:
    return PersonTimers()


def beats(timers: PersonTimers, start: int, end: int, person_clear: bool = True) -> None:
    """Accepted heartbeats every 200 ms from ``start`` up to and including ``end``."""
    for now in range(start, end + 1, 200):
        timers.on_heartbeat(now, person_clear, HOLDOFF)


def check(timers: PersonTimers, now: int, holdoff: int = HOLDOFF, delay: int = DELAY):
    return evaluate_person(timers.snapshot(), now, holdoff, delay)


# ---- the pure rules, with explicit times -----------------------------------------------------


def test_no_heartbeat_yet_means_person_present():
    result = check(timers(), 0)
    assert result.present is True
    assert result.delay_elapsed is False


def test_status_is_fresh_exactly_at_the_hold_off_and_stale_one_ms_later():
    t = timers()
    t.on_heartbeat(100, True, HOLDOFF)
    assert check(t, 100 + HOLDOFF).present is False
    assert check(t, 100 + HOLDOFF + 1).present is True


def test_a_person_report_means_present_while_fresh():
    t = timers()
    t.on_heartbeat(100, False, HOLDOFF)
    assert check(t, 100).present is True
    assert check(t, 600).present is True


def test_delay_not_elapsed_one_ms_early_and_elapsed_exactly_at_the_delay():
    t = timers()
    beats(t, 0, 4800)
    early = check(t, DELAY - 1)
    assert (early.present, early.delay_elapsed) == (False, False)
    on_time = check(t, DELAY)
    assert (on_time.present, on_time.delay_elapsed) == (False, True)
    assert check(t, DELAY + 1).delay_elapsed is True


def test_a_person_reappearing_restarts_the_delay_from_zero():
    t = timers()
    beats(t, 0, 200)
    t.on_heartbeat(400, False, HOLDOFF)  # person steps in
    beats(t, 600, 600 + DELAY - 200)  # clear again: a new period starts at 600
    assert check(t, 600 + DELAY - 1).delay_elapsed is False
    assert check(t, 600 + DELAY).delay_elapsed is True
    # An earlier clear time does not count: measured from 600, not from 0.
    assert check(t, DELAY).delay_elapsed is False


def test_a_stale_status_ends_the_period_and_a_late_clear_heartbeat_starts_a_new_one():
    t = timers()
    t.on_heartbeat(0, True, HOLDOFF)
    t.on_heartbeat(200, True, HOLDOFF)
    # Silence until 3000: the status went stale at 1201.
    t.on_heartbeat(3000, True, HOLDOFF)
    after = check(t, 3000 + DELAY - 1)
    assert after.delay_elapsed is False
    # (If the old period had counted, the delay would already be over at 5000.)
    assert check(t, 5000).delay_elapsed is False


def test_a_stale_status_reads_as_person_present_between_heartbeats():
    t = timers()
    t.on_heartbeat(0, True, HOLDOFF)
    assert check(t, 1001).present is True


def test_restart_discards_the_period_but_keeps_the_latest_status():
    t = timers()
    t.on_heartbeat(0, True, HOLDOFF)
    t.restart()
    result = check(t, 100)
    assert (result.present, result.delay_elapsed) == (False, False)
    beats(t, 300, 300 + DELAY - 200)  # the next clear heartbeat begins the period
    assert check(t, 300 + DELAY).delay_elapsed is True
    assert check(t, 300 + DELAY - 1).delay_elapsed is False


def test_delay_zero_never_reports_a_delay():
    t = timers()
    t.on_heartbeat(0, True, HOLDOFF)
    assert check(t, 0, delay=0).delay_elapsed is True
    t.restart()
    assert check(t, 10, delay=0).delay_elapsed is True
    assert check(timers(), 0, delay=0).present is True  # still present with no status


def test_wrap_safe_across_the_32_bit_clock_wrap():
    t = timers()
    start = 2**32 - 100
    t.on_heartbeat(start, True, HOLDOFF)
    for step in range(1, 26):
        t.on_heartbeat((start + 200 * step) % 2**32, True, HOLDOFF)
    last = (start + 200 * 25) % 2**32
    assert check(t, (start + DELAY - 1) % 2**32).delay_elapsed is False
    assert check(t, (start + DELAY) % 2**32).delay_elapsed is True
    assert check(t, (last + HOLDOFF + 1) % 2**32).present is True


# ---- on the device ---------------------------------------------------------------------------


def run_clear_heartbeats(harness: Harness, total_ms: int, laptop_ts: int = 0) -> None:
    for _ in range(total_ms // 200):
        harness.advance(200)
        harness.reply(Heartbeat(True, laptop_ts))


def test_the_device_applies_the_delay_from_the_receive_time_of_the_first_clear_heartbeat():
    harness = Harness.homed()
    harness.heartbeat()
    start = harness.clock.now_ms()
    run_clear_heartbeats(harness, 4800)
    harness.advance(199)
    assert harness.clock.now_ms() == start + 4999
    assert harness.device.person_check().delay_elapsed is False
    harness.advance(1)
    result = harness.device.person_check()
    assert (result.present, result.delay_elapsed) == (False, True)


def test_a_wild_laptop_timestamp_changes_nothing():
    results = []
    for laptop_ts in (0, 0xFFFFFFFF, 123456789):
        harness = Harness.homed()
        harness.reply(Heartbeat(True, laptop_ts))
        run_clear_heartbeats(harness, 4800, laptop_ts)
        harness.advance(200)
        results.append(harness.device.person_check())
    assert results[0] == results[1] == results[2]
    assert results[0].delay_elapsed is True


def test_a_stale_heartbeat_starts_nothing():
    harness = Harness.homed()
    harness.reply(Heartbeat(False, 0), seq=10)  # person reported, accepted
    harness.advance(300)
    harness.send_wire(host_frame(Heartbeat(True, 0), 3))
    result = harness.device.person_check()
    assert result.present is True  # the stale "clear" was not accepted


def test_a_duplicate_heartbeat_continues_nothing():
    harness = Harness.homed()
    harness.reply(Heartbeat(True, 0), seq=10)
    harness.advance(600)
    before = harness.device.person_snapshot()
    harness.send_wire(host_frame(Heartbeat(True, 0), 10))
    assert harness.device.person_snapshot() == before


def test_heartbeat_loss_restarts_the_clear_period():
    harness = Harness.homed()
    harness.heartbeat()
    run_clear_heartbeats(harness, 1000)
    harness.advance(1001)  # watchdog fires, the device goes to SAFE_ERROR
    harness.heartbeat()  # recovery: a new period begins at this heartbeat
    recovered_at = harness.clock.now_ms()
    run_clear_heartbeats(harness, 4600)
    harness.advance(recovered_at + DELAY - 1 - harness.clock.now_ms())
    assert harness.device.person_check().delay_elapsed is False
    harness.advance(1)
    assert harness.device.person_check().delay_elapsed is True


def test_safe_error_recovery_by_key_restarts_the_clear_period():
    harness = Harness.homed(key_on=True)
    harness.heartbeat()
    run_clear_heartbeats(harness, 5400)
    assert harness.device.person_check().delay_elapsed is True
    harness.control.set_key(False)
    harness.control.set_key(True)
    harness.tick()  # recovery happens here
    result = harness.device.person_check()
    assert (result.present, result.delay_elapsed) == (False, False)


def test_boot_starts_with_no_status():
    harness = Harness(boot_duration_ms=500)
    assert harness.device.person_check().present is True
