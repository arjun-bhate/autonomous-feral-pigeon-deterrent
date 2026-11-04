"""Tests for the settings loader (T003): defaults, environment override, validation."""

import os
import re
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from deterrent import config
from deterrent.config import (
    DEFAULT_CONFIG_PATH,
    MIN_DURATION_MS,
    PAN_SOFT_LIMIT_CDEG,
    TILT_SOFT_LIMIT_CDEG,
    Settings,
    load_settings,
)

# The table from tasks.md T003 / protocol.md section 8.
EXPECTED_DEFAULTS = {
    "heartbeat_interval_ms": 200,
    "watchdog_ms": 1000,
    "person_clear_holdoff_ms": 1000,
    "person_reengage_delay_ms": 5000,
    "fire_ttl_ms": 500,
    "fire_request_safety_margin_ms": 20,
    "command_ttl_ms": 500,
    "per_shot_ms": 5000,
    "cooldown_ms": 5000,
    "zone_upload_timeout_ms": 5000,
    "homing_duration_ms": 3000,
    "aiming_error_cdeg": 20,
    "max_zones": 8,
    "max_points_per_zone": 16,
    "elevation_min_cdeg": 0,
    "elevation_max_cdeg": 8000,
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Remove any PIGEON_ variable so the developer's shell cannot change a test."""
    for name in list(os.environ):
        if name.upper().startswith("PIGEON_"):
            monkeypatch.delenv(name)


def test_default_file_exists_in_config_folder():
    assert DEFAULT_CONFIG_PATH.name == "default.toml"
    assert DEFAULT_CONFIG_PATH.parent.name == "config"
    assert DEFAULT_CONFIG_PATH.is_file()


@pytest.mark.parametrize(("name", "value"), EXPECTED_DEFAULTS.items())
def test_every_default_equals_the_documented_table(name, value):
    assert getattr(load_settings(), name) == value


def test_every_default_is_also_written_in_the_toml_file():
    with DEFAULT_CONFIG_PATH.open("rb") as handle:
        data = tomllib.load(handle)
    for name, value in EXPECTED_DEFAULTS.items():
        assert data[name] == value
    assert data["dev_tools"] is True


def test_settings_have_no_field_missing_from_the_table():
    fields = set(Settings.model_fields) - {"dev_tools"}
    assert fields == set(EXPECTED_DEFAULTS)


def test_fixed_constants():
    assert MIN_DURATION_MS == 500
    assert PAN_SOFT_LIMIT_CDEG == 24000
    assert TILT_SOFT_LIMIT_CDEG == 8000


def test_fixed_constants_cannot_be_configured(monkeypatch):
    monkeypatch.setenv("PIGEON_MIN_DURATION_MS", "100")
    settings = load_settings()
    assert not hasattr(settings, "min_duration_ms")
    assert config.MIN_DURATION_MS == 500
    with pytest.raises(ValidationError):
        Settings(min_duration_ms=100)


def test_environment_override_changes_one_value(monkeypatch):
    monkeypatch.setenv("PIGEON_WATCHDOG_MS", "1500")
    settings = load_settings()
    assert settings.watchdog_ms == 1500
    assert settings.heartbeat_interval_ms == 200


def test_environment_override_wins_over_the_file(tmp_path, monkeypatch):
    path = tmp_path / "custom.toml"
    path.write_text(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("PIGEON_COOLDOWN_MS", "0")
    assert load_settings(path).cooldown_ms == 0


def test_a_custom_file_is_used_when_given(tmp_path):
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8").replace(
        "watchdog_ms = 1000", "watchdog_ms = 2000"
    )
    path = tmp_path / "custom.toml"
    path.write_text(text, encoding="utf-8")
    assert load_settings(path).watchdog_ms == 2000


def test_unknown_key_in_the_file_is_refused(tmp_path):
    path = tmp_path / "typo.toml"
    path.write_text("watchdog_msec = 1000\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)


def test_dev_tools_default_is_on():
    assert load_settings().dev_tools is True


@pytest.mark.parametrize(("text", "expected"), [("1", True), ("0", False)])
def test_dev_tools_environment_both_values(monkeypatch, text, expected):
    monkeypatch.setenv("PIGEON_DEV_TOOLS", text)
    assert load_settings().dev_tools is expected


@pytest.mark.parametrize(("text", "expected"), [("true", True), ("false", False)])
def test_dev_tools_accepts_words(monkeypatch, text, expected):
    monkeypatch.setenv("PIGEON_DEV_TOOLS", text)
    assert load_settings().dev_tools is expected


@pytest.mark.parametrize("value", [0, 1, 499])
def test_per_shot_below_minimum_duration_is_refused(value):
    with pytest.raises(ValidationError):
        Settings(per_shot_ms=value)


def test_per_shot_at_minimum_is_accepted():
    assert Settings(per_shot_ms=500).per_shot_ms == 500


def test_per_shot_below_500_from_environment_is_refused(monkeypatch):
    monkeypatch.setenv("PIGEON_PER_SHOT_MS", "499")
    with pytest.raises(ValidationError):
        load_settings()


@pytest.mark.parametrize("name", sorted(EXPECTED_DEFAULTS))
def test_negative_values_are_refused(name):
    with pytest.raises(ValidationError):
        Settings(**{name: -1})


@pytest.mark.parametrize(
    "name",
    [
        "heartbeat_interval_ms",
        "watchdog_ms",
        "fire_ttl_ms",
        "command_ttl_ms",
        "per_shot_ms",
        "max_zones",
        "max_points_per_zone",
    ],
)
def test_values_that_must_be_positive_refuse_zero(name):
    with pytest.raises(ValidationError):
        Settings(**{name: 0})


@pytest.mark.parametrize(
    "name", ["person_reengage_delay_ms", "cooldown_ms", "person_clear_holdoff_ms"]
)
def test_values_where_zero_means_disabled_accept_zero(name):
    assert getattr(Settings(**{name: 0}), name) == 0


def test_fire_ttl_is_limited_to_the_protocol_range():
    assert Settings(fire_ttl_ms=5000).fire_ttl_ms == 5000
    with pytest.raises(ValidationError):
        Settings(fire_ttl_ms=5001)


def test_zone_limits_are_bounded_by_the_protocol():
    # Three points are the minimum for a polygon; 9 zones is the ZONE_LIST payload limit.
    with pytest.raises(ValidationError):
        Settings(max_points_per_zone=2)
    with pytest.raises(ValidationError):
        Settings(max_zones=10)


def test_elevation_minimum_must_not_exceed_maximum():
    with pytest.raises(ValidationError):
        Settings(elevation_min_cdeg=5000, elevation_max_cdeg=4000)


def test_elevation_limits_stay_inside_the_tilt_soft_limit():
    with pytest.raises(ValidationError):
        Settings(elevation_max_cdeg=TILT_SOFT_LIMIT_CDEG + 1)


def test_default_file_contains_no_secrets():
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    with DEFAULT_CONFIG_PATH.open("rb") as handle:
        data = tomllib.load(handle)
    assert not re.search(r"password|secret|token|api_?key|credential", text, re.IGNORECASE)
    assert all(isinstance(value, int | bool) for value in data.values())


def test_config_module_does_not_read_dotenv_files():
    assert Settings.model_config.get("env_file") is None


def test_default_path_is_inside_the_repository():
    assert isinstance(DEFAULT_CONFIG_PATH, Path)
    assert (DEFAULT_CONFIG_PATH.parents[1] / "pyproject.toml").is_file()


def test_safety_margin_may_be_zero_but_not_negative():
    assert Settings(fire_request_safety_margin_ms=0).fire_request_safety_margin_ms == 0
    with pytest.raises(ValidationError):
        Settings(fire_request_safety_margin_ms=-1)


def test_safety_margin_must_be_shorter_than_the_fire_ttl():
    # A margin as long as the ttl would make every request born expired.
    Settings(fire_ttl_ms=500, fire_request_safety_margin_ms=499)
    with pytest.raises(ValidationError):
        Settings(fire_ttl_ms=500, fire_request_safety_margin_ms=500)
