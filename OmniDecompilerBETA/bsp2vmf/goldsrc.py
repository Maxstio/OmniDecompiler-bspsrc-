"""GoldSrc (BSP v30) and Quake (BSP v29) decompiler.

These formats no longer contain the original brushes, so geometry is rebuilt
from the BSP tree: every solid, liquid or sky leaf is a convex volume and
becomes a brush. Leaves are cut further where one side carries several
textures, sides are textured from the rendered faces lying on them, and the
pieces are merged back into larger brushes afterwards. CLIP brushes, which
only exist in the collision hulls, are recovered from hull 1.
"""

from __future__ import annotations

import logging
import os
import struct
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from .binary import c_string, unpack_array, unpack_ints
from .brushes import box_brush, merge_brushes, side_from_winding
from .geometry import (Polyhedron, box_polyhedron, brush_windings, point_in_winding,
                       split_polyhedron, winding_center)
from .mapdata import (Brush, Entity, Side, axes_from_texture_vecs, default_axes,
                      find_worldspawn, parse_entity_lump, pick_plane_points, source_material)
from .options import Options
from .vecmath import Vec3, add, cross, dot, length, normalize, scale, sub

log = logging.getLogger(__name__)

(LUMP_ENTITIES, LUMP_PLANES, LUMP_TEXTURES, LUMP_VERTEXES, LUMP_VISIBILITY, LUMP_NODES,
 LUMP_TEXINFO, LUMP_FACES, LUMP_LIGHTING, LUMP_CLIPNODES, LUMP_LEAFS, LUMP_MARKSURFACES,
 LUMP_EDGES, LUMP_SURFEDGES, LUMP_MODELS) = range(15)

CONTENTS_EMPTY = -1
CONTENTS_SOLID = -2
CONTENTS_SKY = -6
CONTENTS_CLIP = -8
SOLID_CONTENTS = {CONTENTS_SOLID, CONTENTS_SKY}
LIQUID_CONTENTS = {-3, -4, -5, -9, -10, -11, -12, -13, -14}  # water, slime, lava, currents

# Player hull 1 (standing) used to expand brushes for collision.
HULL1_HALFLIFE = ((-16.0, -16.0, -36.0), (16.0, 16.0, 36.0))
HULL1_QUAKE = ((-16.0, -16.0, -24.0), (16.0, 16.0, 32.0))

# A face candidate: (center, winding, texinfo index)
FaceRef = Tuple[Vec3, List[Vec3], int]


