"""Source map features the compiler moves out of the entity lump.

Each ``restore_*`` / ``read_*`` function takes a :class:`~bsp2vmf.source_bsp.SourceBsp`
and rebuilds one kind of object: displacements, static props, cubemaps,
overlays, occluders, lights stripped by vbsp, area portal brushes, and the
files packed into the BSP.
"""

from __future__ import annotations

import io
import math
import os
import struct
import zipfile
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .binary import c_string, lzma_decompress, unpack_array, unpack_ints
from .brushes import prism_brush, side_from_winding
from .geometry import brush_windings, point_in_winding, winding_center
from .mapdata import Brush, Displacement, Entity, Side, format_number, format_vector
from .source_bsp import (FACE_SIZE, LUMP_AREAPORTALS, LUMP_CLIPPORTALVERTS, LUMP_CUBEMAPS,
                         LUMP_DISP_VERTS, LUMP_DISPINFO, LUMP_EDGES, LUMP_FACES, LUMP_GAME_LUMP,
                         LUMP_OCCLUSION, LUMP_OVERLAY_FADES, LUMP_OVERLAYS, LUMP_PAKFILE,
                         LUMP_SURFEDGES, LUMP_VERTEXES, LUMP_WORLDLIGHTS, LUMP_WORLDLIGHTS_HDR,
                         SourceBsp)
from .vecmath import Vec3, add, cross, dot, length, neg, normalize, sub

DISPINFO_SIZE = 176
OVERLAY_SIZE = 352


class FaceGeometry:
    """Polygons of the BSP's rendered faces."""

    def __init__(self, bsp: SourceBsp):
        self.faces = bsp.lump(LUMP_FACES)
        if len(self.faces) % FACE_SIZE:
            raise ValueError("unknown face structure size")
        self.vertices = unpack_array("<3f", bsp.lump(LUMP_VERTEXES))
        self.edges = unpack_array("<2H", bsp.lump(LUMP_EDGES))
        self.surfedges = unpack_ints(bsp.lump(LUMP_SURFEDGES))

    def __len__(self) -> int:
        return len(self.faces) // FACE_SIZE

    def plane_index(self, face: int) -> int:
        return struct.unpack_from("<H", self.faces, face * FACE_SIZE)[0]

    def texinfo(self, face: int) -> int:
        return struct.unpack_from("<h", self.faces, face * FACE_SIZE + 10)[0]

    def winding(self, face: int) -> List[Vec3]:
        first_edge, edge_count = struct.unpack_from("<ih", self.faces, face * FACE_SIZE + 4)
        winding = []
        for edge in self.surfedges[first_edge:first_edge + edge_count]:
            vertex = self.edges[edge][0] if edge >= 0 else self.edges[-edge][1]
            winding.append(self.vertices[vertex])
        return winding


# ---------------------------------------------------------------------------
# Displacements
# ---------------------------------------------------------------------------


def restore_displacements(bsp: SourceBsp, world: Entity, thickness: float,
                          sides_by_face: Dict[int, Side]) -> int:
    """Rebuild displacements as thin brushes carrying a ``dispinfo``.

    vbsp removes displacement brushes, so a prism is built under each
    displacement face. ``sides_by_face`` receives face index -> top side so
    overlays can be attached to displacements.
    """
    dispinfo = bsp.lump(LUMP_DISPINFO)
    if not dispinfo:
        return 0
    if len(dispinfo) % DISPINFO_SIZE:
        raise ValueError("unknown dispinfo structure size")
    faces = FaceGeometry(bsp)
    disp_verts = unpack_array("<5f", bsp.lump(LUMP_DISP_VERTS))

    count = 0
    for offset in range(0, len(dispinfo), DISPINFO_SIZE):
        start_position = struct.unpack_from("<3f", dispinfo, offset)
        first_vert, _, power = struct.unpack_from("<3i", dispinfo, offset + 12)
        face = struct.unpack_from("<H", dispinfo, offset + 36)[0]
        allowed = struct.unpack_from("<10i", dispinfo, offset + 136)
        if power not in (2, 3, 4) or face >= len(faces):
            continue
        plane_index = faces.plane_index(face)
        winding = faces.winding(face)
        if len(winding) != 4 or plane_index >= len(bsp.planes):
            continue
        # Source face planes already point the right way ("side" is relative
        # to the BSP node, not a flip).
        normal = bsp.planes[plane_index][0]
        top = bsp.make_side(winding, normal, faces.texinfo(face), None)
        if top is None:
            continue
        size = (1 << power) + 1
        if first_vert + size * size > len(disp_verts):
            continue
        rows = [disp_verts[first_vert + r * size:first_vert + (r + 1) * size] for r in range(size)]
        top.displacement = Displacement(
            power=power,
            start_position=start_position,
            normals=[[(v[0], v[1], v[2]) for v in row] for row in rows],
            distances=[[v[3] for v in row] for row in rows],
            alphas=[[v[4] for v in row] for row in rows],
            offset_normal=normal,
            allowed_verts=list(allowed),
        )
        brush = prism_brush(winding, normal, thickness, 0.0, top_side=top,
                            visgroup="Displacements")
        if brush is None:
            continue
        world.brushes.append(brush)
        sides_by_face[face] = top
        count += 1
    return count


