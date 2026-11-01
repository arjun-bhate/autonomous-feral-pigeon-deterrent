"""Tests for the clock module (T004)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deterrent.clock import U32_MODULUS, Clock, FakeClock, SystemClock, elapsed_ms

u32 = st.integers(min_value=0, max_value=U32_MODULUS - 1)


def test_fake_clock_starts_where_told():
    assert FakeClock().now_ms() == 0
    assert FakeClock(start_ms=1234).now_ms() == 1234


def test_fake_clock_does_not_move_by_itself():
    clock = FakeClock(start_ms=10)
    first = clock.now_ms()
    for _ in range(1000):
        clock.now_ms()
    assert clock.now_ms() == first == 10


def test_fake_clock_advances_only_when_told():
    clock = FakeClock()
    clock.advance(250)
    assert clock.now_ms() == 250
    clock.advance(0)
    assert clock.now_ms() == 250
    clock.advance(1)
    assert clock.now_ms() == 251


def test_fake_clock_refuses_to_go_backwards():
    clock = FakeClock(start_ms=5)
    with pytest.raises(ValueError):
        clock.advance(-1)
    assert clock.now_ms() == 5


def test_fake_clock_wraps_at_2_to_the_32():
    clock = FakeClock(start_ms=U32_MODULUS - 1)
    clock.advance(1)
    assert clock.now_ms() == 0
    clock.advance(5)
    assert clock.now_ms() == 5


def test_fake_clock_start_is_reduced_to_32_bits():
    assert FakeClock(start_ms=U32_MODULUS + 7).now_ms() == 7


def test_fake_clock_is_a_clock():
    assert isinstance(FakeClock(), Clock)
    assert isinstance(SystemClock(), Clock)


def test_system_clock_is_monotonic():
    clock = SystemClock()
    readings = [clock.now_ms() for _ in range(2000)]
    assert readings == sorted(readings)


def test_system_clock_reads_are_32_bit_values():
    assert 0 <= SystemClock().now_ms() < U32_MODULUS


def test_elapsed_simple_cases():
    assert elapsed_ms(1000, 400) == 600
    assert elapsed_ms(400, 400) == 0


def test_elapsed_across_the_wrap():
    assert elapsed_ms(5, U32_MODULUS - 5) == 10
    assert elapsed_ms(0, U32_MODULUS - 1) == 1
    assert elapsed_ms(0, 0) == 0


@given(earlier=u32, delta=st.integers(min_value=0, max_value=U32_MODULUS // 2 - 1))
def test_elapsed_recovers_any_forward_distance_even_across_the_wrap(earlier, delta):
    now = (earlier + delta) % U32_MODULUS
    assert elapsed_ms(now, earlier) == delta


@given(now=u32, earlier=u32)
def test_elapsed_is_always_a_valid_32_bit_value(now, earlier):
    assert 0 <= elapsed_ms(now, earlier) < U32_MODULUS


@given(start=u32, steps=st.lists(st.integers(min_value=0, max_value=100_000), max_size=20))
def test_fake_clock_elapsed_matches_total_advance(start, steps):
    clock = FakeClock(start_ms=start)
    for step in steps:
        clock.advance(step)
    assert elapsed_ms(clock.now_ms(), start) == sum(steps) % U32_MODULUS