class GoldSrcDecompiler:
    def __init__(self, data: bytes, version: int, options: Options):
        self.version = version
        self.options = options
        self.lumps = [struct.unpack_from("<ii", data, 4 + i * 8) for i in range(15)]
        self.data = data

        # Blue Shift stores the plane lump first and the entity lump second.
        if (self._lump(LUMP_ENTITIES).lstrip()[:1] != b"{"
                and self._lump(LUMP_PLANES).lstrip()[:1] == b"{"):
            self.lumps[LUMP_ENTITIES], self.lumps[LUMP_PLANES] = (
                self.lumps[LUMP_PLANES], self.lumps[LUMP_ENTITIES])
            log.info("Blue Shift lump layout detected")

        self.entities = parse_entity_lump(self._lump(LUMP_ENTITIES).decode("latin-1"))
        self.planes = [((a, b, c), d) for a, b, c, d, _ in unpack_array("<4fi", self._lump(LUMP_PLANES))]
        self.vertices = unpack_array("<3f", self._lump(LUMP_VERTEXES))
        self.nodes = unpack_array("<i2h6h2H", self._lump(LUMP_NODES))
        self.texinfo = unpack_array("<8f2i", self._lump(LUMP_TEXINFO))
        self.faces = unpack_array("<2HiHH4Bi", self._lump(LUMP_FACES))
        self.leafs = unpack_array("<2i6h2H4B", self._lump(LUMP_LEAFS))
        self.edges = unpack_array("<2H", self._lump(LUMP_EDGES))
        self.surfedges = unpack_ints(self._lump(LUMP_SURFEDGES))
        self.models = unpack_array("<9f4i3i", self._lump(LUMP_MODELS))
        self.clipnodes = unpack_array("<i2h", self._lump(LUMP_CLIPNODES))
        self.texture_lump = self._lump(LUMP_TEXTURES)
        self.texture_names = self._read_texture_names()

        self.nodraw = options.nodraw_material
        self.clip_material = "TOOLS/TOOLSCLIP" if options.format == "vmf" else "CLIP"
        self.hull_mins, self.hull_maxs = HULL1_QUAKE if version == 29 else HULL1_HALFLIFE
        self._signatures: Dict[int, Tuple] = {}

        log.info("%d models, %d nodes, %d leafs, %d faces, %d textures",
                 len(self.models), len(self.nodes), len(self.leafs), len(self.faces),
                 len(self.texture_names))

    def _lump(self, index: int) -> bytes:
        offset, size = self.lumps[index]
        return self.data[offset:offset + size]

    def _read_texture_names(self) -> List[str]:
        lump = self.texture_lump
        if len(lump) < 4:
            return []
        names = []
        for i in range(struct.unpack_from("<i", lump, 0)[0]):
            offset = struct.unpack_from("<i", lump, 4 + i * 4)[0]
            if offset < 0 or offset + 16 > len(lump):
                names.append("NULL")
            else:
                names.append(c_string(lump[offset:offset + 16]) or "NULL")
        return names

    # -- entry point --------------------------------------------------------

    def run(self, wad_path: Optional[str] = None) -> List[Entity]:
        world = find_worldspawn(self.entities)
        started = time.time()

        world.brushes = self._model_brushes(0, None)
        unmerged = len(world.brushes)
        if self.options.clip_brushes:
            clips = self._clip_brushes(0)
            log.info("recovered %d clip brush pieces from hull 1", len(clips))
            world.brushes += clips
        if self.options.merge_brushes:
            world.brushes = merge_brushes(world.brushes, self.nodraw)
        total = len(world.brushes)

        for entity in self.entities:
            index = entity.model_index
            if entity is world or index is None or index <= 0 or index >= len(self.models):
                continue
            shift = entity.origin
            entity.brushes = self._model_brushes(index, shift)
            unmerged += len(entity.brushes)
            if self.options.merge_brushes:
                entity.brushes = merge_brushes(entity.brushes, self.nodraw)
            entity.remove("model")
            total += len(entity.brushes)
            if shift is not None and self.options.format == "map":
                # GoldSrc editors expect an ORIGIN brush rather than an origin key.
                entity.brushes.append(box_brush(shift, 8.0, "ORIGIN", 1.0))
                entity.remove("origin")
        world.remove("model")
        log.info("rebuilt %d brushes (%d before merging) in %.1fs",
                 total, unmerged, time.time() - started)

        if wad_path:
            self._export_embedded_textures(world, wad_path)
        if self.options.format == "map" and world.get("mapversion") is None:
            world.set("mapversion", "220")
        return self.entities

    def _export_embedded_textures(self, world: Entity, wad_path: str) -> None:
        count = export_wad(self.texture_lump, wad_path)
        if not count:
            return
        log.info("exported %d embedded textures to %s", count, wad_path)
        wad_name = os.path.basename(wad_path)
        wads = world.get("wad", "") or ""
        if wad_name.lower() not in wads.lower():
            world.set("wad", (wads.rstrip(";") + ";" if wads else "") + wad_name)

    # -- BSP helpers --------------------------------------------------------

    def _face_winding(self, face_index: int) -> List[Vec3]:
        face = self.faces[face_index]
        first, count = face[2], face[3]
        winding = []
        for i in range(count):
            edge = self.surfedges[first + i]
            vertex = self.edges[edge][0] if edge >= 0 else self.edges[-edge][1]
            winding.append(self.vertices[vertex])
        return winding

    def _child(self, child: int) -> int:
        # Maps with more than 32767 nodes store children as unsigned shorts.
        if child < 0 and -(child + 1) >= len(self.leafs):
            return child & 0xFFFF
        return child

    def _texture_signature(self, texinfo_index: int) -> Tuple:
        """Texture name plus alignment, used to tell faces apart."""
        signature = self._signatures.get(texinfo_index)
        if signature is None:
            info = self.texinfo[texinfo_index]
            name = self.texture_names[info[8]].lower() if 0 <= info[8] < len(self.texture_names) else ""
            signature = (name, tuple(round(x, 3) for x in info[0:8]))
            self._signatures[texinfo_index] = signature
        return signature

    def _texture(self, texinfo_index: int) -> Tuple[str, Sequence[float], Sequence[float]]:
        info = self.texinfo[texinfo_index]
        name = self.texture_names[info[8]] if 0 <= info[8] < len(self.texture_names) else self.nodraw
        if self.options.format == "vmf":
            name = source_material(name)
        return name, info[0:4], info[4:8]

    def _contents_hull0(self, point: Vec3, node: int) -> int:
        while node >= 0:
            if node >= len(self.nodes):
                return CONTENTS_EMPTY
            plane_index, front, back = self.nodes[node][0:3]
            normal, dist = self.planes[plane_index]
            node = self._child(front if dot(normal, point) - dist >= 0 else back)
        leaf = -(node + 1)
        return self.leafs[leaf][0] if leaf < len(self.leafs) else CONTENTS_SOLID

    def _contents_clip(self, point: Vec3, node: int) -> int:
        while node >= 0:
            if node >= len(self.clipnodes):
                return CONTENTS_EMPTY
            plane_index, front, back = self.clipnodes[node]
            normal, dist = self.planes[plane_index]
            node = front if dot(normal, point) - dist >= 0 else back
        return node

    # -- visible brushes ----------------------------------------------------

    def _model_brushes(self, model_index: int, shift: Optional[Vec3]) -> List[Brush]:
        """Walk hull 0 of a model and turn its solid/liquid leaves into brushes."""
        model = self.models[model_index]
        mins, maxs = model[0:3], model[3:6]
        headnode = model[9]
        first_face, face_count = model[14], model[15]

        # Rendered faces of this model, keyed by (plane, side) of their normal.
        faces_by_plane: Dict[Tuple[int, int], List[FaceRef]] = defaultdict(list)
        for fi in range(first_face, min(first_face + face_count, len(self.faces))):
            winding = self._face_winding(fi)
            if len(winding) >= 3:
                face = self.faces[fi]
                faces_by_plane[(face[0], face[1] & 1)].append((winding_center(winding), winding, face[4]))

        margin = 1.0
        box = box_polyhedron(tuple(x - margin for x in mins), tuple(x + margin for x in maxs))
        brushes = []
        stack = [(headnode, box)]
        while stack:
            node, poly = stack.pop()
            if poly is None:
                continue
            if node < 0:
                leaf = -(node + 1)
                if leaf >= len(self.leafs):
                    continue
                contents = self.leafs[leaf][0]
                if contents in SOLID_CONTENTS or contents in LIQUID_CONTENTS:
                    sky = contents == CONTENTS_SKY
                    uniform = sky or contents in LIQUID_CONTENTS
                    for piece in self._split_by_texture(poly, faces_by_plane, sky):
                        brush = self._leaf_brush(piece, faces_by_plane, uniform, shift, sky)
                        if brush:
                            brush.contents = contents
                            brushes.append(brush)
                continue
            if node >= len(self.nodes):
                continue
            plane_index, front_child, back_child = self.nodes[node][0:3]
            normal, dist = self.planes[plane_index]
            front, back = split_polyhedron(poly, normal, dist, (plane_index, 0), (plane_index, 1))
            stack.append((self._child(front_child), front))
            stack.append((self._child(back_child), back))
        return brushes

    def _candidates(self, face_key, faces_by_plane, sky: bool) -> List[FaceRef]:
        if face_key is None:
            return []
        candidates = faces_by_plane.get(face_key)
        if sky and not candidates:
            # Sky faces point into the sky volume rather than out of it.
            candidates = faces_by_plane.get((face_key[0], face_key[1] ^ 1))
        return candidates or []

    def _split_by_texture(self, poly: Polyhedron, faces_by_plane, sky: bool,
                          depth: int = 0) -> List[Polyhedron]:
        """Cut a leaf until each of its sides carries a single texture.

        One BSP leaf can span several original brushes, so a single leaf side
        may hold faces with different textures. Splitting along an edge of one
        of those faces separates them again.
        """
        if depth > 10:
            return [poly]
        for face in poly:
            inside = [c for c in self._candidates(face.key, faces_by_plane, sky)
                      if point_in_winding(c[0], face.winding, face.normal)]
            if len(inside) < 2 or len({self._texture_signature(c[2]) for c in inside}) < 2:
                continue
            for center, winding, texinfo_index in inside:
                signature = self._texture_signature(texinfo_index)
                for i in range(len(winding)):
                    a, b = winding[i], winding[(i + 1) % len(winding)]
                    cut_normal = cross(sub(b, a), face.normal)
                    if length(cut_normal) < 1e-3:
                        continue
                    cut_normal = normalize(cut_normal)
                    cut_dist = dot(cut_normal, a)
                    own_side = dot(cut_normal, center) - cut_dist
                    if abs(own_side) < 0.5:
                        continue
                    separates = any(
                        self._texture_signature(other[2]) != signature
                        and (dot(cut_normal, other[0]) - cut_dist) * own_side < -0.25
                        for other in inside)
                    if not separates:
                        continue
                    front, back = split_polyhedron(poly, cut_normal, cut_dist, None, None)
                    if front and back:
                        return (self._split_by_texture(front, faces_by_plane, sky, depth + 1)
                                + self._split_by_texture(back, faces_by_plane, sky, depth + 1))
        return [poly]

    def _match_face(self, face, candidates: List[FaceRef]) -> FaceRef:
        """The rendered face that best covers a polyhedron face."""
        for candidate in candidates:
            if point_in_winding(candidate[0], face.winding, face.normal):
                return candidate
        center = winding_center(face.winding)
        for candidate in candidates:
            if point_in_winding(center, candidate[1], face.normal):
                return candidate
        return min(candidates, key=lambda c: length(sub(c[0], center)))

    def _leaf_brush(self, poly: Polyhedron, faces_by_plane, uniform: bool,
                    shift: Optional[Vec3], sky: bool) -> Optional[Brush]:
        """Turn a leaf polyhedron into a brush textured from the rendered faces.

        ``uniform`` brushes (liquids, sky) give every untextured side the most
        common texture, as their sides must all match in the editor.
        """
        textures = []
        for face in poly:
            texture = None
            candidates = self._candidates(face.key, faces_by_plane, sky)
            if candidates:
                texture = self._texture(self._match_face(face, candidates)[2])
            textures.append(texture)

        found = [t for t in textures if t is not None]
        fallback = None
        if uniform and found:
            most_common = Counter(t[0] for t in found).most_common(1)[0][0]
            fallback = next(t for t in found if t[0] == most_common)

        sides = []
        for face, texture in zip(poly, textures):
            winding = face.winding
            if shift is not None:
                winding = [add(p, shift) for p in winding]
            points = pick_plane_points(winding, face.normal)
            if points is None:
                continue
            texture = texture or fallback
            if texture is None:
                u, v = default_axes(face.normal, 1.0)
                sides.append(Side(points, face.normal, self.nodraw, u, v, winding=winding))
            else:
                u, v = axes_from_texture_vecs(texture[1], texture[2], face.normal, 1.0, shift)
                sides.append(Side(points, face.normal, texture[0], u, v, winding=winding))
        return Brush(sides) if len(sides) >= 4 else None

    # -- clip brushes ---------------------------------------------------------

    def _hull_offset(self, normal: Vec3) -> float:
        """How far the compiler pushed a plane outwards to build hull 1."""
        return sum(normal[i] * (-self.hull_mins[i] if normal[i] > 0 else -self.hull_maxs[i])
                   for i in range(3))

    def _clip_brushes(self, model_index: int) -> List[Brush]:
        """Recover CLIP brushes from the player collision hull.

        Solid leaves of hull 1 are shrunk back by the player box; the pieces
        that turn out empty in the visible hull 0 are clip volumes.
        """
        model = self.models[model_index]
        head0, head1 = model[9], model[10]
        if head1 < 0 or head1 >= len(self.clipnodes):
            return []
        pad = 64.0
        box = box_polyhedron(tuple(model[i] - pad for i in range(3)),
                             tuple(model[3 + i] + pad for i in range(3)))
        brushes = []
        stack = [(head1, box)]
        while stack:
            node, poly = stack.pop()
            if poly is None:
                continue
            if node < 0:
                if node == CONTENTS_SOLID:
                    brush = self._clip_brush(poly, head0, head1)
                    if brush:
                        brushes.append(brush)
                continue
            plane_index, front_child, back_child = self.clipnodes[node]
            normal, dist = self.planes[plane_index]
            front, back = split_polyhedron(poly, normal, dist, (plane_index, 0), (plane_index, 1))
            stack.append((front_child, front))
            stack.append((back_child, back))
        return brushes

    def _clip_brush(self, poly: Polyhedron, head0: int, head1: int) -> Optional[Brush]:
        shrunk = []
        for face in poly:
            # Faces against other hull-1 solid are internal splits: don't shrink them.
            probe = add(winding_center(face.winding), scale(face.normal, 0.5))
            internal = face.key is not None and self._contents_clip(probe, head1) == CONTENTS_SOLID
            offset = 0.0 if internal else self._hull_offset(face.normal)
            shrunk.append((face.normal, face.dist - offset))

        windings = brush_windings(shrunk)
        if len(windings) < 4:
            return None
        points = [p for _, w in windings for p in w]
        center = winding_center(points)
        extent = [max(p[k] for p in points) - min(p[k] for p in points) for k in range(3)]
        if min(extent) < 2.0 or extent[0] * extent[1] * extent[2] < 64.0:
            return None  # sliver left over from BSP splits
        if self._contents_hull0(center, head0) in SOLID_CONTENTS:
            return None
        samples = [add(center, scale(sub(p, center), 0.75)) for p in points]
        empty = sum(1 for s in samples if self._contents_hull0(s, head0) not in SOLID_CONTENTS)
        if empty < 0.75 * len(samples):
            return None

        sides = []
        for index, winding in windings:
            side = side_from_winding(winding, shrunk[index][0], self.clip_material, 1.0)
            if side:
                sides.append(side)
        if len(sides) < 4:
            return None
        return Brush(sides, visgroup="Clip brushes", contents=CONTENTS_CLIP)


