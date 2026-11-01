"""Tests for integer polygon checks: crossing, duplicates, zero area (T010)."""

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from deterrent.geometry.polygon import (
    has_duplicate_consecutive_points,
    has_self_crossing,
    has_zero_area,
    segments_intersect,
    signed_area_x2,
)

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]
L_SHAPE = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)]
BOW_TIE = [(0, 0), (10, 10), (10, 0), (0, 10)]
TRIANGLE = [(0, 0), (10, 0), (5, 8)]
# Vertex (5, 0) lies exactly on the edge from (0, 0) to (10, 0).
VERTEX_ON_EDGE = [(0, 0), (10, 0), (10, 10), (5, 0), (0, 10)]
# Edge E-F (10,0)-(3,0) overlaps edge A-B (0,0)-(6,0) along a line; they are not neighbours.
COLLINEAR_OVERLAP = [(0, 0), (6, 0), (6, 4), (10, 4), (10, 0), (3, 0)]


# ---- segments -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "c", "d", "expected"),
    [
        ((0, 0), (10, 10), (0, 10), (10, 0), True),  # proper X crossing
        ((0, 0), (10, 0), (0, 1), (10, 1), False),  # parallel apart
        ((0, 0), (10, 0), (5, 0), (5, 5), True),  # T: end point touches interior
        ((0, 0), (10, 0), (10, 0), (10, 5), True),  # touching at a shared end point
        ((0, 0), (10, 0), (11, 0), (20, 0), False),  # collinear, disjoint
        ((0, 0), (10, 0), (10, 0), (20, 0), True),  # collinear, touching at one point
        ((0, 0), (10, 0), (5, 0), (15, 0), True),  # collinear overlap
        ((0, 0), (10, 0), (2, 0), (4, 0), True),  # collinear, one inside the other
        ((0, 0), (10, 0), (5, 1), (5, 5), False),  # beside, would cross if extended
        ((0, 0), (4, 4), (5, 5), (9, 9), False),  # collinear diagonal, disjoint
    ],
)
def test_segments_intersect_cases(a, b, c, d, expected):
    assert segments_intersect(a, b, c, d) is expected


point = st.tuples(st.integers(-50, 50), st.integers(-50, 50))


@given(point, point, point, point)
def test_segments_intersect_is_symmetric(a, b, c, d):
    result = segments_intersect(a, b, c, d)
    assert segments_intersect(c, d, a, b) is result
    assert segments_intersect(b, a, c, d) is result
    assert segments_intersect(a, b, d, c) is result


# ---- polygons -----------------------------------------------------------------------------


def test_bow_tie_crosses():
    assert has_self_crossing(BOW_TIE)


@pytest.mark.parametrize("polygon", [SQUARE, L_SHAPE, TRIANGLE])
def test_simple_polygons_do_not_cross(polygon):
    assert not has_self_crossing(polygon)


def test_vertex_touching_another_edge_counts_as_crossing():
    assert has_self_crossing(VERTEX_ON_EDGE)


def test_collinear_overlap_of_edges_that_are_not_neighbours_counts_as_crossing():
    assert has_self_crossing(COLLINEAR_OVERLAP)


def test_a_neighbouring_edge_folding_back_over_the_previous_one_counts_as_crossing():
    # B-C goes back along A-B: the edges share only B, but overlap on a line.
    assert has_self_crossing([(0, 0), (10, 0), (4, 0), (4, 6)])


def test_closing_edge_is_checked_too():
    # The closing edge (10,6)->(0,0) crosses the second edge (10,0)->(0,6).
    assert has_self_crossing([(0, 0), (10, 0), (0, 6), (10, 6)])


def test_a_triangle_in_either_direction_is_simple():
    assert not has_self_crossing(TRIANGLE)
    assert not has_self_crossing(TRIANGLE[::-1])


def test_a_shared_vertex_visited_twice_counts_as_crossing():
    # A figure eight that meets itself exactly at (5, 5).
    assert has_self_crossing([(0, 0), (5, 5), (10, 0), (10, 10), (5, 5), (0, 10)])


def test_fewer_than_three_points_are_not_a_polygon():
    with pytest.raises(ValueError):
        has_self_crossing([(0, 0), (1, 1)])


# ---- duplicates and area --------------------------------------------------------------------


def test_repeated_consecutive_point_is_degenerate():
    assert has_duplicate_consecutive_points([(0, 0), (10, 0), (10, 0), (5, 5)])


def test_closing_edge_with_equal_ends_is_degenerate():
    assert has_duplicate_consecutive_points([(0, 0), (10, 0), (5, 5), (0, 0)])


def test_distinct_points_are_not_degenerate():
    assert not has_duplicate_consecutive_points(SQUARE)


def test_the_same_point_twice_but_not_in_a_row_is_not_a_consecutive_duplicate():
    assert not has_duplicate_consecutive_points([(0, 0), (10, 0), (5, 5), (10, 0), (7, 9)])


def test_collinear_points_have_zero_area():
    assert has_zero_area([(0, 0), (5, 5), (10, 10)])
    assert has_zero_area([(0, 0), (3, 0), (7, 0), (10, 0)])


def test_all_points_equal_has_zero_area():
    assert has_zero_area([(2, 2), (2, 2), (2, 2)])


def test_areas_of_known_shapes():
    assert signed_area_x2(SQUARE) == 200
    assert signed_area_x2(SQUARE[::-1]) == -200
    assert signed_area_x2(TRIANGLE) == 80
    assert not has_zero_area(SQUARE)
    assert not has_zero_area(L_SHAPE)


def test_bow_tie_has_zero_signed_area_but_is_caught_by_crossing():
    assert signed_area_x2(BOW_TIE) == 0
    assert has_self_crossing(BOW_TIE)


# ---- properties ---------------------------------------------------------------------------


def convex_hull(points):
    """Andrew's monotone chain; strictly convex, counter-clockwise, no collinear points."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

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


hull_inputs = st.lists(
    st.tuples(st.integers(-1000, 1000), st.integers(-1000, 1000)), min_size=3, max_size=14
)


@given(hull_inputs)
def test_random_convex_polygons_never_report_crossing(points):
    hull = convex_hull(points)
    assume(len(hull) >= 3)
    assert not has_self_crossing(hull)
    assert not has_self_crossing(hull[::-1])
    assert not has_duplicate_consecutive_points(hull)
    assert not has_zero_area(hull)


@given(hull_inputs, st.data())
def test_swapping_two_neighbouring_vertices_of_a_convex_polygon_is_caught(points, data):
    hull = convex_hull(points)
    assume(len(hull) >= 4)
    index = data.draw(st.integers(0, len(hull) - 1))
    other = (index + 1) % len(hull)
    swapped = list(hull)
    swapped[index], swapped[other] = swapped[other], swapped[index]
    assert has_self_crossing(swapped)
