"""Command line interface: ``python -m bsp2vmf`` or the ``bsp2vmf`` script."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional, Sequence

from . import ConversionError, Options, __version__, convert

log = logging.getLogger("bsp2vmf")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bsp2vmf",
        description="Decompile GoldSrc, Quake and Source BSP maps, or convert Quake-family .map "
                    "files, into VMF or Valve 220 MAP.")
    parser.add_argument("inputs", nargs="*", help=".bsp or .map files, or folders of .bsp files")
    parser.add_argument("-o", "--output", help="output file (single input) or folder")
    parser.add_argument("-f", "--format", choices=("vmf", "map"), default="vmf",
                        help="output format (default: vmf)")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    parser.add_argument("--gui", action="store_true", help="pick files in a dialog")
    parser.add_argument("--version", action="version", version=f"bsp2vmf {__version__}")

    source = parser.add_argument_group("Source maps")
    source.add_argument("--no-props", action="store_true", help="don't restore prop_static")
    source.add_argument("--no-cubemaps", action="store_true", help="don't restore env_cubemap")
    source.add_argument("--no-disp", action="store_true", help="don't rebuild displacements")
    source.add_argument("--disp-thickness", type=float, default=8.0, metavar="UNITS",
                        help="depth of the brushes created under displacements (default: 8)")
    source.add_argument("--no-detail", action="store_true",
                        help="put func_detail brushes into the world")
    source.add_argument("--no-overlays", action="store_true", help="don't restore info_overlay")
    source.add_argument("--no-occluders", action="store_true", help="don't rebuild func_occluder")
    source.add_argument("--no-lights", action="store_true",
                        help="don't restore lights the compiler removed")
    source.add_argument("--keep-patched-names", action="store_true",
                        help="keep cubemap-patched material names (maps/<map>/..._x_y_z)")
    source.add_argument("--no-pak", action="store_true",
                        help="don't extract the files packed into the BSP")
    source.add_argument("--pak-all", action="store_true",
                        help="also extract compiler-generated cubemaps and patched materials")

    goldsrc = parser.add_argument_group("GoldSrc / Quake maps")
    goldsrc.add_argument("--no-clip", action="store_true",
                         help="don't recover CLIP brushes from the collision hull")
    goldsrc.add_argument("--no-merge", action="store_true",
                         help="keep one brush per BSP leaf instead of merging them")
    goldsrc.add_argument("--no-wad", action="store_true",
                         help="don't export embedded textures to <map>_embedded.wad")
    goldsrc.add_argument("--no-bundle-wads", action="store_true",
                         help="don't copy the WADs the map uses into <map>_wads/")
    return parser


def options_from_args(args: argparse.Namespace) -> Options:
    return Options(
        format=args.format,
        static_props=not args.no_props,
        cubemaps=not args.no_cubemaps,
        displacements=not args.no_disp,
        displacement_thickness=args.disp_thickness,
        func_detail=not args.no_detail,
        overlays=not args.no_overlays,
        occluders=not args.no_occluders,
        restore_lights=not args.no_lights,
        fix_material_names=not args.keep_patched_names,
        extract_pakfile=not args.no_pak,
        extract_generated_files=args.pak_all,
        clip_brushes=not args.no_clip,
        merge_brushes=not args.no_merge,
        export_wad=not args.no_wad,
        bundle_wads=not args.no_bundle_wads,
    )


def collect_inputs(paths: Sequence[str]) -> List[str]:
    files = []
    for path in paths:
        if os.path.isdir(path):
            files.extend(os.path.join(path, name) for name in sorted(os.listdir(path))
                         if name.lower().endswith(".bsp"))
        else:
            files.append(path)
    return files


def output_path_for(bsp_path: str, output: Optional[str], file_count: int, fmt: str) -> Optional[str]:
    """Where to write ``bsp_path``; None means next to the input."""
    if not output:
        return None
    if file_count == 1 and not os.path.isdir(output) and output.lower().endswith((".vmf", ".map")):
        return output
    os.makedirs(output, exist_ok=True)
    return os.path.join(output, os.path.splitext(os.path.basename(bsp_path))[0] + "." + fmt)


def convert_all(files: Sequence[str], output: Optional[str], options: Options) -> int:
    failures = 0
    for path in files:
        log.info("%s", os.path.basename(path))
        try:
            convert(path, output_path_for(path, output, len(files), options.format), options)
        except (OSError, ConversionError) as error:
            log.error("cannot convert %s: %s", path, error)
            failures += 1
        except Exception:  # keep going with the remaining maps
            log.exception("failed to convert %s", path)
            failures += 1
    return 1 if failures else 0


def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()
    files = filedialog.askopenfilenames(title="Select maps to convert",
                                        filetypes=[("Maps", "*.bsp *.map"), ("All files", "*.*")])
    if not files:
        return 0
    as_map = messagebox.askyesno(
        "Output format",
        "Write Valve 220 .map files (Hammer 3.5 / J.A.C.K.) instead of .vmf?")
    options = Options(format="map" if as_map else "vmf")
    done, failed = [], []
    for path in files:
        try:
            done.append(convert(path, options=options))
        except (OSError, ConversionError) as error:
            failed.append(f"{os.path.basename(path)}: {error}")
    message = f"Converted {len(done)} file(s)."
    if done:
        message += "\n\n" + "\n".join(done)
    if failed:
        message += "\n\nFailed:\n" + "\n".join(failed)
    messagebox.showinfo("bsp2vmf", message)
    return 1 if failed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR if args.quiet else logging.INFO,
                        format="%(message)s")
    if args.gui or not args.inputs:
        try:
            return run_gui()
        except ImportError:  # Python built without tkinter
            parser.print_help()
            return 2
    return convert_all(collect_inputs(args.inputs), args.output, options_from_args(args))


if __name__ == "__main__":
    sys.exit(main())
