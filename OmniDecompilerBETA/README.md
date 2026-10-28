# OmniDecompiler

A map decompiler and converter for modders, level designers and archivists. It turns compiled
`.bsp` maps back into editable Hammer maps, converts Quake-family `.map` files, and collects the
textures a map needs. One tool for GoldSrc, Quake and Source engine games, with no dependencies
beyond Python 3.8+.

| Input | Games |
|---|---|
| Source `.bsp` (v17–21) | Half-Life 2 + episodes, Portal, Portal 2, CS:S, CS:GO, TF2, L4D/L4D2, Garry's Mod… |
| GoldSrc `.bsp` (v30) | Half-Life, Counter-Strike 1.6, Opposing Force, Blue Shift, Sven Co-op… |
| Quake `.bsp` (v29) | Quake |
| `.map` (Valve 220 or Quake standard) | GoldSrc Hammer, J.A.C.K., TrenchBroom, Quake / Quake 2 / Quake 3 editors |

Output is a `.vmf` for Source Hammer / Hammer++, or a Valve 220 `.map` for GoldSrc Hammer,
J.A.C.K. and TrenchBroom.

## Quick start (Windows)

1. Install [Python 3](https://www.python.org/downloads/) and tick **Add python.exe to PATH**.
2. Double-click **`OmniDecompiler.bat`** and pick your maps, or drag `.bsp` / `.map` files onto it.

The converted maps are saved next to the originals. New here? The **[Getting Started guide](GUIDE.md)**
walks through installing, converting and opening the result in Hammer.

## Install as a command

```bash
pip install .
```

This installs the `bsp2vmf` command. From a checkout you can also run `python -m bsp2vmf`.

## Usage

```bash
bsp2vmf de_dust2.bsp                  # writes de_dust2.vmf next to the map
bsp2vmf maps/ -o decompiled/          # every .bsp in a folder
bsp2vmf crossfire.bsp -f map          # Valve 220 .map for GoldSrc editors
bsp2vmf e1m1.map                      # Quake / Valve 220 .map -> .vmf
bsp2vmf                               # pick files in a dialog
```

`bsp2vmf --help` lists every option; the main ones are below.

| Option | Effect |
|---|---|
| `-f map` | Write Valve 220 `.map` instead of `.vmf` |
| `--no-pak` | Don't extract the files packed into a Source map |
| `--no-props`, `--no-disp`, `--no-overlays`, `--no-lights` | Skip restoring that kind of object |
| `--no-detail` | Put `func_detail` brushes into the world |
| `--no-clip` | GoldSrc: don't recover CLIP brushes |
| `--no-merge` | GoldSrc: keep one brush per BSP leaf |
| `--no-wad`, `--no-bundle-wads` | GoldSrc: don't export embedded textures / copy the map's WADs |

It can also be used as a library:

```python
from bsp2vmf import Options, convert

convert("cs_office.bsp", "out/cs_office.vmf", Options(static_props=False))
```

## What gets restored

**Source maps** keep their original brushes, so geometry comes back exactly. On top of that,
everything the compiler moved out of the entity list is rebuilt:

- `func_detail`, brush entities (placed relative to their origin) and area portals
- displacements, as brushes carrying their `dispinfo`
- `prop_static`, `env_cubemap` and `info_overlay` (attached to the right brush faces)
- `func_occluder` brushes, and `light` / `light_spot` entities the compiler removed
- I/O connections, visgroups per kind of object, and the files packed into the map
  (custom materials, models, sounds) in `<map>_pakfile/`
- material names the compiler rewrote for cubemaps (`maps/<map>/…_x_y_z`) are restored

**GoldSrc and Quake maps** no longer contain brushes, so they are rebuilt from the BSP tree.
Each solid, liquid or sky leaf becomes a brush, split wherever one side carries several
textures, textured and aligned from the faces lying on it, then merged back into larger
brushes. CLIP brushes are recovered from the player collision hull. Tool textures (`NULL`,
`CLIP`, `SKY`, `AAATRIGGER`…) become their Source equivalents in `.vmf` output.

**Textures.** Textures embedded in a GoldSrc map are written to `<map>_embedded.wad`, and the
external WADs listed in its `wad` key are looked up in the game folders (the mod, its base mod,
and `valve`) and copied to `<map>_wads/`.

## Large maps

Maps are memory-mapped, so only the parts being read are loaded and packed files are unzipped
straight from the map file. Decompiling CS:GO's `de_ancient` port (678 MB, 2,256 displacements,
3,027 props, 9,484 packed files) takes about 7 seconds with a peak of about 220 MB of RAM.

## Accuracy

The test suite rebuilds every brush from the planes written to the VMF and checks that the
map's rendered faces lie on outward-facing brush sides. On the maps it was developed against
(HL2 `background01`, `gm_construct`, three CS:S maps up to 678 MB and a CS 1.6 map) every
sampled face is covered, and all GoldSrc faces carry their original texture. A `.bsp` → `.map`
→ `.vmf` round trip reproduces the direct `.vmf` exactly.

Known limits:

- GoldSrc/Quake brushes are rebuilt, not recovered: they look and collide the same but are
  cut differently from the original, and the space outside the map is filled with brushes.
- Recovered clip brushes can be shorter where they touch floors or walls; collision is
  unchanged.
- `.map` patches (`patchDef2`) and Doom 3 `brushDef3` blocks are skipped.
- Titanfall/Apex (`rBSP`), Quake 2/3 `.bsp` (`IBSP`) and console BSPs are not supported.

## Development

```bash
python -m unittest discover -s tests -t .
```

Tests that need real game maps run when `BSP2VMF_TEST_MAPS` points at a folder of `.bsp`
files. Game maps belong to their authors and are not included in the repository.

| Module | Contents |
|---|---|
| `geometry.py` | windings, brush faces from planes, convex polyhedra |
| `mapdata.py` | entities, brushes, sides, texture axes, entity lump parsing |
| `brushes.py` | building and merging brushes |
| `goldsrc.py` | GoldSrc / Quake decompiler |
| `source_bsp.py`, `source.py`, `source_features.py` | Source reader, decompiler, and restored objects |
| `mapfile.py` | `.map` reader |
| `wads.py` | finding and copying a map's WAD files |
| `writers.py` | VMF and Valve 220 MAP output |