# ---------------------------------------------------------------------------
# Static props and cubemaps
# ---------------------------------------------------------------------------

STATIC_PROP_FLAGS = (
    (0x10, "disableshadows"),
    (0x20, "screenspacefade"),
    (0x40, "disablevertexlighting"),
    (0x80, "disableselfshadowing"),
)


def read_static_props(bsp: SourceBsp) -> List[Entity]:
    """prop_static entities from the "sprp" game lump."""
    directory = bsp.lump(LUMP_GAME_LUMP)
    if len(directory) < 4:
        return []
    count = struct.unpack_from("<i", directory, 0)[0]
    entries = [struct.unpack_from("<4sHHii", directory, 4 + i * 16) for i in range(count)]
    for index, (lump_id, flags, version, offset, size) in enumerate(entries):
        if lump_id != b"prps":  # "sprp" stored little-endian
            continue
        blob = bsp.data[offset:offset + size]
        if blob[:4] == b"LZMA" or flags & 1:
            # A compressed game lump extends to the start of the next one.
            end = entries[index + 1][3] if index + 1 < len(entries) else offset + size
            blob = lzma_decompress(bsp.data[offset:max(end, offset + size)])
        return _parse_static_props(blob, version)
    return []


def _parse_static_props(data: bytes, version: int) -> List[Entity]:
    position = 0
    name_count = struct.unpack_from("<i", data, position)[0]
    position += 4
    names = [c_string(data[position + i * 128:position + (i + 1) * 128]) for i in range(name_count)]
    position += name_count * 128
    leaf_count = struct.unpack_from("<i", data, position)[0]
    position += 4 + leaf_count * 2
    prop_count = struct.unpack_from("<i", data, position)[0]
    position += 4
    if prop_count <= 0:
        return []

    # The record layout changes between versions but always starts the same,
    # so the stride is derived from the lump size.
    stride = (len(data) - position) // prop_count
    props = []
    for i in range(prop_count):
        offset = position + i * stride
        if offset + 44 > len(data):
            break
        x, y, z, pitch, yaw, roll = struct.unpack_from("<6f", data, offset)
        model, _, _, solid, flags = struct.unpack_from("<3H2B", data, offset + 24)
        skin, fade_min, fade_max = struct.unpack_from("<iff", data, offset + 32)
        keys = [
            ("classname", "prop_static"),
            ("origin", format_vector((x, y, z))),
            ("angles", format_vector((pitch, yaw, roll))),
            ("model", names[model] if model < len(names) else "models/error.mdl"),
            ("skin", str(skin)),
            ("solid", str(solid)),
            ("fademindist", format_number(fade_min)),
            ("fademaxdist", format_number(fade_max)),
        ]
        if version >= 5 and stride >= 60:
            keys.append(("fadescale", format_number(struct.unpack_from("<f", data, offset + 56)[0])))
        keys.extend((key, "1") for bit, key in STATIC_PROP_FLAGS if flags & bit)
        props.append(Entity(keys, visgroup="Static props"))
    return props


def read_cubemaps(bsp: SourceBsp) -> List[Entity]:
    return [
        Entity([("classname", "env_cubemap"),
                ("origin", f"{x} {y} {z}"),
                ("cubemapsize", str(size))], visgroup="Cubemaps")
        for x, y, z, size in unpack_array("<4i", bsp.lump(LUMP_CUBEMAPS))
    ]


