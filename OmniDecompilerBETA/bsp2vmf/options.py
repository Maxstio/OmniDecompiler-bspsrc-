"""Conversion options."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Options:
    """What to reconstruct and how. Defaults match the command line."""

    #: Output format: "vmf" (Source Hammer) or "map" (Valve 220 for GoldSrc editors).
    format: str = "vmf"

    # Source maps
    static_props: bool = True
    cubemaps: bool = True
    displacements: bool = True
    displacement_thickness: float = 8.0
    func_detail: bool = True  # False puts detail brushes into the world
    overlays: bool = True
    occluders: bool = True
    restore_lights: bool = True
    fix_material_names: bool = True  # undo cubemap-patched "maps/<map>/..." names
    extract_pakfile: bool = True
    extract_generated_files: bool = False  # also extract cubemaps / patched materials

    # GoldSrc / Quake maps
    clip_brushes: bool = True
    merge_brushes: bool = True
    export_wad: bool = True  # textures embedded in the BSP -> <map>_embedded.wad
    bundle_wads: bool = True  # copy the external WADs the map uses -> <map>_wads/

    @property
    def nodraw_material(self) -> str:
        return "TOOLS/TOOLSNODRAW" if self.format == "vmf" else "NULL"
