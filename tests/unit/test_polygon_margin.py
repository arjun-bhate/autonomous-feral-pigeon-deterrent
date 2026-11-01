"""Tests for point-in-polygon and the zone margin (T011)."""

import ast
from pathlib import Path

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from deterrent.config import load_settings
from deterrent.geometry.polygon import (
    BASE_ZONE_MARGIN_CDEG,
    in_zone_margin,
    point_in_polygon,
    zone_margin_cdeg,
)

POLYGON_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "deterrent" / "geometry" / "polygon.py"
)

SQUARE = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
L_SHAPE = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)]
SLANTED_TRIANGLE = [(0, 0), (300, 400), (300, 0)]


# ---- the margin setting ----------------------------------------------------------------


def test_margin_is_50_plus_the_aiming_error():
    assert BASE_ZONE_MARGIN_CDEG == 50
    assert zone_margin_cdeg(20) == 70
    assert zone_margin_cdeg(0) == 50


def test_default_settings_give_a_margin_of_70():
    assert zone_margin_cdeg(load_settings().aiming_error_cdeg) == 70


def test_margin_follows_the_aiming_error_setting():
    # 0 setting: margin 50; 20 setting: margin 70. A point 60 outside is in one, not the other.
    point = (-60, 500)
    assert not in_zone_margin(point, SQUARE, zone_margin_cdeg(0))
    assert in_zone_margin(point, SQUARE, zone_margin_cdeg(20))


def test_negative_aiming_error_is_refused():
    with pytest.raises(ValueError):
        zone_margin_cdeg(-1)


# ---- point in polygon (even-odd) -----------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        ((500, 500), True),
        ((-1, 500), False),
        ((1001, 500), False),
        ((500, -1), False),
        ((500, 1001), False),
    ],
)
def test_point_in_square(point, expected):
    assert point_in_polygon(point, SQUARE) is expected


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        ((2, 2), True),
        ((2, 7), True),  # in the tall arm
        ((7, 2), True),  # in the wide arm
        ((7, 7), False),  # in the notch of the L
        ((12, 4), False),  # level with the vertices, outside on the right
        ((-1, 4), False),  # level with the vertices, outside on the left
        ((2, 4), True),  # level with the vertices, inside
        ((11, 0), False),
    ],
)
def test_point_in_l_shape_handles_concave_corners_and_vertex_heights(point, expected):
    assert point_in_polygon(point, L_SHAPE) is expected


def test_points_on_the_boundary_count_as_inside():
    assert point_in_polygon((0, 0), SQUARE)  # vertex
    assert point_in_polygon((1000, 1000), SQUARE)  # vertex
    assert point_in_polygon((500, 0), SQUARE)  # on an edge
    assert point_in_polygon((1000, 300), SQUARE)
    assert point_in_polygon((7, 4), L_SHAPE)  # on the inner horizontal edge


def test_either_winding_direction_gives_the_same_answer():
    for point in [(2, 2), (7, 7), (12, 4), (7, 4)]:
        assert point_in_polygon(point, L_SHAPE) == point_in_polygon(point, L_SHAPE[::-1])


def test_polygon_needs_three_points():
    with pytest.raises(ValueError):
        point_in_polygon((0, 0), [(0, 0), (1, 1)])
    with pytest.raises(ValueError):
        in_zone_margin((0, 0), [(0, 0), (1, 1)], 70)


# ---- the margin test -------------------------------------------------------------------


def test_inside_is_in_margin():
    assert in_zone_margin((500, 500), SQUARE, 70)
    assert in_zone_margin((500, 500), SQUARE, 0)


def test_outside_by_margin_plus_one_is_not_in_margin():
    margin = 70
    assert not in_zone_margin((-margin - 1, 500), SQUARE, margin)
    assert not in_zone_margin((1000 + margin + 1, 500), SQUARE, margin)
    assert not in_zone_margin((500, -margin - 1), SQUARE, margin)
    assert not in_zone_margin((500, 1000 + margin + 1), SQUARE, margin)


def test_exactly_at_margin_distance_is_in_margin():
    margin = 70
    assert in_zone_margin((-margin, 500), SQUARE, margin)
    assert in_zone_margin((1000 + margin, 500), SQUARE, margin)
    assert in_zone_margin((500, -margin), SQUARE, margin)
    assert in_zone_margin((500, 1000 + margin), SQUARE, margin)