def export_wad(texture_lump: bytes, path: str) -> int:
    """Write the textures embedded in a BSP's texture lump to a WAD3 file.

    Textures stored in external WADs (no pixel data in the BSP) are skipped.
    Returns the number of textures written.
    """
    if len(texture_lump) < 4:
        return 0
    count = struct.unpack_from("<i", texture_lump, 0)[0]
    offsets = [struct.unpack_from("<i", texture_lump, 4 + i * 4)[0] for i in range(count)]
    valid_offsets = sorted(o for o in offsets if 0 <= o < len(texture_lump))

    entries = []
    seen = set()
    for offset in offsets:
        if offset < 0 or offset + 40 > len(texture_lump):
            continue
        name = c_string(texture_lump[offset:offset + 16])
        width, height, mip0 = struct.unpack_from("<IIi", texture_lump, offset + 16)
        if (mip0 <= 0 or not name or name.lower() in seen
                or width == 0 or height == 0 or width * height > 1 << 24):
            continue
        # header + 4 mip levels (1 + 1/4 + 1/16 + 1/64) + palette size + palette
        needed = 40 + (width * height * 85) // 64 + 2 + 768
        following = [o for o in valid_offsets if o > offset]
        end = min(following[0] if following else len(texture_lump), len(texture_lump))
        blob = texture_lump[offset:end]
        if len(blob) < needed:
            continue
        seen.add(name.lower())
        entries.append((name, blob[:needed] + b"\0\0"))  # WAD entries are padded

    if not entries:
        return 0
    body = bytearray()
    directory = bytearray()
    position = 12
    for name, blob in entries:
        directory += struct.pack("<iiibbh16s", position, len(blob), len(blob), 0x43, 0, 0,
                                 name.encode("latin-1")[:15])
        body += blob
        position += len(blob)
    with open(path, "wb") as f:
        f.write(b"WAD3" + struct.pack("<ii", len(entries), 12 + len(body)))
        f.write(body)
        f.write(directory)
    return len(entries)
