"""Small-area geometry in meters for Buffalo road matching."""

from collections import defaultdict
from math import atan2, cos, degrees, floor, hypot, radians, sin

LAT_M = 111_195
LON_M = LAT_M * cos(radians(42.9))
CELL = 100


def xy(lat, lon):
    return lon * LON_M, lat * LAT_M


def distance(a, b):
    x, y = xy(*a)
    u, v = xy(*b)
    return hypot(x - u, y - v)


def heading(a, b):
    x, y = xy(*a)
    u, v = xy(*b)
    return degrees(atan2(u - x, v - y)) % 360


def offset(point, bearing, meters):
    angle = radians(bearing)
    return point[0] + cos(angle) * meters / LAT_M, point[1] + sin(angle) * meters / LON_M


def interpolate(a, b, fraction):
    return tuple(x + fraction * (y - x) for x, y in zip(a, b))


class Roads:
    def __init__(self, rows):
        self.cells = defaultdict(list)
        for way_id, name, coordinates in rows:
            for a, b in zip(coordinates, coordinates[1:]):
                start, end = xy(a[1], a[0]), xy(b[1], b[0])
                segment = (way_id, name, start, end)
                for i in range(floor(min(start[0], end[0]) / CELL), floor(max(start[0], end[0]) / CELL) + 1):
                    for j in range(floor(min(start[1], end[1]) / CELL), floor(max(start[1], end[1]) / CELL) + 1):
                        self.cells[i, j].append(segment)

    def snap(self, point, radius=40, preferred=None):
        x, y = xy(*point)
        best = None
        for i in range(floor((x - radius) / CELL), floor((x + radius) / CELL) + 1):
            for j in range(floor((y - radius) / CELL), floor((y + radius) / CELL) + 1):
                for way, name, (a, b), (c, d) in self.cells[i, j]:
                    dx, dy = c - a, d - b
                    length = dx * dx + dy * dy
                    if not length:
                        continue
                    t = min(1, max(0, ((x - a) * dx + (y - b) * dy) / length))
                    u, v = a + t * dx, b + t * dy
                    gap = hypot(x - u, y - v)
                    rank = (gap + (3 if preferred is not None and way != preferred else 0), way)
                    if gap <= radius and (best is None or rank < best[0]):
                        best = (rank, (v / LAT_M, u / LON_M), way, name, gap)
        return best[1:] if best else None


def point_on_route(route, fraction):
    segments = [(a, b, distance(a, b)) for a, b in zip(route, route[1:]) if a != b]
    remaining = sum(length for _, _, length in segments) * min(1, max(0, fraction))
    for a, b, length in segments:
        if remaining <= length:
            return interpolate(a, b, remaining / length), heading(a, b)
        remaining -= length
    a, b, _ = segments[-1]
    return b, heading(a, b)
