"""Settings for the deterrent system.

Every tunable value lives in ``config/default.toml``. ``load_settings`` reads that
file, lets an environment variable named ``PIGEON_<NAME>`` override any single
value, and refuses to start on nonsense (a negative timeout, a shot shorter than
the protocol minimum). The values that are fixed by the protocol are plain
constants below and cannot be configured.
"""

from pathlib import Path
from typing import Annotated

from pydantic import Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

# Fixed by the protocol (protocol.md sections 1 and 4.1). Not settings.
MIN_DURATION_MS = 500
PAN_SOFT_LIMIT_CDEG = 24000
TILT_SOFT_LIMIT_CDEG = 8000

# ZONE_LIST must fit the 59-byte payload: 6 bytes per zone gives at most 9 zones.
_MAX_ZONES_PER_LIST = 9
_MIN_POINTS_FOR_POLYGON = 3
_MAX_TTL_MS = 5000  # protocol: ttl_ms is 1 to 5000

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.toml"

NonNegative = Annotated[int, Field(ge=0)]
Positive = Annotated[int, Field(ge=1)]


class Settings(BaseSettings):
    """All tunable values, validated. Build it with ``load_settings``."""

    model_config = SettingsConfigDict(
        env_prefix="PIGEON_",
        extra="forbid",
        toml_file=DEFAULT_CONFIG_PATH,
    )

    heartbeat_interval_ms: Positive
    watchdog_ms: Positive
    person_clear_holdoff_ms: NonNegative
    person_reengage_delay_ms: NonNegative
    fire_ttl_ms: Annotated[int, Field(ge=1, le=_MAX_TTL_MS)]
    command_ttl_ms: Positive
    per_shot_ms: Annotated[int, Field(ge=MIN_DURATION_MS)]
    cooldown_ms: NonNegative
    zone_upload_timeout_ms: Positive
    homing_duration_ms: NonNegative
    aiming_error_cdeg: NonNegative
    max_zones: Annotated[int, Field(ge=1, le=_MAX_ZONES_PER_LIST)]
    max_points_per_zone: Annotated[int, Field(ge=_MIN_POINTS_FOR_POLYGON)]
    elevation_min_cdeg: NonNegative
    elevation_max_cdeg: Annotated[int, Field(ge=0, le=TILT_SOFT_LIMIT_CDEG)]
    dev_tools: bool = True

    @model_validator(mode="after")
    def _elevation_range_is_ordered(self) -> "Settings":
        if self.elevation_min_cdeg > self.elevation_max_cdeg:
            raise ValueError("elevation_min_cdeg must not exceed elevation_max_cdeg")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Highest priority first: explicit arguments, then environment, then the file.
        # No .env or secrets-folder source is used: this project keeps no secrets here.
        return (init_settings, env_settings, TomlConfigSettingsSource(settings_cls))


def load_settings(toml_path: Path | str = DEFAULT_CONFIG_PATH) -> Settings:
    """Load settings from ``toml_path`` with environment overrides applied."""

    class _FileSettings(Settings):
        model_config = SettingsConfigDict(toml_file=Path(toml_path))

    return _FileSettings()
