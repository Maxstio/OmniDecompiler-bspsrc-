"""Source engine (VBSP) decompiler.

Source BSPs keep the original brushes, so brush geometry is rebuilt exactly.
Everything the compiler moved out of the entity lump (displacements, static
props, cubemaps, overlays, occluders, stripped lights, packed files) is
restored by :mod:`bsp2vmf.source_features`.
"""

from __future__ import annotations

import logging
import lzma
import struct
import time
import zipfile
from typing import Callable, Dict, List, Optional

from . import source_features as features
from .geometry import brush_windings
from .mapdata import Brush, Entity, Side, find_worldspawn, parse_entity_lump, split_outputs
from .options import Options
from .source_bsp import CONTENTS_AREAPORTAL, CONTENTS_DETAIL, LUMP_ENTITIES, SourceBsp
from .vecmath import Vec3

log = logging.getLogger(__name__)


class SourceDecompiler:
    def __init__(self, data: bytes, version: int, options: Options):
        self.options = options
        self.bsp = SourceBsp(data, version, options.fix_material_names)
        self.entities = parse_entity_lump(self.bsp.lump(LUMP_ENTITIES).decode("latin-1"))
        log.info("%d models, %d brushes, %d brush sides, %d texinfo",
                 len(self.bsp.models), len(self.bsp.brushes), len(self.bsp.brushsides),
                 len(self.bsp.texinfo))

    def run(self, pakfile_dir: Optional[str] = None) -> List[Entity]:
        world = find_worldspawn(self.entities)
        world.remove("model")
        self._build_brushes(world)
        self._restore_features(world, pakfile_dir)
        for entity in self.entities:
            split_outputs(entity)
        return self.entities

    # -- brushes --------------------------------------------------------------

    def _brush(self, brush_index: int, shift: Optional[Vec3]) -> Optional[Brush]:
        bsp = self.bsp
        first_side, side_count, _ = bsp.brushes[brush_index]
        planes, texinfos = [], []
        for si in range(first_side, min(first_side + side_count, len(bsp.brushsides))):
            plane_index, texinfo_index, _, bevel = bsp.brushsides[si]
            # Bevel planes are added by the compiler for collision only.
            if bevel & 0xFF or plane_index >= len(bsp.planes):
                continue
            planes.append(bsp.planes[plane_index])
            texinfos.append(texinfo_index)
        sides = []
        for index, winding in brush_windings(planes):
            side = bsp.make_side(winding, planes[index][0], texinfos[index], shift)
            if side:
                sides.append(side)
        return Brush(sides) if len(sides) >= 4 else None

    def _model_brush_indices(self, model_index: int) -> List[int]:
        """Brushes referenced by the leaves of a model's BSP tree."""
        bsp = self.bsp
        found = set()
        stack = [bsp.models[model_index][9]]
        while stack:
            node = stack.pop()
            if node < 0:
                leaf = -1 - node
                if leaf >= len(bsp.leafs):
                    continue
                _, first, count = bsp.leafs[leaf]
                found.update(bsp.leafbrushes[k] for k in range(first, min(first + count, len(bsp.leafbrushes))))
                continue
            if node < len(bsp.nodes):
                stack.extend(bsp.nodes[node][1:3])
        return sorted(b for b in found if b < len(bsp.brushes))

    def _build_brushes(self, world: Entity) -> None:
        started = time.time()
        bsp = self.bsp
        detail: List[Brush] = []
        areaportals: List[Brush] = []

        def add_world_brush(brush_index: int, brush: Brush) -> None:
            contents = bsp.brushes[brush_index][2]
            if contents & CONTENTS_AREAPORTAL:
                areaportals.append(brush)
            elif self.options.func_detail and contents & CONTENTS_DETAIL:
                detail.append(brush)
            else:
                world.brushes.append(brush)

        used = set()
        if bsp.models:
            for bi in self._model_brush_indices(0):
                used.add(bi)
                brush = self._brush(bi, None)
                if brush:
                    add_world_brush(bi, brush)

        for entity in self.entities:
            index = entity.model_index
            if entity is world or index is None or index <= 0 or index >= len(bsp.models):
                continue
            # Brush entity geometry is stored relative to the entity origin.
            shift = entity.origin
            for bi in self._model_brush_indices(index):
                used.add(bi)
                brush = self._brush(bi, shift)
                if brush:
                    entity.brushes.append(brush)
            entity.remove("model")

        # Brushes no model references (area portals and the like).
        for bi in range(len(bsp.brushes)):
            if bi not in used:
                brush = self._brush(bi, None)
                if brush:
                    add_world_brush(bi, brush)

        self._try("areaportal matching", features.restore_areaportals,
                  bsp, self.entities, areaportals)
        if detail:
            func_detail = Entity([("classname", "func_detail")], visgroup="Detail")
            func_detail.brushes = detail
            self.entities.append(func_detail)
        log.info("rebuilt %d world + %d detail brushes in %.1fs",
                 len(world.brushes), len(detail), time.time() - started)

    # -- everything else --------------------------------------------------------

    def _restore_features(self, world: Entity, pakfile_dir: Optional[str]) -> None:
        options, bsp, entities = self.options, self.bsp, self.entities
        vmf = options.format == "vmf"

        displacement_sides: Dict[int, Side] = {}
        if options.displacements and vmf:
            count = self._try("displacements", features.restore_displacements, bsp, world,
                              options.displacement_thickness, displacement_sides)
            if count is not None:
                log.info("rebuilt %d displacements", count)

        if options.static_props:
            props = self._try("static props", features.read_static_props, bsp)
            if props is not None:
                entities.extend(props)
                log.info("restored %d prop_static", len(props))

        if options.cubemaps:
            cubemaps = features.read_cubemaps(bsp)
            entities.extend(cubemaps)
            if cubemaps:
                log.info("restored %d env_cubemap", len(cubemaps))

        if options.overlays and vmf:
            result = self._try("overlays", features.restore_overlays, bsp, entities,
                               displacement_sides)
            if result is not None:
                count, unlinked = result
                extra = f" ({unlinked} without a matching face)" if unlinked else ""
                log.info("restored %d info_overlay%s", count, extra)

        if options.occluders:
            count = self._try("occluders", features.restore_occluders, bsp, entities)
            if count:
                log.info("rebuilt %d func_occluder", count)

        if options.restore_lights:
            count = self._try("lights", features.restore_lights, bsp, entities)
            if count:
                log.info("restored %d light entities removed at compile time", count)

        if pakfile_dir:
            result = self._try("packed files", features.extract_pakfile, bsp, pakfile_dir,
                               options.extract_generated_files)
            if result is not None:
                extracted, skipped = result
                log.info("extracted %d packed files to %s (%d skipped)",
                         extracted, pakfile_dir, skipped)

    @staticmethod
    def _try(what: str, func: Callable, *args):
        """Run an optional reconstruction step; a failure only skips that step.

        Lump layouts vary between Source branches, so one unreadable optional
        lump should not lose the whole map.
        """
        try:
            return func(*args)
        except (ValueError, IndexError, KeyError, struct.error, OSError,
                lzma.LZMAError, zipfile.BadZipFile) as error:
            log.warning("%s skipped: %s", what, error)
            return None
