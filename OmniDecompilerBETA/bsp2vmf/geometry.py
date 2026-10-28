"""Convex geometry: plane windings and convex polyhedra.

Planes are ``(normal, dist)`` pairs; a point ``p`` is behind (inside) a plane
when ``dot(normal, p) <= dist``. Windings are lists of points in order.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

from .vecmath import Vec3, add, cross, dot, length, neg, normalize, scale, sub

Plane = Tuple[Vec3, float]
Winding = List[Vec3]

#: Half size of the initial quad created for a plane (covers any map).
WORLD_EXTENT = 131072.0
#: Distance within which a point counts as lying on a plane.
ON_EPSILON = 0.01
#: Windings smaller than this are treated as degenerate.
MIN_AREA = 0.01


def base_winding(normal: Vec3, dist: float) -> Winding:
    """Return a huge quad lying on the plane ``dot(normal, p) == dist``."""
    major = max(range(3), key=lambda i: abs(normal[i]))
    up: Vec3 = (1.0, 0.0, 0.0) if major == 2 else (0.0, 0.0, 1.0)
    up = normalize(sub(up, scale(normal, dot(up, normal))))
    right = cross(up, normal)
    origin = scale(normal, dist)
    up = scale(up, WORLD_EXTENT)
    right = scale(right, WORLD_EXTENT)
    return [
        add(sub(origin, right), up),
        add(add(origin, right), up),
        sub(add(origin, right), up),
        sub(sub(origin, right), up),
    ]


def clip_winding(winding: Winding, normal: Vec3, dist: float,
                 epsilon: float = ON_EPSILON) -> Optional[Winding]:
    """Keep the part of ``winding`` behind the plane; None if nothing is left."""
    dists = [dot(p, normal) - dist for p in winding]
    has_front = any(d > epsilon for d in dists)
    has_back = any(d < -epsilon for d in dists)
    if not has_front:
        return winding
    if not has_back:
        return None

    out: Winding = []
    count = len(winding)
    for i in range(count):
        p, dp = winding[i], dists[i]
        q, dq = winding[(i + 1) % count], dists[(i + 1) % count]
        if dp <= epsilon:
            out.append(p)
        if (dp > epsilon and dq < -epsilon) or (dp < -epsilon and dq > epsilon):
            t = dp / (dp - dq)
            mid = [p[k] + t * (q[k] - p[k]) for k in range(3)]
            # Keep coordinates exact on axial planes.
            for k in range(3):
                if normal[k] == 1.0:
                    mid[k] = dist
                elif normal[k] == -1.0:
                    mid[k] = -dist
            out.append((mid[0], mid[1], mid[2]))
    return out if len(out) >= 3 else None


def winding_area(winding: Sequence[Vec3]) -> float:
    total: Vec3 = (0.0, 0.0, 0.0)
    for i in range(1, len(winding) - 1):
        total = add(total, cross(sub(winding[i], winding[0]), sub(winding[i + 1], winding[0])))
    return length(total) * 0.5


def winding_center(winding: Sequence[Vec3]) -> Vec3:
    total: Vec3 = (0.0, 0.0, 0.0)
    for p in winding:
        total = add(total, p)
    return scale(total, 1.0 / len(winding))


def point_in_winding(point: Vec3, winding: Sequence[Vec3], normal: Vec3,
                     epsilon: float = 0.1) -> bool:
    """True if ``point`` (assumed on the winding's plane) lies inside it."""
    sign = 0
    count = len(winding)
    for i in range(count):
        a, b = winding[i], winding[(i + 1) % count]
        c = dot(cross(sub(b, a), sub(point, a)), normal)
        if abs(c) <= epsilon:
            continue
        s = 1 if c > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    return True


def brush_windings(planes: Sequence[Plane]) -> List[Tuple[int, Winding]]:
    """Face polygons of the convex brush bounded by ``planes``.

    Planes must have outward normals. Returns ``(plane_index, winding)`` for
    every plane that contributes a face; duplicate planes keep the first one.
    """
    faces = []
    for i, (n, d) in enumerate(planes):
        w: Optional[Winding] = base_winding(n, d)
        for j, (n2, d2) in enumerate(planes):
            if i == j:
                continue
            if dot(n, n2) > 0.99999 and abs(d - d2) < 0.001:
                if j < i:
                    w = None
                    break
                continue
            w = clip_winding(w, n2, d2)
            if w is None:
                break
        if w is not None and winding_area(w) > MIN_AREA:
            faces.append((i, w))
    return faces


class PolyFace:
    """One face of a convex polyhedron.

    ``key`` identifies where the face's plane came from (e.g. a BSP node plane
    and side) so faces can later be matched with the BSP's rendered faces.
    """

    __slots__ = ("normal", "dist", "key", "winding")

    def __init__(self, normal: Vec3, dist: float, key: Any, winding: Winding):
        self.normal = normal
        self.dist = dist
        self.key = key
        self.winding = winding


Polyhedron = List[PolyFace]


def box_polyhedron(mins: Vec3, maxs: Vec3) -> Polyhedron:
    faces = []
    for axis in range(3):
        for sign, dist in ((1.0, maxs[axis]), (-1.0, -mins[axis])):
            n = [0.0, 0.0, 0.0]
            n[axis] = sign
            normal = (n[0], n[1], n[2])
            faces.append(PolyFace(normal, dist, None, base_winding(normal, dist)))
    for face in faces:
        for other in faces:
            if other is not face:
                face.winding = clip_winding(face.winding, other.normal, other.dist)
    return faces


def split_polyhedron(poly: Polyhedron, normal: Vec3, dist: float,
                     key_back: Any, key_front: Any
                     ) -> Tuple[Optional[Polyhedron], Optional[Polyhedron]]:
    """Split a convex polyhedron by a plane into ``(front, back)`` parts.

    The new cap face gets ``key_back`` on the back part and ``key_front`` on the
    front part. Either part is None when empty.
    """
    has_front = has_back = False
    for face in poly:
        for p in face.winding:
            d = dot(p, normal) - dist
            if d > ON_EPSILON:
                has_front = True
            elif d < -ON_EPSILON:
                has_back = True
        if has_front and has_back:
            break
    if not has_front:
        return None, poly
    if not has_back:
        return poly, None

    flipped = neg(normal)
    back: Polyhedron = []
    front: Polyhedron = []
    for face in poly:
        wb = clip_winding(face.winding, normal, dist)
        if wb is not None:
            back.append(PolyFace(face.normal, face.dist, face.key, wb))
        wf = clip_winding(face.winding, flipped, -dist)
        if wf is not None:
            front.append(PolyFace(face.normal, face.dist, face.key, wf))

    cap: Optional[Winding] = base_winding(normal, dist)
    for face in poly:
        cap = clip_winding(cap, face.normal, face.dist)
        if cap is None:
            break
    if cap is not None and winding_area(cap) > MIN_AREA:
        back.append(PolyFace(normal, dist, key_back, cap))
        front.append(PolyFace(flipped, -dist, key_front, cap[::-1]))

    return (front if len(front) >= 4 else None,
            back if len(back) >= 4 else None)
