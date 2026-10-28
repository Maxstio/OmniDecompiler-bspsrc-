"""Reading Source engine (VBSP) map files."""

from __future__ import annotations

import logging
import re
import struct
from typing import Dict, List, Optional, Tuple

from .binary import ByteWindow, lzma_decompress, unpack_array, unpack_ints
from .brushes import NODRAW
from .mapdata import Side, axes_from_texture_vecs, default_axes, pick_plane_points
from .vecmath import Vec3, add, length

log = logging.getLogger(__name__)

LUMP_ENTITIES = 0
LUMP_PLANES = 1
LUMP_TEXDATA = 2
LUMP_VERTEXES = 3
LUMP_NODES = 5
LUMP_TEXINFO = 6
LUMP_FACES = 7
LUMP_OCCLUSION = 9
LUMP_LEAFS = 10
LUMP_EDGES = 12
LUMP_SURFEDGES = 13
LUMP_MODELS = 14
LUMP_WORLDLIGHTS = 15
LUMP_LEAFBRUSHES = 17
LUMP_BRUSHES = 18
LUMP_BRUSHSIDES = 19
LUMP_AREAPORTALS = 21
LUMP_DISPINFO = 26
LUMP_DISP_VERTS = 33
LUMP_GAME_LUMP = 35
LUMP_PAKFILE = 40
LUMP_CLIPPORTALVERTS = 41
LUMP_CUBEMAPS = 42
LUMP_TEXDATA_STRING_DATA = 43
LUMP_TEXDATA_STRING_TABLE = 44
LUMP_OVERLAYS = 45
LUMP_WORLDLIGHTS_HDR = 54
LUMP_OVERLAY_FADES = 60

CONTENTS_AREAPORTAL = 0x8000
CONTENTS_DETAIL = 0x8000000

HEADER_SIZE = 8 + 64 * 16 + 4
FACE_SIZE = 56

# vbsp renames materials of faces it assigns a cubemap or water patch to.
_PATCHED_MATERIAL = re.compile(r"^maps/[^/]+/(.+?)(?:_-?\d+_-?\d+_-?\d+)?(?:_wvt_patch)?$", re.I)


class SourceBsp:
    """Lump access and the tables shared by the decompiler and its features."""

    def __init__(self, data: bytes, version: int, fix_material_names: bool = True):
        self.data = data
        self.version = version
        self.fix_material_names = fix_material_names
        self.lumps = self._read_lump_directory()

        self.planes = [((a, b, c), d) for a, b, c, d, _ in unpack_array("<4fi", self.lump(LUMP_PLANES))]
        self.texdata = unpack_array("<3f5i", self.lump(LUMP_TEXDATA))
        self.texinfo = unpack_array("<16f2i", self.lump(LUMP_TEXINFO))
        self.nodes = unpack_array("<3i6h2H2h", self.lump(LUMP_NODES))
        self.models = unpack_array("<9f3i", self.lump(LUMP_MODELS))
        self.brushes = unpack_array("<3i", self.lump(LUMP_BRUSHES))
        self.brushsides = unpack_array("<H3h", self.lump(LUMP_BRUSHSIDES))
        self.leafbrushes = [x[0] for x in unpack_array("<H", self.lump(LUMP_LEAFBRUSHES))]
        self.leafs = self._read_leafs()
        self._string_data = self.lump(LUMP_TEXDATA_STRING_DATA)
        self._string_table = unpack_ints(self.lump(LUMP_TEXDATA_STRING_TABLE))
        self._material_names: Dict[int, str] = {}

    def _read_lump_directory(self) -> List[Tuple[int, int, int]]:
        """(offset, length, version) per lump. L4D2 stores the version first."""
        entries = [struct.unpack_from("<iiii", self.data, 8 + i * 16) for i in range(64)]

        def plausible(offset_index: int, length_index: int) -> bool:
            for entry in entries:
                offset, size = entry[offset_index], entry[length_index]
                if size == 0:
                    continue
                if offset < HEADER_SIZE or size < 0 or offset + size > len(self.data):
                    return False
            return True

        if not plausible(0, 1) and plausible(1, 2):
            log.info("L4D2-style lump headers detected")
            return [(offset, size, version) for version, offset, size, _ in entries]
        return [(offset, size, version) for offset, size, version, _ in entries]

    def _read_leafs(self) -> List[Tuple[int, int, int]]:
        """(contents, first leafbrush, leafbrush count) per leaf."""
        data = self.lump(LUMP_LEAFS)
        # Version 0 leafs (HL2 / BSP v19) carry 24 bytes of ambient lighting.
        size = 56 if (self.lump_version(LUMP_LEAFS) == 0 and self.version <= 19) else 32
        if len(data) % size:
            size = 32 if size == 56 else 56
        leafs = []
        for i in range(len(data) // size):
            contents = struct.unpack_from("<i", data, i * size)[0]
            first, count = struct.unpack_from("<2H", data, i * size + 24)
            leafs.append((contents, first, count))
        return leafs

    def lump(self, index: int) -> bytes:
        offset, size, _ = self.lumps[index]
        return lzma_decompress(self.data[offset:offset + size])

    def lump_stream(self, index: int) -> ByteWindow:
        """An uncompressed lump as a file object, without copying it into memory."""
        offset, size, _ = self.lumps[index]
        return ByteWindow(self.data, offset, size)

    def lump_version(self, index: int) -> int:
        return self.lumps[index][2]

    def material(self, texinfo_index: int) -> str:
        """Material name of a texinfo, with cubemap patching undone."""
        if texinfo_index < 0 or texinfo_index >= len(self.texinfo):
            return NODRAW
        texdata_index = self.texinfo[texinfo_index][17]
        cached = self._material_names.get(texdata_index)
        if cached is not None:
            return cached
        name = NODRAW
        if 0 <= texdata_index < len(self.texdata):
            string_index = self.texdata[texdata_index][3]
            if 0 <= string_index < len(self._string_table):
                start = self._string_table[string_index]
                end = self._string_data.find(b"\0", start)
                name = self._string_data[start:end if end >= 0 else None].decode("latin-1")
        if self.fix_material_names:
            match = _PATCHED_MATERIAL.match(name.replace("\\", "/"))
            if match:
                name = match.group(1)
        self._material_names[texdata_index] = name
        return name

    def make_side(self, winding: List[Vec3], normal: Vec3, texinfo_index: int,
                  shift: Optional[Vec3]) -> Optional[Side]:
        """A brush side textured from a texinfo; ``shift`` moves the geometry."""
        if shift is not None:
            winding = [add(p, shift) for p in winding]
        points = pick_plane_points(winding, normal)
        if points is None:
            return None
        if 0 <= texinfo_index < len(self.texinfo):
            info = self.texinfo[texinfo_index]
            u, v = axes_from_texture_vecs(info[0:4], info[4:8], normal, 0.25, shift)
            luxels_per_unit = length(info[8:11])
            lightmap_scale = int(round(1.0 / luxels_per_unit)) if luxels_per_unit > 1e-6 else 16
        else:
            u, v = default_axes(normal, 0.25)
            lightmap_scale = 16
        return Side(points, normal, self.material(texinfo_index), u, v,
                    max(1, lightmap_scale), winding=winding)
