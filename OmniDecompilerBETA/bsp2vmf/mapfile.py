"""Reading Quake-family ``.map`` source files.

Both face formats are supported:

* Valve 220 (GoldSrc Hammer, J.A.C.K., TrenchBroom)::

    ( x y z ) ( x y z ) ( x y z ) TEXTURE [ ux uy uz uoff ] [ vx vy vz voff ] rot uscale vscale

* Quake standard (Quake, and Quake 2/3 with trailing content flags)::

    ( x y z ) ( x y z ) ( x y z ) TEXTURE xoff yoff rot xscale yscale

The three plane points follow the same rule as VMF (``cross(p0 - p1, p2 - p1)``
points out of the brush), so they are kept as they are. Patches and Doom 3
style ``brushDef3`` blocks are skipped.
"""

from __future__ import annotations

import logging
import math
import re
from typing import List, Optional, Tuple

from .errors import MapParseError
from .mapdata import Brush, Entity, Side, TextureAxis, default_axes
from .vecmath import Vec3, cross, length, normalize, sub

log = logging.getLogger(__name__)

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_POINT = rf"\(\s*({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*\)"
_FACE = re.compile(rf"^\s*{_POINT}\s*{_POINT}\s*{_POINT}\s*(\S+)\s*(.*)$")
_VALVE_AXES = re.compile(
    rf"^\[\s*({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*\]\s*"
    rf"\[\s*({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*\]\s*"
    rf"({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})")
_KEYVALUE = re.compile(r'^\s*"([^"]*)"\s+"([^"]*)"\s*$')


def quake_texture_axes(normal: Vec3, x_offset: float, y_offset: float, rotation: float,
                       x_scale: float, y_scale: float) -> Tuple[TextureAxis, TextureAxis]:
    """Texture axes of a Quake-standard face, as qbsp computes them."""
    (u, _, _), (v, _, _) = default_axes(normal, 1.0)
    angle = math.radians(rotation)
    sin_a, cos_a = math.sin(angle), math.cos(angle)
    # qbsp rotates both axes within the two coordinates the base axes use.
    s_index = next(i for i in range(3) if u[i])
    t_index = next(i for i in range(3) if v[i])
    axes = []
    for axis in (u, v):
        rotated = list(axis)
        s, t = axis[s_index], axis[t_index]
        rotated[s_index] = cos_a * s - sin_a * t
        rotated[t_index] = sin_a * s + cos_a * t
        axes.append((rotated[0] + 0.0, rotated[1] + 0.0, rotated[2] + 0.0))
    return (TextureAxis(axes[0], x_offset, x_scale or 1.0),
            TextureAxis(axes[1], y_offset, y_scale or 1.0))


def parse_face(line: str) -> Optional[Side]:
    """One brush face; None for a degenerate plane."""
    match = _FACE.match(line)
    if not match:
        raise MapParseError(f"cannot read face: {line.strip()[:80]}")
    values = [float(x) for x in match.groups()[:9]]
    points = [tuple(values[i:i + 3]) for i in (0, 3, 6)]
    material, rest = match.group(10), match.group(11)
    normal = cross(sub(points[0], points[1]), sub(points[2], points[1]))
    if length(normal) < 1e-6:
        return None
    normal = normalize(normal)

    valve = _VALVE_AXES.match(rest)
    if valve:
        n = [float(x) for x in valve.groups()]
        u = TextureAxis((n[0], n[1], n[2]), n[3], n[9] or 1.0)
        v = TextureAxis((n[4], n[5], n[6]), n[7], n[10] or 1.0)
    else:
        numbers = re.findall(_NUMBER, rest)
        if len(numbers) < 5:
            raise MapParseError(f"missing texture alignment: {line.strip()[:80]}")
        x_offset, y_offset, rotation, x_scale, y_scale = (float(x) for x in numbers[:5])
        u, v = quake_texture_axes(normal, x_offset, y_offset, rotation, x_scale, y_scale)
    return Side(points, normal, material, u, v)


UNSUPPORTED_BLOCKS = ("patchDef2", "patchDef3", "brushDef", "brushDef3")


def _strip_comment(line: str) -> str:
    # Keyvalues may legitimately contain "//" (URLs, paths), so leave them alone.
    stripped = line.strip()
    if stripped.startswith('"'):
        return stripped
    return stripped.split("//", 1)[0].strip()


def read_map(text: str) -> List[Entity]:
    """Parse a ``.map`` file into entities with brushes."""
    entities: List[Entity] = []
    entity: Optional[Entity] = None
    brush: Optional[List[Side]] = None
    skipping = False  # inside a patch / brushDef block
    skip_depth = 0
    skipped_blocks = 0

    for number, raw_line in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw_line)
        if not line:
            continue
        if skipping:
            skip_depth += line.count("{") - line.count("}")
            skipping = skip_depth > 0
            continue
        if line in UNSUPPORTED_BLOCKS:
            skipping, skip_depth = True, 0
            skipped_blocks += 1
            continue
        if line == "{":
            if entity is None:
                entity = Entity([])
            elif brush is None:
                brush = []
            else:
                raise MapParseError(f"line {number}: unexpected '{{'")
        elif line == "}":
            if brush is not None:
                if len(brush) >= 4:
                    entity.brushes.append(Brush(brush))
                brush = None
            elif entity is not None:
                entities.append(entity)
                entity = None
            else:
                raise MapParseError(f"line {number}: unexpected '}}'")
        elif brush is not None:
            try:
                side = parse_face(line)
            except MapParseError as error:
                raise MapParseError(f"line {number}: {error}") from None
            if side is not None:
                brush.append(side)
        elif entity is not None:
            match = _KEYVALUE.match(line)
            if not match:
                raise MapParseError(f"line {number}: expected a keyvalue, got {line[:80]!r}")
            entity.keys.append(match.groups())
        else:
            raise MapParseError(f"line {number}: unexpected {line[:80]!r}")

    if entity is not None or brush is not None:
        raise MapParseError("unexpected end of file (missing '}')")
    if skipped_blocks:
        log.warning("skipped %d patch / brushDef blocks (not supported)", skipped_blocks)
    return entities