# ---------------------------------------------------------------------------
# Area portals
# ---------------------------------------------------------------------------


def _brush_bounds(brush: Brush) -> Tuple[List[float], List[float]]:
    points = [p for s in brush.sides for p in (s.winding or s.points)]
    return ([min(p[k] for p in points) for k in range(3)],
            [max(p[k] for p in points) for k in range(3)])


def restore_areaportals(bsp: SourceBsp, entities: List[Entity],
                        portal_brushes: List[Brush]) -> None:
    """Give func_areaportal entities their brushes back.

    Each entity only keeps a "portalnumber"; the matching portal polygon in
    the areaportals lump locates its CONTENTS_AREAPORTAL brush, or is used to
    build a new brush when vbsp dropped it.
    """
    vertices = unpack_array("<3f", bsp.lump(LUMP_CLIPPORTALVERTS))
    portals: Dict[int, Tuple[List[Vec3], Vec3]] = {}
    for key, _, first, count, plane_index in unpack_array("<4Hi", bsp.lump(LUMP_AREAPORTALS)):
        if key in portals or count < 3 or first + count > len(vertices) or plane_index >= len(bsp.planes):
            continue
        portals[key] = ([tuple(v) for v in vertices[first:first + count]], bsp.planes[plane_index][0])

    unassigned = list(portal_brushes)
    portal_entities = [e for e in entities if e.classname.startswith("func_areaportal") and not e.brushes]
    for entity in portal_entities:
        try:
            key = int(entity.get("portalnumber", "-1"))
        except ValueError:
            key = -1
        portal = portals.get(key)
        if portal is not None:
            polygon, normal = portal
            center = winding_center(polygon)
            match = next((b for b in unassigned
                          if all(lo - 1 <= c <= hi + 1
                                 for c, lo, hi in zip(center, *_brush_bounds(b)))), None)
            if match is not None:
                entity.brushes.append(match)
                unassigned.remove(match)
            else:
                brush = prism_brush(polygon, normal, 4.0, 4.0, material="TOOLS/TOOLSAREAPORTAL")
                if brush:
                    entity.brushes.append(brush)
        entity.remove("portalnumber")
        entity.visgroup = "Areaportals"

    for brush in unassigned:
        target = next((e for e in portal_entities if not e.brushes), None)
        if target is None:
            target = Entity([("classname", "func_areaportal"), ("StartOpen", "1")],
                            visgroup="Areaportals")
            entities.append(target)
        target.brushes.append(brush)


# ---------------------------------------------------------------------------
# Overlays
# ---------------------------------------------------------------------------


class FaceSideLookup:
    """Find the rebuilt brush side a compiled BSP face came from."""

    def __init__(self, bsp: SourceBsp, entities: List[Entity],
                 displacement_sides: Dict[int, Side]):
        self.bsp = bsp
        self.faces = FaceGeometry(bsp)
        self.displacement_sides = displacement_sides
        # Faces of brush entities are stored relative to the entity origin.
        shifts = {e.model_index: e.origin for e in entities
                  if e.model_index and e.origin and e.model_index < len(bsp.models)}
        self.face_ranges = [(m[10], m[10] + m[11], shifts.get(i)) for i, m in enumerate(bsp.models)]
        self.sides_by_plane: Dict[Tuple, List[Side]] = defaultdict(list)
        for entity in entities:
            for brush in entity.brushes:
                for side in brush.sides:
                    if side.winding:
                        key = self._key(side.normal, dot(side.normal, side.winding[0]))
                        self.sides_by_plane[key].append(side)

    @staticmethod
    def _key(normal: Vec3, dist: float) -> Tuple:
        return (round(normal[0], 2) + 0.0, round(normal[1], 2) + 0.0,
                round(normal[2], 2) + 0.0, int(math.floor(dist + 0.5)))

    def side_for_face(self, face: int) -> Optional[Side]:
        if face in self.displacement_sides:
            return self.displacement_sides[face]
        if face < 0 or face >= len(self.faces):
            return None
        plane_index = self.faces.plane_index(face)
        winding = self.faces.winding(face)
        if plane_index >= len(self.bsp.planes) or len(winding) < 3:
            return None
        center = winding_center(winding)
        normal, dist = self.bsp.planes[plane_index]
        shift = next((s for first, end, s in self.face_ranges if first <= face < end), None)
        if shift:
            center = add(center, shift)
            dist += dot(normal, shift)
        key = self._key(normal, dist)
        for rounding in (0, -1, 1):  # the plane distance may round either way
            for side in self.sides_by_plane.get(key[:3] + (key[3] + rounding,), ()):
                if (abs(dot(side.normal, center) - dot(side.normal, side.winding[0])) < 0.5
                        and point_in_winding(center, side.winding, side.normal, 0.5)):
                    return side
        return None


