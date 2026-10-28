"""Writing maps as VMF (Source Hammer) or Valve 220 MAP (GoldSrc editors)."""

from __future__ import annotations

from typing import Dict, List

from .mapdata import Brush, Displacement, Entity, Side, format_number, format_vector

WORLD_COLOR = "0 180 0"
ENTITY_COLOR = "220 30 220"
VISGROUP_COLORS = ("0 200 255", "255 160 0", "200 0 255", "255 255 0", "0 255 120", "255 80 80")


def _quote(text: str) -> str:
    # Neither format can escape quotes inside values.
    return text.replace('"', "'")


def _write_lines(path: str, lines: List[str]) -> None:
    with open(path, "w", encoding="latin-1", errors="replace", newline="\r\n") as f:
        f.write("\n".join(lines))
        f.write("\n")


class VmfWriter:
    """Serialises entities to Hammer's VMF format."""

    def __init__(self) -> None:
        self._lines: List[str] = []
        self._indent = 0
        self._next_id = 1
        self._visgroups: Dict[str, int] = {}

    def write(self, entities: List[Entity], path: str) -> None:
        self._collect_visgroups(entities)
        # Side ids are assigned first so overlays can reference them.
        for entity in entities:
            for brush in entity.brushes:
                for side in brush.sides:
                    side.id = self._new_id()
        self._write_header()
        world = next((e for e in entities if e.classname == "worldspawn"), None)
        if world is not None:
            self._write_world(world)
        for entity in entities:
            if entity is not world and entity.classname != "worldspawn":
                self._write_entity(entity)
        self._write_footer()
        _write_lines(path, self._lines)

    # -- low level -----------------------------------------------------------

    def _new_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _line(self, text: str) -> None:
        self._lines.append("\t" * self._indent + text)

    def _kv(self, key: str, value) -> None:
        self._line(f'"{_quote(key)}" "{_quote(str(value))}"')

    def _open(self, name: str) -> None:
        self._line(name)
        self._line("{")
        self._indent += 1

    def _close(self) -> None:
        self._indent -= 1
        self._line("}")

    def _block(self, name: str, pairs) -> None:
        self._open(name)
        for key, value in pairs:
            self._kv(key, value)
        self._close()

    # -- structure -----------------------------------------------------------

    def _collect_visgroups(self, entities: List[Entity]) -> None:
        names: List[str] = []
        for entity in entities:
            for name in [entity.visgroup] + [b.visgroup for b in entity.brushes]:
                if name and name not in names:
                    names.append(name)
        self._visgroups = {name: i + 1 for i, name in enumerate(names)}

    def _write_header(self) -> None:
        self._block("versioninfo", [("editorversion", 400), ("editorbuild", 8864),
                                    ("mapversion", 1), ("formatversion", 100), ("prefab", 0)])
        self._open("visgroups")
        for i, (name, group_id) in enumerate(self._visgroups.items()):
            self._block("visgroup", [("name", name), ("visgroupid", group_id),
                                     ("color", VISGROUP_COLORS[i % len(VISGROUP_COLORS)])])
        self._close()
        self._block("viewsettings", [("bSnapToGrid", 1), ("bShowGrid", 1), ("bShowLogicalGrid", 0),
                                     ("nGridSpacing", 64), ("bShow3DGrid", 0)])

    def _write_footer(self) -> None:
        self._block("cameras", [("activecamera", -1)])
        self._block("cordon", [("mins", "(-1024 -1024 -1024)"), ("maxs", "(1024 1024 1024)"),
                               ("active", 0)])

    def _write_world(self, world: Entity) -> None:
        self._open("world")
        self._kv("id", self._new_id())
        for key, value in world.keys:
            if key.lower() != "id":
                self._kv(key, value)
        for brush in world.brushes:
            self._write_solid(brush, WORLD_COLOR)
        self._close()

    def _write_entity(self, entity: Entity) -> None:
        self._open("entity")
        self._kv("id", self._new_id())
        for key, value in entity.keys:
            if key.lower() != "id":
                self._kv(key, value)
        if entity.side_refs is not None:
            self._kv("sides", " ".join(str(s.id) for s in entity.side_refs if s.id))
        if entity.connections:
            self._block("connections", entity.connections)
        for brush in entity.brushes:
            self._write_solid(brush, ENTITY_COLOR)
        self._open("editor")
        self._kv("color", ENTITY_COLOR)
        self._write_visgroup(entity.visgroup)
        self._kv("visgroupshown", 1)
        self._kv("visgroupautoshown", 1)
        self._kv("logicalpos", "[0 0]")
        self._close()
        self._close()

    def _write_visgroup(self, name) -> None:
        if name in self._visgroups:
            self._kv("visgroupid", self._visgroups[name])

    def _write_solid(self, brush: Brush, color: str) -> None:
        self._open("solid")
        self._kv("id", self._new_id())
        for side in brush.sides:
            self._write_side(side)
        self._open("editor")
        self._kv("color", color)
        self._write_visgroup(brush.visgroup)
        self._kv("visgroupshown", 1)
        self._kv("visgroupautoshown", 1)
        self._close()
        self._close()

    def _write_side(self, side: Side) -> None:
        self._open("side")
        self._kv("id", side.id if side.id is not None else self._new_id())
        self._kv("plane", " ".join(f"({format_vector(p)})" for p in side.points))
        self._kv("material", side.material.upper())
        for name, axis in (("uaxis", side.u), ("vaxis", side.v)):
            self._kv(name, f"[{format_vector(axis.axis)} {format_number(axis.offset)}] "
                           f"{format_number(axis.scale)}")
        self._kv("rotation", 0)
        self._kv("lightmapscale", side.lightmap_scale)
        self._kv("smoothing_groups", 0)
        if side.displacement is not None:
            self._write_displacement(side.displacement)
        self._close()

    def _write_displacement(self, disp: Displacement) -> None:
        size = disp.size

        def rows(name: str, values) -> None:
            self._block(name, [(f"row{r}", row) for r, row in enumerate(values)])

        self._open("dispinfo")
        self._kv("power", disp.power)
        self._kv("startposition", f"[{format_vector(disp.start_position)}]")
        self._kv("flags", 0)
        self._kv("elevation", 0)
        self._kv("subdiv", 0)
        rows("normals", (" ".join(format_vector(n) for n in row) for row in disp.normals))
        rows("distances", (" ".join(format_number(d) for d in row) for row in disp.distances))
        rows("offsets", (" ".join(["0 0 0"] * size) for _ in range(size)))
        offset_normal = " ".join([format_vector(disp.offset_normal)] * size)
        rows("offset_normals", (offset_normal for _ in range(size)))
        rows("alphas", (" ".join(format_number(a) for a in row) for row in disp.alphas))
        self._block("allowed_verts", [("10", " ".join(str(a) for a in disp.allowed_verts))])
        self._close()


def write_vmf(entities: List[Entity], path: str) -> None:
    VmfWriter().write(entities, path)


def write_map(entities: List[Entity], path: str) -> None:
    """Valve 220 ``.map`` for Hammer 3.x, J.A.C.K. and TrenchBroom."""
    lines: List[str] = []
    ordered = sorted(entities, key=lambda e: e.classname != "worldspawn")
    for entity_index, entity in enumerate(ordered):
        lines.append(f"// entity {entity_index}")
        lines.append("{")
        for key, value in entity.keys + entity.connections:
            lines.append(f'"{_quote(key)}" "{_quote(value)}"')
        for brush_index, brush in enumerate(entity.brushes):
            lines.append(f"// brush {brush_index}")
            lines.append("{")
            for side in brush.sides:
                points = " ".join(f"( {format_vector(p)} )" for p in side.points)
                lines.append(
                    f"{points} {side.material} "
                    f"[ {format_vector(side.u.axis)} {format_number(side.u.offset)} ] "
                    f"[ {format_vector(side.v.axis)} {format_number(side.v.offset)} ] "
                    f"0 {format_number(side.u.scale)} {format_number(side.v.scale)}")
            lines.append("}")
        lines.append("}")
    _write_lines(path, lines)
