"""Polygon checks on whole-number coordinates.

A *zone* is a polygon of (pan, tilt) points in centidegrees. Every check here uses
only integer arithmetic (no floating point), so the answer is exact: a point is never
"almost" on an edge because of rounding. The polygon closes by itself: the last
point connects back to the first.

Part 1: is the polygon usable at all (no repeated points, non-zero area, no edges
crossing or touching). Part 2 (bottom of the file): is a point inside the polygon or
within the safety margin of its edges.
"""

from collections.abc import Sequence

Point = tuple[int, int]


def _cross(origin: Point, a: Point, b: Point) -> int:
    """Cross product of (a - origin) and (b - origin).

    Positive: b is to the left of the line origin->a. Negative: to the right. Zero:
    the three points are on one straight line.
    """
    return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _within_box(a: Point, b: Point, p: Point) -> bool:
    """Is p inside the bounding box of a and b? (Used only when p is on the line a-b.)"""
    return min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Do the closed segments a-b and c-d share at least one point?

    Touching at an end point and overlapping along a line both count.
    """
    o1 = _sign(_cross(a, b, c))
    o2 = _sign(_cross(a, b, d))
    o3 = _sign(_cross(c, d, a))
    o4 = _sign(_cross(c, d, b))
    if o1 != o2 and o3 != o4:
        return True  # the segments properly cross
    # Otherwise they can only meet if an end point lies on the other segment.
    return (
        (o1 == 0 and _within_box(a, b, c))
        or (o2 == 0 and _within_box(a, b, d))
        or (o3 == 0 and _within_box(c, d, a))
        or (o4 == 0 and _within_box(c, d, b))
    )


def _fold_back(a: Point, b: Point, c: Point) -> bool:
    """For edges a-b and b-c that share b: does c-b run back over a-b along one line?"""
    return _cross(b, a, c) == 0 and (
        (a[0] - b[0]) * (c[0] - b[0]) + (a[1] - b[1]) * (c[1] - b[1]) > 0
    )


def has_self_crossing(points: Sequence[Point]) -> bool:
    """Do any two edges of the closed polygon cross, touch, or overlap?

    Two edges that merely meet at the vertex they share are fine. Everything else
    counts, including a vertex touching another edge and edges overlapping along a
    line. Raises ValueError for fewer than 3 points.
    """
    count = len(points)
    if count < 3:
        raise ValueError("a polygon needs at least 3 points")
    edges = [(points[i], points[(i + 1) % count]) for i in range(count)]
    for i in range(count):
        for j in range(i + 1, count):
            (a, b), (c, d) = edges[i], edges[j]
            if j == i + 1:  # neighbours sharing b == c
                if _fold_back(a, b, d):
                    return True
            elif i == 0 and j == count - 1:  # the closing edge shares a start point with edge 0
                if _fold_back(b, a, c):
                    return True
            elif segments_intersect(a, b, c, d):
                return True
    return False


def has_duplicate_consecutive_points(points: Sequence[Point]) -> bool:
    """Is any point equal to the next one, counting last-to-first (the closing edge)?"""
    count = len(points)
    return any(points[i] == points[(i + 1) % count] for i in range(count))


def signed_area_x2(points: Sequence[Point]) -> int:
    """Twice the signed area (shoelace formula). Positive if the points run counter-clockwise."""
    count = len(points)
    return sum(
        points[i][0] * points[(i + 1) % count][1] - points[(i + 1) % count][0] * points[i][1]
        for i in range(count)
    )


def has_zero_area(points: Sequence[Point]) -> bool:
    """True if the polygon encloses no area (all points on one line, or all equal)."""
    return signed_area_x2(points) == 0


# ---------------------------------------------------------------------------------------
# Part 2: point in polygon and the safety margin
# ---------------------------------------------------------------------------------------

BASE_ZONE_MARGIN_CDEG = 50


def zone_margin_cdeg(aiming_error_cdeg: int) -> int:
    """The safety margin around a zone: a fixed 50 plus the simulated aiming error."""
    if aiming_error_cdeg < 0:
        raise ValueError("aiming error cannot be negative")
    return BASE_ZONE_MARGIN_CDEG + aiming_error_cdeg


def _on_segment(a: Point, b: Point, p: Point) -> bool:
    return _cross(a, b, p) == 0 and _within_box(a, b, p)


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Is ``point`` inside the polygon, by the even-odd rule? The boundary counts as inside.

    Even-odd rule: draw a ray from the point straight to the right and count how many
    edges it crosses. An odd count means inside. No division is used: whether an
    edge crosses to the right of the point is decided from the sign of a cross product.
    """
    count = len(polygon)
    if count < 3:
        raise ValueError("a polygon needs at least 3 points")
    inside = False
    for i in range(count):
        a, b = polygon[i], polygon[(i + 1) % count]
        if _on_segment(a, b, point):
            return True
        # Count the edge only if it straddles the point's height (half-open, so a ray
        # through a vertex is counted once, not twice).
        if (a[1] > point[1]) != (b[1] > point[1]):
            side = _cross(a, b, point)  # sign tells on which side of the edge the point is
            if (side > 0) == (b[1] > a[1]):
                inside = not inside
    return inside


def _within_distance_of_segment(p: Point, a: Point, b: Point, distance: int) -> bool:
    """Is the distance from p to the segment a-b at most ``distance``? Exact, integers only.

    Compares squared distances. When p projects onto the middle of the segment, the
    squared distance is cross**2 / length**2, so both sides are multiplied by
    length**2 instead of dividing.
    """
    limit = distance * distance
    abx, aby = b[0] - a[0], b[1] - a[1]
    apx, apy = p[0] - a[0], p[1] - a[1]
    length2 = abx * abx + aby * aby
    along = apx * abx + apy * aby
    if length2 == 0 or along <= 0:
        return apx * apx + apy * apy <= limit  # nearest point is a
    if along >= length2:
        bpx, bpy = p[0] - b[0], p[1] - b[1]
        return bpx * bpx + bpy * bpy <= limit  # nearest point is b
    cross = abx * apy - aby * apx
    return cross * cross <= limit * length2


def in_zone_margin(point: Point, polygon: Sequence[Point], margin_cdeg: int) -> bool:
    """Is the point inside the zone, or within ``margin_cdeg`` of any of its edges?"""
    if point_in_polygon(point, polygon):
        return True
    count = len(polygon)
    return any(
        _within_distance_of_segment(point, polygon[i], polygon[(i + 1) % count], margin_cdeg)
        for i in range(count)
    )
