"""Decompile GoldSrc, Quake and Source engine BSP maps, and convert Quake-family
``.map`` files, into Hammer VMF or Valve 220 MAP.

Example::

    from bsp2vmf import Options, convert

    convert("de_dust2.bsp")                                  # writes de_dust2.vmf
    convert("crossfire.bsp", options=Options(format="map"))  # writes crossfire.map
    convert("e1m1.map")                                      # Quake .map -> e1m1.vmf
"""

from __future__ import annotations

import logging
import mmap
import os
import struct
from typing import List, Optional, Tuple

from .errors import ConversionError, MapParseError, UnsupportedFormatError
from .goldsrc import GoldSrcDecompiler
from .mapdata import Entity, find_worldspawn, source_material
from .mapfile import read_map
from .options import Options
from .source import SourceDecompiler
from .wads import bundle_wads
from .writers import write_map, write_vmf

__all__ = ["ConversionError", "MapParseError", "Options", "UnsupportedFormatError",
           "convert", "decompile", "detect_format", "read_map", "__version__"]
__version__ = "1.1.0"

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())  # silent as a library unless the app configures logging


def detect_format(data: bytes) -> Tuple[str, int]:
    """Return ``(engine, version)`` where engine is "source", "goldsrc" or "quake"."""
    if len(data) < 8:
        raise UnsupportedFormatError("file too small to be a BSP")
    ident = data[:4]
    if ident == b"VBSP":
        return "source", struct.unpack_from("<i", data, 4)[0]
    if ident in (b"rBSP", b"PSBV"):
        raise UnsupportedFormatError("Titanfall/Apex and console (big-endian) BSPs are not supported")
    if ident == b"IBSP":
        raise UnsupportedFormatError("Quake 2 / Quake 3 BSPs are not supported")
    version = struct.unpack_from("<i", data, 0)[0]
    if version == 30:
        return "goldsrc", 30
    if version == 29:
        return "quake", 29
    raise UnsupportedFormatError(f"unknown BSP format (ident {ident!r})")


def decompile(data: bytes, options: Optional[Options] = None,
              pakfile_dir: Optional[str] = None,
              wad_path: Optional[str] = None) -> List[Entity]:
    """Decompile BSP bytes into entities with brushes.

    ``pakfile_dir`` (Source) receives the files packed into the map;
    ``wad_path`` (GoldSrc) receives its embedded textures.
    """
    options = options or Options()
    engine, version = detect_format(data)
    if engine == "source":
        log.info("Source VBSP v%d", version)
        return SourceDecompiler(data, version, options).run(pakfile_dir)
    log.info("%s BSP v%d", "GoldSrc" if engine == "goldsrc" else "Quake", version)
    return GoldSrcDecompiler(data, version, options).run(wad_path)


def _decompile_file(bsp_path: str, stem: str, options: Options) -> List[Entity]:
    with open(bsp_path, "rb") as f:
        # Memory-map the map so only the lumps actually used are loaded;
        # Source maps can be hundreds of megabytes, mostly packed files.
        try:
            data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:  # empty file
            data = f.read()
        try:
            return decompile(
                data, options,
                pakfile_dir=stem + "_pakfile" if options.extract_pakfile else None,
                wad_path=stem + "_embedded.wad" if options.export_wad else None,
            )
        finally:
            if isinstance(data, mmap.mmap):
                data.close()


def convert(input_path: str, output_path: Optional[str] = None,
            options: Optional[Options] = None) -> str:
    """Convert a ``.bsp`` or ``.map`` file and write the result; returns its path.

    The output defaults to the input path with a .vmf/.map extension. For
    BSPs, packed files go to ``<output>_pakfile/``, embedded GoldSrc textures
    to ``<output>_embedded.wad`` and the external WADs the map uses to
    ``<output>_wads/``, unless disabled in ``options``.
    """
    options = options or Options()
    if output_path is None:
        output_path = os.path.splitext(input_path)[0] + "." + options.format
    if os.path.abspath(output_path) == os.path.abspath(input_path):
        raise ConversionError(f"refusing to overwrite the input file {input_path}")
    stem = os.path.splitext(output_path)[0]

    if input_path.lower().endswith(".map"):
        with open(input_path, encoding="latin-1") as f:
            entities = read_map(f.read())
        find_worldspawn(entities)
        if options.format == "vmf":
            for entity in entities:
                for brush in entity.brushes:
                    for side in brush.sides:
                        side.material = source_material(side.material)
    else:
        entities = _decompile_file(input_path, stem, options)
        world = find_worldspawn(entities)
        if options.bundle_wads and world.get("wad"):
            bundle_wads(world.get("wad"), input_path, stem + "_wads",
                        exclude=[os.path.basename(stem + "_embedded.wad")])

    if options.format == "vmf":
        write_vmf(entities, output_path)
    else:
        write_map(entities, output_path)
    brush_count = sum(len(e.brushes) for e in entities)
    log.info("-> %s (%d entities, %d brushes)", output_path, len(entities), brush_count)
    return output_path
