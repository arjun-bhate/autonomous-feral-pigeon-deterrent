"""Zones on the simulated device: the upload session and (later) commit and the store rules.

A *zone* is a polygon the effector must never point into. It reaches the device in
three steps: ZONE_BEGIN (declare the id and the number of points), ZONE_POINT (one
message per point, in order) and ZONE_COMMIT (check everything and store it).

Until commit, the points live only in a *staging buffer* in memory. That buffer is
never written to storage, never used to decide a shot, and is thrown away whenever
the device leaves ZONE_SETUP by any route. Only an accepted commit turns it into a
stored zone.
"""

from dataclasses import dataclass, field

from deterrent.clock import elapsed_ms
from deterrent.config import Settings
from deterrent.protocol.codes import RejectCode
from deterrent.simulator.zone_storage import StorageError, ZoneStorage

_MIN_POINTS = 3


@dataclass
class _Upload:
    zone_id: int
    point_count: int
    points: list[tuple[int, int]] = field(default_factory=list)
    last_activity_ms: int = 0


class ZoneService:
    def __init__(self, storage: ZoneStorage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings
        self._upload: _Upload | None = None

    # ---- looking at the upload ------------------------------------------------------------

    @property
    def upload_active(self) -> bool:
        return self._upload is not None

    def upload_summary(self) -> tuple[int, int, int] | None:
        """(zone_id, declared point count, points received so far), or None if no upload."""
        if self._upload is None:
            return None
        return (self._upload.zone_id, self._upload.point_count, len(self._upload.points))

    def upload_points(self) -> list[tuple[int, int]]:
        return list(self._upload.points) if self._upload else []

    # ---- the upload messages --------------------------------------------------------------

    def begin(self, zone_id: int, point_count: int, now_ms: int) -> RejectCode | None:
        """Start (or restart) an upload. A rejected begin changes nothing at all."""
        if point_count < _MIN_POINTS:
            return RejectCode.ZONE_TOO_FEW_POINTS
        if point_count > self._settings.max_points_per_zone:
            return RejectCode.ZONE_TOO_MANY_POINTS
        try:
            stored = len(self._storage.read_all())
        except StorageError:
            return RejectCode.ZONE_STORAGE_ERROR  # cannot tell whether there is room: refuse
        if stored >= self._settings.max_zones:
            return RejectCode.ZONE_STORE_FULL
        self._upload = _Upload(zone_id, point_count, last_activity_ms=now_ms)
        return None

    def point(self, index: int, pan: int, tilt: int, now_ms: int) -> RejectCode | None:
        """Add the next point. A wrong index is refused and the upload is kept."""
        if self._upload is None:
            return RejectCode.ZONE_NO_UPLOAD
        upload = self._upload
        if index != len(upload.points) or index >= upload.point_count:
            return RejectCode.ZONE_BAD_INDEX
        upload.points.append((pan, tilt))
        upload.last_activity_ms = now_ms
        return None

    # ---- ending an upload ------------------------------------------------------------------

    def timed_out(self, now_ms: int) -> bool:
        """True if an upload exists and no accepted upload message arrived for too long."""
        return (
            self._upload is not None
            and elapsed_ms(now_ms, self._upload.last_activity_ms)
            >= self._settings.zone_upload_timeout_ms
        )

    def discard(self) -> None:
        self._upload = None