def restore_overlays(bsp: SourceBsp, entities: List[Entity],
                     displacement_sides: Dict[int, Side]) -> Tuple[int, int]:
    """info_overlay entities; returns (restored, without a matching face)."""
    data = bsp.lump(LUMP_OVERLAYS)
    if not data or len(data) % OVERLAY_SIZE:
        return 0, 0
    lookup = FaceSideLookup(bsp, entities, displacement_sides)
    fades = unpack_array("<2f", bsp.lump(LUMP_OVERLAY_FADES))

    unlinked = 0
    count = len(data) // OVERLAY_SIZE
    for i in range(count):
        offset = i * OVERLAY_SIZE
        _, texinfo, face_count_and_order = struct.unpack_from("<ihH", data, offset)
        face_count = min(face_count_and_order & 0x3FFF, 64)
        render_order = face_count_and_order >> 14
        faces = struct.unpack_from("<64i", data, offset + 8)[:face_count]
        start_u, end_u, start_v, end_v = struct.unpack_from("<4f", data, offset + 264)
        uv_points = [struct.unpack_from("<3f", data, offset + 280 + 12 * k) for k in range(4)]
        origin = struct.unpack_from("<3f", data, offset + 328)
        normal = struct.unpack_from("<3f", data, offset + 340)

        # vbsp stores BasisU in the unused z of the first three UV points and
        # a "flip BasisV" flag in the fourth.
        basis_u = normalize((uv_points[0][2], uv_points[1][2], uv_points[2][2]))
        basis_v = cross(normal, basis_u)
        if uv_points[3][2] == 1.0:
            basis_v = neg(basis_v)

        sides: List[Side] = []
        for face in faces:
            side = lookup.side_for_face(face)
            if side is not None and side not in sides:
                sides.append(side)
        if not sides:
            unlinked += 1

        keys = [
            ("classname", "info_overlay"),
            ("origin", format_vector(origin)),
            ("BasisOrigin", format_vector(origin)),
            ("BasisNormal", format_vector(normal)),
            ("BasisU", format_vector(basis_u)),
            ("BasisV", format_vector(basis_v)),
            ("StartU", format_number(start_u)),
            ("EndU", format_number(end_u)),
            ("StartV", format_number(start_v)),
            ("EndV", format_number(end_v)),
            ("material", bsp.material(texinfo)),
            ("RenderOrder", str(render_order)),
        ]
        keys.extend((f"uv{k}", f"{format_number(p[0])} {format_number(p[1])} 0")
                    for k, p in enumerate(uv_points))
        if i < len(fades):
            fade_min_sq, fade_max_sq = fades[i]
            if fade_max_sq > 0:
                keys.append(("fademindist", format_number(math.sqrt(max(fade_min_sq, 0.0)))))
                keys.append(("fademaxdist", format_number(math.sqrt(fade_max_sq))))
        overlay = Entity(keys, visgroup="Overlays")
        overlay.side_refs = sides
        entities.append(overlay)
    return count, unlinked


# ---------------------------------------------------------------------------
# Occluders
# ---------------------------------------------------------------------------


