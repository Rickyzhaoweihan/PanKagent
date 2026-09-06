import math
from collections import defaultdict
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Rect = Tuple[float, float, float, float]
Segment = Tuple[Point, Point]

EPSILON = 1e-7


def rect_for(point: Dict[str, float]) -> Rect:
    width = float(point.get("width", 120.0))
    height = float(point.get("height", 44.0))
    x = float(point["x"])
    y = float(point["y"])
    return (x - width / 2, y - height / 2, x + width / 2, y + height / 2)


def expand_rect(rect: Rect, amount: float) -> Rect:
    return (
        rect[0] - amount,
        rect[1] - amount,
        rect[2] + amount,
        rect[3] + amount,
    )


def rects_overlap(left: Rect, right: Rect, gap: float = 0.0) -> bool:
    return not (
        left[2] + gap <= right[0]
        or right[2] + gap <= left[0]
        or left[3] + gap <= right[1]
        or right[3] + gap <= left[1]
    )


def overlap_area(left: Rect, right: Rect) -> float:
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return width * height


def orientation(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def point_on_segment(point: Point, start: Point, end: Point) -> bool:
    return (
        abs(orientation(start, end, point)) <= EPSILON
        and min(start[0], end[0]) - EPSILON <= point[0] <= max(start[0], end[0]) + EPSILON
        and min(start[1], end[1]) - EPSILON <= point[1] <= max(start[1], end[1]) + EPSILON
    )


def segments_intersect(left: Segment, right: Segment, include_endpoints: bool = True) -> bool:
    a, b = left
    c, d = right
    o1 = orientation(a, b, c)
    o2 = orientation(a, b, d)
    o3 = orientation(c, d, a)
    o4 = orientation(c, d, b)
    proper = ((o1 > EPSILON and o2 < -EPSILON) or (o1 < -EPSILON and o2 > EPSILON)) and (
        (o3 > EPSILON and o4 < -EPSILON) or (o3 < -EPSILON and o4 > EPSILON)
    )
    if proper:
        return True
    if not include_endpoints:
        return False
    return (
        (abs(o1) <= EPSILON and point_on_segment(c, a, b))
        or (abs(o2) <= EPSILON and point_on_segment(d, a, b))
        or (abs(o3) <= EPSILON and point_on_segment(a, c, d))
        or (abs(o4) <= EPSILON and point_on_segment(b, c, d))
    )


def segment_intersects_rect(segment: Segment, rect: Rect, allow_boundary: bool = False) -> bool:
    (x1, y1), (x2, y2) = segment
    left, top, right, bottom = rect
    strictly_inside = lambda x, y: left < x < right and top < y < bottom
    if (
        strictly_inside(x1, y1)
        or strictly_inside(x2, y2)
        or strictly_inside((x1 + x2) / 2, (y1 + y2) / 2)
    ):
        return True
    edges = [
        ((left, top), (right, top)),
        ((right, top), (right, bottom)),
        ((right, bottom), (left, bottom)),
        ((left, bottom), (left, top)),
    ]
    return any(
        segments_intersect(segment, edge, include_endpoints=not allow_boundary)
        for edge in edges
    )


def segment_length(segment: Segment) -> float:
    return math.hypot(segment[1][0] - segment[0][0], segment[1][1] - segment[0][1])


def route_points(route: Dict[str, object]) -> List[Point]:
    source = tuple(route.get("source_port", ()))  # type: ignore[arg-type]
    target = tuple(route.get("target_port", ()))  # type: ignore[arg-type]
    if len(source) != 2 or len(target) != 2:
        return []
    middle_key = "control_points" if route.get("route_type") == "bezier" else "waypoints"
    middle = [tuple(point) for point in route.get(middle_key, [])]  # type: ignore[arg-type]
    return [source, *middle, target]  # type: ignore[list-item]


def _cubic_point(start: Point, control_a: Point, control_b: Point, end: Point, t: float) -> Point:
    """Return a point on a cubic Bezier without a numerical dependency."""
    inverse = 1.0 - t
    return (
        inverse ** 3 * start[0]
        + 3 * inverse ** 2 * t * control_a[0]
        + 3 * inverse * t ** 2 * control_b[0]
        + t ** 3 * end[0],
        inverse ** 3 * start[1]
        + 3 * inverse ** 2 * t * control_a[1]
        + 3 * inverse * t ** 2 * control_b[1]
        + t ** 3 * end[1],
    )


def route_segments(route: Dict[str, object]) -> List[Segment]:
    # Two controls denote the new explicit cubic route.  Sample it sparsely for
    # collision/crossing metrics; legacy one-control routes retain their old
    # polyline-compatible interpretation.
    if route.get("route_type") == "bezier":
        points = route_points(route)
        if len(points) == 4:
            sampled = [_cubic_point(points[0], points[1], points[2], points[3], step / 6)
                       for step in range(7)]
            return list(zip(sampled, sampled[1:]))
    points = route_points(route)
    return list(zip(points, points[1:]))


def simplify_polyline(points: Sequence[Point]) -> List[Point]:
    simplified: List[Point] = []
    for point in points:
        if simplified and point == simplified[-1]:
            continue
        if len(simplified) >= 2 and abs(orientation(simplified[-2], simplified[-1], point)) <= EPSILON:
            simplified[-1] = point
        else:
            simplified.append(point)
    return simplified


class SpatialHash:
    """Small sparse rectangle index used by placement and routing."""

    def __init__(self, cell_size: float = 128.0):
        self.cell_size = max(float(cell_size), 1.0)
        self._cells = defaultdict(set)
        self._rects: Dict[str, Rect] = {}

    def _keys(self, rect: Rect) -> Iterator[Tuple[int, int]]:
        min_x = math.floor(rect[0] / self.cell_size)
        max_x = math.floor(rect[2] / self.cell_size)
        min_y = math.floor(rect[1] / self.cell_size)
        max_y = math.floor(rect[3] / self.cell_size)
        for grid_x in range(min_x, max_x + 1):
            for grid_y in range(min_y, max_y + 1):
                yield grid_x, grid_y

    def insert(self, item_id: str, rect: Rect) -> None:
        self._rects[item_id] = rect
        for key in self._keys(rect):
            self._cells[key].add(item_id)

    def remove(self, item_id: str) -> None:
        rect = self._rects.pop(item_id, None)
        if rect is None:
            return
        for key in self._keys(rect):
            self._cells[key].discard(item_id)

    def query(self, rect: Rect) -> Iterable[Tuple[str, Rect]]:
        found = set()
        for key in self._keys(rect):
            found.update(self._cells.get(key, ()))
        return ((item_id, self._rects[item_id]) for item_id in sorted(found))

    def overlaps(self, item_id: str, rect: Rect, gap: float) -> bool:
        query_rect = expand_rect(rect, gap)
        return any(
            other_id != item_id and rects_overlap(rect, other_rect, gap)
            for other_id, other_rect in self.query(query_rect)
        )