def test_corner_distance_uses_the_straight_line_to_the_vertex():
    # 42-56-70 is a whole-number right triangle: the corner (0,0) is exactly 70 away.
    assert in_zone_margin((-42, -56), SQUARE, 70)
    assert not in_zone_margin((-42, -57), SQUARE, 70)
    # A point 70 away along each axis is farther than 70 from the corner itself.
    assert not in_zone_margin((-70, -70), SQUARE, 70)
    assert in_zone_margin((-49, -49), SQUARE, 70)  # 49*sqrt(2) = 69.3


def test_slanted_edge_at_exactly_the_margin_distance():
    # The point (110, 230) is exactly 50 from the edge (0,0)-(300,400) (cross 25000 / length 500).
    point = (110, 230)
    assert not point_in_polygon(point, SLANTED_TRIANGLE)
    assert in_zone_margin(point, SLANTED_TRIANGLE, 50)
    assert not in_zone_margin(point, SLANTED_TRIANGLE, 49)


def test_vertex_and_edge_points_are_in_margin_even_with_margin_zero():
    for point in [(0, 0), (1000, 1000), (500, 0), (0, 700)]:
        assert in_zone_margin(point, SQUARE, 0)


def test_margin_zero_does_not_include_outside_points():
    assert not in_zone_margin((-1, 500), SQUARE, 0)


def test_the_margin_reaches_across_the_notch_of_a_concave_polygon():
    # (7, 7) is in the notch, 3 from the nearest edge (x=4 wall: 3, y=4 floor: 3).
    assert in_zone_margin((7, 7), L_SHAPE, 3)
    assert not in_zone_margin((7, 7), L_SHAPE, 2)


def test_degenerate_zero_length_edges_do_not_break_the_distance():
    # Not a valid zone, but the function must not crash or divide by zero.
    assert in_zone_margin((5, 5), [(0, 0), (0, 0), (10, 0), (10, 10)], 5)


# ---- properties ----------------------------------------------------------------------------


def hull(points):
    pts = sorted(set(points))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


coordinate = st.integers(-500, 500)
polygon_points = st.lists(st.tuples(coordinate, coordinate), min_size=3, max_size=10)


@given(polygon_points, st.tuples(coordinate, coordinate), st.integers(0, 300))
def test_result_is_identical_for_translated_and_rotated_by_90_polygons(points, probe, margin):
    polygon = hull(points)
    assume(len(polygon) >= 3)
    expected = in_zone_margin(probe, polygon, margin)
    # Translate by (dx, dy).
    dx, dy = 1234, -777
    moved = [(x + dx, y + dy) for x, y in polygon]
    assert in_zone_margin((probe[0] + dx, probe[1] + dy), moved, margin) is expected
    # Rotate by 90 degrees: (x, y) -> (-y, x). Repeat four times to cover all rotations.
    rotated, p = polygon, probe
    for _ in range(4):
        rotated = [(-y, x) for x, y in rotated]
        p = (-p[1], p[0])
        assert in_zone_margin(p, rotated, margin) is expected


@given(polygon_points, st.tuples(coordinate, coordinate), st.integers(0, 300))
def test_rotation_keeps_point_in_polygon_too(points, probe, _margin):
    polygon = hull(points)
    assume(len(polygon) >= 3)
    rotated = [(-y, x) for x, y in polygon]
    assert point_in_polygon((-probe[1], probe[0]), rotated) is point_in_polygon(probe, polygon)


@given(polygon_points, st.tuples(coordinate, coordinate), st.integers(0, 200), st.integers(0, 200))
def test_a_bigger_margin_never_removes_a_point(points, probe, margin, extra):
    polygon = hull(points)
    assume(len(polygon) >= 3)
    if in_zone_margin(probe, polygon, margin):
        assert in_zone_margin(probe, polygon, margin + extra)


@given(polygon_points, st.tuples(coordinate, coordinate), st.integers(0, 200))
def test_inside_always_implies_in_margin(points, probe, margin):
    polygon = hull(points)
    assume(len(polygon) >= 3)
    if point_in_polygon(probe, polygon):
        assert in_zone_margin(probe, polygon, margin)


# ---- no floating point --------------------------------------------------------------------


def test_the_geometry_module_uses_no_floating_point():
    tree = ast.parse(POLYGON_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            assert not isinstance(node.value, float), "float literal"
        if isinstance(node, ast.BinOp):
            assert not isinstance(node.op, ast.Div), "true division"
        if isinstance(node, ast.Import):
            assert all(alias.name != "math" for alias in node.names), "import math"
        if isinstance(node, ast.ImportFrom):
            assert node.module != "math", "import from math"
        if isinstance(node, ast.Name):
            assert node.id not in {"float", "sqrt", "hypot"}, node.id
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"sqrt", "hypot", "isqrt"}, node.attr
