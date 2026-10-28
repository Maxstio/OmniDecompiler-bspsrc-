"""Building, and merging, brushes from polygons."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from .geometry import box_polyhedron, brush_windings, winding_area, winding_center
from .mapdata import Brush, Side, default_axes, pick_plane_points
from .vecmath import Vec3, add, cross, dot, length, neg, normalize, scale, sub

NODRAW = "TOOLS/TOOLSNODRAW"


def side_from_winding(winding: List[Vec3], normal: Vec3, material: str,
                      texture_scale: float = 0.25) -> Optional[Side]:
    """A side with world-aligned texture axes, or None for a degenerate face."""
    points = pick_plane_points(winding, normal)
    if points is None:
        return None
    u, v = default_axes(normal, texture_scale)
    return Side(points, normal, material, u, v, winding=winding)


def prism_brush(polygon: Sequence[Vec3], normal: Vec3, below: float, above: float,
                top_side: Optional[Side] = None, material: str = NODRAW,
                top_material: Optional[str] = None, texture_scale: float = 0.25,
                visgroup: Optional[str] = None) -> Optional[Brush]:
    """Extrude a convex polygon into a brush.

    The prism reaches ``below`` units along ``-normal`` and ``above`` units
    along ``normal``. ``top_side`` replaces the generated ``+normal`` face.
    """
    top = [add(p, scale(normal, above)) for p in polygon]
    bottom = [add(p, scale(normal, -below)) for p in polygon]
    sides = [
        top_side or side_from_winding(top, normal, top_material or material, texture_scale),
        side_from_winding(bottom[::-1], neg(normal), material, texture_scale),
    ]
    center = scale(add(winding_center(top), winding_center(bottom)), 0.5)
    count = len(polygon)
    for i in range(count):
        a, b = polygon[i], polygon[(i + 1) % count]
        side_normal = normalize(cross(sub(b, a), normal))
        if dot(side_normal, sub(a, center)) < 0:
            side_normal = neg(side_normal)
        quad = [top[i], top[(i + 1) % count], bottom[(i + 1) % count], bottom[i]]
        sides.append(side_from_winding(quad, side_normal, material, texture_scale))
    if any(s is None for s in sides):
        return None
    return Brush(sides, visgroup)


def box_brush(center: Vec3, half_size: float, material: str,
              texture_scale: float) -> Brush:
    """An axis-aligned cube, e.g. an ORIGIN brush."""
    mins = sub(center, (half_size, half_size, half_size))
    maxs = add(center, (half_size, half_size, half_size))
    sides = []
    for face in box_polyhedron(mins, maxs):
        points = pick_plane_points(face.winding, face.normal)
        u, v = default_axes(face.normal, texture_scale)
        sides.append(Side(points, face.normal, material, u, v))
    return Brush(sides)


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

PlaneKey = Tuple[float, float, float, float]


def _plane_key(normal: Vec3, dist: float) -> PlaneKey:
    # "+ 0.0" turns -0.0 into 0.0 so opposite zero components hash the same.
    return (round(normal[0], 3) + 0.0, round(normal[1], 3) + 0.0,
            round(normal[2], 3) + 0.0, round(dist, 1) + 0.0)


def _side_dist(side: Side) -> float:
    return dot(side.normal, side.winding[0])


def _same_texture(a: Side, b: Side) -> bool:
    if a.material.lower() != b.material.lower():
        return False
    for x, y in ((a.u, b.u), (a.v, b.v)):
        if dot(x.axis, y.axis) < 0.999 or abs(x.scale - y.scale) > 1e-3:
            return False
    return True


class _BrushMerger:
    CONVEX_EPSILON = 0.05

    def __init__(self, nodraw: str):
        self.nodraw = nodraw.lower()

    def try_merge(self, a: Brush, b: Brush, shared_a: Side, shared_b: Side) -> Optional[Brush]:
        """Union of two brushes touching on ``shared_a``/``shared_b``, if convex."""
        if a.contents != b.contents:
            return None
        if abs(winding_area(shared_a.winding) - winding_area(shared_b.winding)) > 0.5:
            return None
        if length(sub(winding_center(shared_a.winding), winding_center(shared_b.winding))) > 0.05:
            return None
        if not (self._contains(a, shared_a, b) and self._contains(b, shared_b, a)):
            return None

        chosen: Dict[PlaneKey, Side] = {}
        order: List[PlaneKey] = []
        for side in a.sides + b.sides:
            if side is shared_a or side is shared_b:
                continue
            key = _plane_key(side.normal, _side_dist(side))
            if key not in chosen:
                chosen[key] = side
                order.append(key)
                continue
            existing = chosen[key]
            existing_hidden = existing.material.lower() == self.nodraw
            side_hidden = side.material.lower() == self.nodraw
            if existing_hidden and not side_hidden:
                chosen[key] = side
            elif not existing_hidden and not side_hidden and not _same_texture(existing, side):
                return None  # merging would lose a texture or its alignment

        planes = [(chosen[k].normal, _side_dist(chosen[k])) for k in order]
        sides = []
        for index, winding in brush_windings(planes):
            source = chosen[order[index]]
            points = pick_plane_points(winding, source.normal)
            if points is not None:
                sides.append(Side(points, source.normal, source.material, source.u,
                                  source.v, source.lightmap_scale, winding=winding))
        if len(sides) < 4:
            return None
        return Brush(sides, a.visgroup, a.contents)

    def _contains(self, brush: Brush, shared: Side, other: Brush) -> bool:
        """True if every vertex of ``other`` is behind every plane of ``brush``
        except the shared one (i.e. the union stays convex)."""
        vertices = [p for s in other.sides for p in s.winding]
        for side in brush.sides:
            if side is shared:
                continue
            dist = _side_dist(side)
            if any(dot(side.normal, p) - dist > self.CONVEX_EPSILON for p in vertices):
                return False
        return True


def merge_brushes(brushes: List[Brush], nodraw: str) -> List[Brush]:
    """Greedily join brushes that share a whole face and whose union is convex.

    Brushes only merge when they have the same ``contents`` and no textured
    side would lose its texture. Undoes most of the splitting a BSP tree does.
    """
    mergeable = [b for b in brushes if all(s.winding for s in b.sides)]
    others = [b for b in brushes if not all(s.winding for s in b.sides)]
    merger = _BrushMerger(nodraw)

    changed = True
    while changed:
        changed = False
        index: Dict[PlaneKey, List[Tuple[int, Side]]] = defaultdict(list)
        for bi, brush in enumerate(mergeable):
            for side in brush.sides:
                index[_plane_key(side.normal, _side_dist(side))].append((bi, side))

        consumed = set()
        merged_brushes = []
        for bi, brush in enumerate(mergeable):
            if bi in consumed:
                continue
            for side in brush.sides:
                opposite = _plane_key(neg(side.normal), -_side_dist(side))
                merged = None
                for bj, other_side in index.get(opposite, ()):
                    if bj == bi or bj in consumed:
                        continue
                    merged = merger.try_merge(brush, mergeable[bj], side, other_side)
                    if merged:
                        consumed.update((bi, bj))
                        merged_brushes.append(merged)
                        break
                if merged:
                    changed = True
                    break
        mergeable = [b for i, b in enumerate(mergeable) if i not in consumed] + merged_brushes
    return mergeable + others