def restore_occluders(bsp: SourceBsp, entities: List[Entity]) -> int:
    """Give func_occluder entities their brushes back from the occlusion lump."""
    data = bsp.lump(LUMP_OCCLUSION)
    if len(data) < 4:
        return 0
    # doccluderdata_t: flags, firstpoly, polycount, mins, maxs [, area (v1+)]
    record_size = 40 if bsp.lump_version(LUMP_OCCLUSION) >= 1 else 36
    position = 0
    occluder_count = struct.unpack_from("<i", data, position)[0]
    position += 4
    occluders = []
    for _ in range(occluder_count):
        _, first_poly, poly_count = struct.unpack_from("<3i", data, position)
        occluders.append((first_poly, poly_count))
        position += record_size
    poly_total = struct.unpack_from("<i", data, position)[0]
    position += 4
    polys = [struct.unpack_from("<3i", data, position + 12 * i) for i in range(poly_total)]
    position += 12 * poly_total
    index_count = struct.unpack_from("<i", data, position)[0]
    position += 4
    vertex_indices = struct.unpack_from(f"<{index_count}i", data, position)
    vertices = unpack_array("<3f", bsp.lump(LUMP_VERTEXES))

    by_number = {}
    for entity in entities:
        if entity.classname == "func_occluder":
            try:
                by_number[int(entity.get("occludernumber", "-1"))] = entity
            except ValueError:
                pass

    material = "TOOLS/TOOLSOCCLUDER"
    restored = 0
    for number, (first_poly, poly_count) in enumerate(occluders):
        faces = []
        for first_vertex, vertex_count, plane_index in polys[first_poly:first_poly + poly_count]:
            polygon = [tuple(vertices[vertex_indices[k]])
                       for k in range(first_vertex, first_vertex + vertex_count)
                       if vertex_indices[k] < len(vertices)]
            if len(polygon) >= 3 and plane_index < len(bsp.planes):
                normal, dist = bsp.planes[plane_index]
                faces.append((polygon, normal, dist))
        if not faces:
            continue
        brushes = _occluder_brushes(faces, material)
        entity = by_number.get(number)
        if entity is None:
            entity = Entity([("classname", "func_occluder"), ("StartActive", "1")])
            entities.append(entity)
        entity.remove("occludernumber")
        entity.visgroup = "Occluders"
        entity.brushes.extend(brushes)
        restored += 1
    return restored


def _occluder_brushes(faces, material: str) -> List[Brush]:
    # Usually the polygons are all sides of one closed brush.
    windings = brush_windings([(n, d) for _, n, d in faces]) if len(faces) >= 4 else []
    closed = len(windings) >= 4 and all(
        any(abs(dot(faces[i][1], winding_center(polygon)) - faces[i][2]) < 0.5 for i, _ in windings)
        for polygon, _, _ in faces)
    if closed:
        sides = [side_from_winding(w, faces[i][1], material) for i, w in windings]
        if all(sides):
            return [Brush(sides)]
    # Otherwise give each polygon its own thin brush.
    prisms = (prism_brush(polygon, normal, 4.0, 0.0, top_material=material)
              for polygon, normal, _ in faces)
    return [b for b in prisms if b]


# ---------------------------------------------------------------------------
# Lights
# ---------------------------------------------------------------------------

EMIT_POINT = 1
EMIT_SPOTLIGHT = 2


def restore_lights(bsp: SourceBsp, entities: List[Entity]) -> int:
    """Re-create light / light_spot entities that vbsp removed.

    vbsp strips lights without a targetname from the entity lump, but vrad's
    copy survives in the worldlights lump. Lights still present as entities
    (same origin) are not duplicated.
    """
    data, version = bsp.lump(LUMP_WORLDLIGHTS), bsp.lump_version(LUMP_WORLDLIGHTS)
    if not data:
        data, version = bsp.lump(LUMP_WORLDLIGHTS_HDR), bsp.lump_version(LUMP_WORLDLIGHTS_HDR)
    if not data:
        return 0
    record_size = 100 if version >= 1 else 88
    if len(data) % record_size:
        record_size = next((s for s in (100, 88) if len(data) % s == 0), 0)
        if not record_size:
            return 0
    # Version 1 adds a shadow_cast_offset vector after the normal.
    extra = 12 if record_size == 100 else 0

    existing = []
    for entity in entities:
        if entity.classname in ("light", "light_spot"):
            try:
                existing.append(tuple(float(x) for x in (entity.get("origin") or "").split()[:3]))
            except ValueError:
                pass

    restored = 0
    for offset in range(0, len(data), record_size):
        origin = struct.unpack_from("<3f", data, offset)
        intensity = struct.unpack_from("<3f", data, offset + 12)
        normal = struct.unpack_from("<3f", data, offset + 24)
        tail = offset + 36 + extra
        _, light_type, style = struct.unpack_from("<3i", data, tail)
        stopdot, stopdot2, exponent, radius, constant, linear, quadratic = (
            struct.unpack_from("<7f", data, tail + 12))
        if light_type not in (EMIT_POINT, EMIT_SPOTLIGHT):
            continue
        if any(len(o) == 3 and length(sub(origin, o)) < 1.0 for o in existing):
            continue
        keys = _light_keys(light_type, origin, intensity, normal, style, stopdot, stopdot2,
                           exponent, radius, constant, linear, quadratic)
        if keys:
            entities.append(Entity(keys, visgroup="Restored lights"))
            restored += 1
    return restored


