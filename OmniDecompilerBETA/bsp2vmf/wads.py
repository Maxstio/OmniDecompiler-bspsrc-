"""Collecting the WAD texture files a GoldSrc map uses."""

from __future__ import annotations

import logging
import os
import re
import shutil
from typing import Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

# Mod folders whose base content lives in a sibling folder, e.g. cstrike_downloads -> cstrike.
_MOD_SUFFIXES = ("_downloads", "_addon", "_hd", "_lv")


def wad_names(wad_key: str) -> List[str]:
    """File names listed in a worldspawn "wad" key (paths are often absolute)."""
    names = []
    for entry in wad_key.split(";"):
        name = re.split(r"[\\/]", entry.strip())[-1]
        if name.lower().endswith(".wad") and name.lower() not in (n.lower() for n in names):
            names.append(name)
    return names


def search_folders(bsp_path: str) -> List[str]:
    """Game folders that may hold a map's WADs.

    For ``<game>/<mod>/maps/x.bsp`` that is the mod folder, its base mod (for
    ``*_downloads`` style folders) and ``valve``, which holds halflife.wad.
    """
    maps_dir = os.path.dirname(os.path.abspath(bsp_path))
    mod_dir = os.path.dirname(maps_dir) if os.path.basename(maps_dir).lower() == "maps" else maps_dir
    game_dir = os.path.dirname(mod_dir)
    mod = os.path.basename(mod_dir)

    folders = [maps_dir, mod_dir]
    for suffix in _MOD_SUFFIXES:
        if mod.lower().endswith(suffix):
            folders.append(os.path.join(game_dir, mod[:-len(suffix)]))
    folders.append(os.path.join(game_dir, "valve"))
    unique = []
    for folder in folders:
        if os.path.isdir(folder) and os.path.normcase(folder) not in map(os.path.normcase, unique):
            unique.append(folder)
    return unique


def find_wad(name: str, folders: Iterable[str]) -> Optional[str]:
    for folder in folders:
        for entry in os.listdir(folder):
            if entry.lower() == name.lower():
                path = os.path.join(folder, entry)
                if os.path.isfile(path):
                    return path
    return None


def bundle_wads(wad_key: str, bsp_path: str, out_dir: str,
                exclude: Iterable[str] = ()) -> Tuple[List[str], List[str]]:
    """Copy the WADs named in ``wad_key`` (except ``exclude``) into ``out_dir``.

    Returns ``(copied paths, names that could not be found)``.
    """
    folders = search_folders(bsp_path)
    skip = {name.lower() for name in exclude}
    copied, missing = [], []
    for name in wad_names(wad_key):
        if name.lower() in skip:
            continue
        source = find_wad(name, folders)
        if source is None:
            missing.append(name)
            continue
        os.makedirs(out_dir, exist_ok=True)
        destination = os.path.join(out_dir, os.path.basename(source))
        if not os.path.exists(destination):
            shutil.copy2(source, destination)
        copied.append(destination)
    if copied:
        log.info("bundled %d WAD file(s) into %s", len(copied), out_dir)
    if missing:
        log.warning("WAD file(s) not found next to the map: %s", ", ".join(missing))
    return copied, missing
