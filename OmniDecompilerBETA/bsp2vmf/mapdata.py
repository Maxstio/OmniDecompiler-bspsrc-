"""In-memory representation of a decompiled map (entities, brushes, sides)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, NamedTuple, Optional, Sequence, Tuple

from .vecmath import Vec3, cross, dot, length, scale, sub

# ---------------------------------------------------------------------------
# Number formatting
# ---------------------------------------------------------------------------


def snap(x: float) -> float:
    """Snap values within 0.001 of an integer to that integer."""
    r = round(x)
    return float(r) if abs(x - r) < 0.001 else x


def format_number(x: float) -> str:
    """Compact decimal text: integers without a fraction, at most 6 decimals."""
    x = snap(x)
    if x == int(x):
        return str(int(x))
    text = f"{x:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def format_vector(v: Iterable[float]) -> str:
    return " ".join(format_number(c) for c in v)


# ---------------------------------------------------------------------------
# Texture axes
# ---------------------------------------------------------------------------


class TextureAxis(NamedTuple):
    """Hammer texture axis: texel coordinate = dot(p, axis) / scale + offset."""

    axis: Vec3
    offset: float
    scale: float


# Quake's texture-axis table: (face normal, u axis, v axis).
_BASE_AXES = (
    ((0, 0, 1), (1, 0, 0), (0, -1, 0)),
    ((0, 0, -1), (1, 0, 0), (0, -1, 0)),
    ((1, 0, 0), (0, 1, 0), (0, 0, -1)),
    ((-1, 0, 0), (0, 1, 0), (0, 0, -1)),
    ((0, 1, 0), (1, 0, 0), (0, 0, -1)),
    ((0, -1, 0), (1, 0, 0), (0, 0, -1)),
)


def default_axes(normal: Vec3, texture_scale: float) -> Tuple[TextureAxis, TextureAxis]:
    """World-aligned texture axes for a face with the given normal."""
    _, u, v = max(_BASE_AXES, key=lambda b: dot(b[0], normal))
    return TextureAxis(u, 0.0, texture_scale), TextureAxis(v, 0.0, texture_scale)


def axes_from_texture_vecs(s: Sequence[float], t: Sequence[float], normal: Vec3,
                           default_scale: float, shift: Optional[Vec3] = None
                           ) -> Tuple[TextureAxis, TextureAxis]:
    """Convert BSP texture vectors (texels per unit + offset) to Hammer axes.

    ``shift`` is a translation that will be applied to the geometry; the
    offsets are adjusted so the texture stays put.
    """
    axes = []
    for vec in (s, t):
        axis = (vec[0], vec[1], vec[2])
        texels_per_unit = length(axis)
        if texels_per_unit < 1e-8:
            return default_axes(normal, default_scale)
        offset = vec[3]
        if shift is not None:
            offset -= dot(shift, axis)
        axes.append(TextureAxis(scale(axis, 1.0 / texels_per_unit), offset,
                                1.0 / texels_per_unit))
    return axes[0], axes[1]


def pick_plane_points(winding: Sequence[Vec3], normal: Vec3) -> Optional[List[Vec3]]:
    """Three well separated points of a face, in Hammer's winding order.

    Hammer derives a side's plane from three points as
    ``cross(p0 - p1, p2 - p1)``, which must point out of the brush.
    """
    p0 = winding[0]
    p1 = max(winding, key=lambda p: dot(sub(p, p0), sub(p, p0)))
    edge = sub(p1, p0)
    p2 = max(winding, key=lambda p: dot(cross(edge, sub(p, p0)), cross(edge, sub(p, p0))))
    if length(cross(edge, sub(p2, p0))) < 1e-4:
        return None
    points = [(snap(p[0]), snap(p[1]), snap(p[2])) for p in (p0, p1, p2)]
    if dot(cross(sub(points[0], points[1]), sub(points[2], points[1])), normal) < 0:
        points[0], points[2] = points[2], points[0]
    return points


# GoldSrc / Quake special textures and their Source tool materials.
_SOURCE_TOOL_MATERIALS = {
    "null": "TOOLS/TOOLSNODRAW",
    "skip": "TOOLS/TOOLSSKIP",
    "hint": "TOOLS/TOOLSHINT",
    "clip": "TOOLS/TOOLSCLIP",
    "origin": "TOOLS/TOOLSORIGIN",
    "aaatrigger": "TOOLS/TOOLSTRIGGER",
    "trigger": "TOOLS/TOOLSTRIGGER",
    "sky": "TOOLS/TOOLSSKYBOX",
}


def source_material(name: str) -> str:
    """Map a GoldSrc/Quake tool texture to its Source equivalent; others unchanged."""
    return _SOURCE_TOOL_MATERIALS.get(name.lower(), name)


# ---------------------------------------------------------------------------
# Map objects
# ---------------------------------------------------------------------------


@dataclass
class Displacement:
    """A displacement surface attached to a brush side (VMF ``dispinfo``)."""

    power: int
    start_position: Vec3
    normals: List[List[Vec3]]
    distances: List[List[float]]
    alphas: List[List[float]]
    offset_normal: Vec3
    allowed_verts: List[int]

    @property
    def size(self) -> int:
        return (1 << self.power) + 1


@dataclass(eq=False)
class Side:
    points: List[Vec3]  # three plane points in Hammer order
    normal: Vec3  # outward unit normal
    material: str
    u: TextureAxis
    v: TextureAxis
    lightmap_scale: int = 16
    displacement: Optional[Displacement] = None
    winding: Optional[List[Vec3]] = None  # full face polygon in world space
    id: Optional[int] = None  # assigned by the VMF writer


@dataclass(eq=False)
class Brush:
    sides: List[Side]
    visgroup: Optional[str] = None
    contents: Optional[int] = None  # BSP contents, only used while merging


@dataclass(eq=False)
class Entity:
    keys: List[Tuple[str, str]]  # keyvalues in file order; keys may repeat
    connections: List[Tuple[str, str]] = field(default_factory=list)
    brushes: List[Brush] = field(default_factory=list)
    visgroup: Optional[str] = None
    side_refs: Optional[List[Side]] = None  # written as the "sides" key

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        wanted = key.lower()
        for k, v in self.keys:
            if k.lower() == wanted:
                return v
        return default

    def set(self, key: str, value: str) -> None:
        wanted = key.lower()
        for i, (k, _) in enumerate(self.keys):
            if k.lower() == wanted:
                self.keys[i] = (k, value)
                return
        self.keys.append((key, value))

    def remove(self, key: str) -> None:
        wanted = key.lower()
        self.keys = [(k, v) for k, v in self.keys if k.lower() != wanted]

    @property
    def classname(self) -> str:
        return self.get("classname", "") or ""

    @property
    def origin(self) -> Optional[Vec3]:
        """The entity origin, or None when absent or (0, 0, 0)."""
        text = self.get("origin")
        if not text:
            return None
        try:
            values = [float(x) for x in text.split()[:3]]
        except ValueError:
            return None
        if len(values) != 3 or values == [0.0, 0.0, 0.0]:
            return None
        return (values[0], values[1], values[2])

    @property
    def model_index(self) -> Optional[int]:
        """Index of the inline brush model (``"model" "*N"``), if any."""
        model = self.get("model", "") or ""
        if not model.startswith("*"):
            return None
        try:
            return int(model[1:])
        except ValueError:
            return None


def find_worldspawn(entities: List[Entity]) -> Entity:
    """Return the worldspawn entity, creating it at the front if missing."""
    for entity in entities:
        if entity.classname == "worldspawn":
            return entity
    world = Entity([("classname", "worldspawn")])
    entities.insert(0, world)
    return world


# ---------------------------------------------------------------------------
# Entity lump
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r'"([^"]*)"|(\{)|(\})')


def parse_entity_lump(text: str) -> List[Entity]:
    entities: List[Entity] = []
    current: Optional[List[Tuple[str, str]]] = None
    pending_key: Optional[str] = None
    for match in _TOKEN.finditer(text):
        if match.group(2):
            current, pending_key = [], None
        elif match.group(3):
            if current is not None:
                entities.append(Entity(current))
            current = None
        elif current is not None:
            if pending_key is None:
                pending_key = match.group(1)
                continue
            pair = (pending_key, match.group(1))
            # Some compilers write "classname" twice.
            if not (pending_key.lower() == "classname" and pair in current):
                current.append(pair)
            pending_key = None
    return entities


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _is_output(key: str, value: str) -> bool:
    """Source I/O outputs: "target,input,param,delay,times" (or 0x1B separated)."""
    if value.count("\x1b") == 4:
        return True
    if value.count(",") == 4 and key[:1].isalpha():
        parts = value.split(",")
        return _is_number(parts[3]) and re.match(r"^-?\d+$", parts[4].strip()) is not None
    return False


def split_outputs(entity: Entity) -> None:
    """Move I/O outputs from the keyvalues into ``entity.connections``."""
    keep = []
    for key, value in entity.keys:
        if _is_output(key, value):
            entity.connections.append((key, value))
        else:
            keep.append((key, value))
    entity.keys = keep