def _light_keys(light_type, origin, intensity, normal, style, stopdot, stopdot2,
                exponent, radius, constant, linear, quadratic) -> Optional[List[Tuple[str, str]]]:
    # vrad stores intensity = (rgb / 255) ^ 2.2 * brightness / 255 * attenuation
    # scale, where the scale makes the light hit its brightness at 100 units.
    if constant < 1e-4 and linear < 1e-4 and quadratic < 1e-4:
        constant = 1.0
    attenuation_scale = constant + 100.0 * linear + 10000.0 * quadratic
    linear_rgb = [max(c, 0.0) / attenuation_scale for c in intensity]
    peak = max(linear_rgb)
    if peak <= 0:
        return None
    rgb = [int(round(255.0 * (c / peak) ** (1 / 2.2))) for c in linear_rgb]
    brightness = format_number(round(peak * 255.0, 2))

    keys = [
        ("classname", "light" if light_type == EMIT_POINT else "light_spot"),
        ("origin", format_vector(origin)),
        ("_light", f"{rgb[0]} {rgb[1]} {rgb[2]} {brightness}"),
        ("_lightHDR", "-1 -1 -1 1"),
        ("_lightscaleHDR", "1"),
        ("_constant_attn", format_number(constant)),
        ("_linear_attn", format_number(linear)),
        ("_quadratic_attn", format_number(quadratic)),
    ]
    if radius > 0:
        keys.append(("_distance", format_number(radius)))
    if 0 < style < 32:
        keys.append(("style", str(style)))
    if light_type == EMIT_SPOTLIGHT:
        # vrad: normal.z = sin(pitch); Hammer's light_spot uses the same pitch.
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, normal[2]))))
        yaw = math.degrees(math.atan2(normal[1], normal[0]))

        def cone(dot_value: float) -> str:
            return format_number(round(math.degrees(math.acos(max(-1.0, min(1.0, dot_value)))), 3))

        keys += [
            ("angles", f"{format_number(round(pitch, 3))} {format_number(round(yaw, 3))} 0"),
            ("pitch", format_number(round(pitch, 3))),
            ("_inner_cone", cone(stopdot)),
            ("_cone", cone(stopdot2)),
            ("_exponent", format_number(exponent)),
        ]
    return keys


# ---------------------------------------------------------------------------
# Packed files
# ---------------------------------------------------------------------------


def extract_pakfile(bsp: SourceBsp, out_dir: str, include_generated: bool = False
                    ) -> Tuple[int, int]:
    """Unzip the files packed into the BSP. Returns (extracted, skipped).

    Compiler-generated cubemaps and patched materials (materials/maps/<map>/)
    are skipped unless ``include_generated`` is set.
    """
    stream = bsp.lump_stream(LUMP_PAKFILE)
    if stream.seek(0, io.SEEK_END) < 22:  # smaller than an empty zip
        return 0, 0
    stream.seek(0)
    extracted = skipped = 0
    root = os.path.abspath(out_dir)
    try:
        archive = zipfile.ZipFile(io.BufferedReader(stream, 1 << 20))
    except zipfile.BadZipFile as error:
        raise ValueError(f"pakfile is not a valid zip ({error})") from error
    with archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            if not include_generated and name.lower().startswith("materials/maps/"):
                skipped += 1
                continue
            destination = os.path.abspath(os.path.join(root, name))
            if not destination.startswith(root + os.sep):
                continue  # refuse paths escaping the output folder
            try:
                payload = archive.read(info)
            except (zipfile.BadZipFile, NotImplementedError, OSError):
                skipped += 1  # unsupported compression or a damaged entry
                continue
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "wb") as f:
                f.write(payload)
            extracted += 1
    return extracted, skipped
