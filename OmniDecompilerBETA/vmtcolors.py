#!/usr/bin/env python3
"""
vmtcolors - extract real material data for the Roblox VMF importer.

For every material used by a .vmf this reads the game's .vmt (from VPK
archives, loose folders, or a bsp2vmf "_pakfile" folder), follows its
$basetexture(s) to the .vtf, and records the values below. GoldSrc maps
(bare texture names) are looked up in WAD3 files and in the textures
embedded in the map's .bsp instead.

    color / color2    average texture colour (sRGB 0-255), color2 = blend layer
    surfaceprop(2)    Source surface property (concrete, metal, dirt, ...)
    shader            e.g. lightmappedgeneric, worldvertextransition, water
    translucent       $translucent / $alphatest / average alpha
    compile flags     %compilewater, %compilenodraw, %compileclip, ...

Output is a JSON file the Roblox plugin loads with "Load Material Data".

Usage
  python vmtcolors.py map.vmf --game "C:/.../Half-Life 2/hl2"
  python vmtcolors.py map.vmf --game ".../garrysmod" --game ".../hl2" --pak map_pakfile
  python vmtcolors.py map.vmf          (auto-detects Steam games + map_pakfile)

Only the Python standard library is needed.
"""

import argparse
import glob
import json
import os
import re
import struct
import sys

__version__ = "1.0"

# --------------------------------------------------------------------------
# file sources: VPK archives and loose folders
# --------------------------------------------------------------------------


class VPK(object):
    def __init__(self, dir_path):
        self.dir_path = dir_path
        self.base = dir_path[:-len("_dir.vpk")]
        self.entries = {}
        with open(dir_path, "rb") as f:
            data = f.read()
        sig, ver = struct.unpack_from("<II", data, 0)
        if sig != 0x55AA1234:
            raise ValueError("not a VPK")
        tree_size = struct.unpack_from("<I", data, 8)[0]
        header = 12 if ver == 1 else 28
        self.data_start = header + tree_size
        pos = header

        def rstr():
            nonlocal pos
            end = data.index(b"\0", pos)
            s = data[pos:end].decode("latin-1")
            pos = end + 1
            return s

        while True:
            ext = rstr()
            if not ext:
                break
            while True:
                path = rstr()
                if not path:
                    break
                while True:
                    name = rstr()
                    if not name:
                        break
                    _crc, preload, arch, ofs, ln, _term = struct.unpack_from("<IHHIIH", data, pos)
                    pos += 18
                    pre = data[pos:pos + preload]
                    pos += preload
                    full = ((path + "/") if path.strip() else "") + name + "." + ext
                    self.entries[full.lower()] = (pre, arch, ofs, ln)
        self._dir_data = data

    def read(self, path):
        e = self.entries.get(path)
        if e is None:
            return None
        pre, arch, ofs, ln = e
        if ln == 0:
            return pre
        if arch == 0x7FFF:
            return pre + self._dir_data[self.data_start + ofs:self.data_start + ofs + ln]
        fn = "%s_%03d.vpk" % (self.base, arch)
        try:
            with open(fn, "rb") as f:
                f.seek(ofs)
                return pre + f.read(ln)
        except OSError:
            return None


class FileSystem(object):
    """Search path of loose folders and VPKs, first match wins."""

    def __init__(self):
        self.sources = []

    def add_folder(self, folder):
        if os.path.isdir(folder):
            self.sources.append(("dir", folder))

    def add_game(self, game_dir):
        self.add_folder(game_dir)
        for vpk in sorted(glob.glob(os.path.join(game_dir, "*_dir.vpk"))):
            try:
                self.sources.append(("vpk", VPK(vpk)))
            except Exception as ex:
                print("  skipping %s (%s)" % (vpk, ex))

    def read(self, rel):
        rel = rel.replace("\\", "/").lower().lstrip("/")
        for kind, src in self.sources:
            if kind == "dir":
                p = os.path.join(src, *rel.split("/"))
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        return f.read()
            else:
                d = src.read(rel)
                if d is not None:
                    return d
        return None


# --------------------------------------------------------------------------
# VMT (KeyValues)
# --------------------------------------------------------------------------

_TOK = re.compile(r'"([^"]*)"|(\{)|(\})|(//[^\n]*)|([^\s{}"]+)')


def parse_keyvalues(text):
    """Returns (shader_name, dict) with lower-cased keys; nested blocks are dicts."""
    tokens = []
    for m in _TOK.finditer(text):
        if m.group(4):
            continue
        if m.group(2):
            tokens.append("{")
        elif m.group(3):
            tokens.append("}")
        else:
            tokens.append(("s", m.group(1) if m.group(1) is not None else m.group(5)))
    pos = 0

    def block():
        nonlocal pos
        out = {}
        while pos < len(tokens):
            t = tokens[pos]
            if t == "}":
                pos += 1
                return out
            if t == "{":
                pos += 1
                block()
                continue
            key = t[1].lower()
            pos += 1
            if pos >= len(tokens):
                break
            v = tokens[pos]
            if v == "{":
                pos += 1
                out[key] = block()
            elif v == "}":
                out[key] = ""
            else:
                out[key] = v[1]
                pos += 1
        return out

    if not tokens or tokens[0] in ("{", "}"):
        return "", {}
    shader = tokens[0][1].lower()
    pos = 1
    if pos < len(tokens) and tokens[pos] == "{":
        pos += 1
        return shader, block()
    return shader, {}


def load_vmt(fs, name, depth=0):
    data = fs.read("materials/%s.vmt" % name)
    if data is None or depth > 4:
        return None, None
    shader, kv = parse_keyvalues(data.decode("latin-1", "replace"))
    if shader == "patch":
        inc = kv.get("include", "")
        inc = inc.replace("\\", "/").lower()
        if inc.startswith("materials/"):
            inc = inc[len("materials/"):]
        if inc.endswith(".vmt"):
            inc = inc[:-4]
        bshader, base = load_vmt(fs, inc, depth + 1)
        if base is None:
            base = {}
        base = dict(base)
        for blk in ("insert", "replace"):
            if isinstance(kv.get(blk), dict):
                base.update(kv[blk])
        return bshader, base
    return shader, kv


# --------------------------------------------------------------------------
# VTF -> average colour
# --------------------------------------------------------------------------

FMT_BPP = {0: 4, 1: 4, 2: 3, 3: 3, 4: 2, 5: 1, 6: 2, 8: 1, 9: 3, 10: 3, 11: 4, 12: 4,
           16: 4, 17: 2, 18: 2, 19: 2, 21: 2, 22: 2, 23: 4, 24: 8, 25: 8, 26: 4}
DXT = {13: 8, 20: 8, 14: 16, 15: 16}


def img_size(fmt, w, h):
    if fmt in DXT:
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * DXT[fmt]
    return w * h * FMT_BPP.get(fmt, 0)


def _565(c):
    return (((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31)


def decode_avg(fmt, w, h, buf):
    """Average (r, g, b, a) of an image, or None for unsupported formats."""
    tr = tg = tb = ta = 0.0
    n = 0
    if fmt in DXT:
        bs = DXT[fmt]
        blocks = max(1, (w + 3) // 4) * max(1, (h + 3) // 4)
        if len(buf) < blocks * bs:
            return None
        for b in range(blocks):
            o = b * bs
            alphas = None
            if fmt == 15:   # DXT5 interpolated alpha
                a0, a1 = buf[o], buf[o + 1]
                bits = int.from_bytes(buf[o + 2:o + 8], "little")
                pal = [a0, a1]
                if a0 > a1:
                    pal += [((6 - i) * a0 + (1 + i) * a1) // 7 for i in range(6)]
                else:
                    pal += [((4 - i) * a0 + (1 + i) * a1) // 5 for i in range(4)] + [0, 255]
                alphas = [pal[(bits >> (3 * i)) & 7] for i in range(16)]
                o += 8
            elif fmt == 14:  # DXT3 explicit alpha
                bits = int.from_bytes(buf[o:o + 8], "little")
                alphas = [((bits >> (4 * i)) & 15) * 17 for i in range(16)]
                o += 8
            c0, c1, idx = struct.unpack_from("<HHI", buf, o)
            p0, p1 = _565(c0), _565(c1)
            if c0 > c1 or fmt in (14, 15):
                cols = [p0, p1,
                        tuple((2 * a + b) // 3 for a, b in zip(p0, p1)),
                        tuple((a + 2 * b) // 3 for a, b in zip(p0, p1))]
                ca = [255, 255, 255, 255]
            else:
                cols = [p0, p1, tuple((a + b) // 2 for a, b in zip(p0, p1)), (0, 0, 0)]
                ca = [255, 255, 255, 0]
            for i in range(16):
                k = (idx >> (2 * i)) & 3
                c = cols[k]
                a = alphas[i] if alphas else ca[k]
                tr += c[0]
                tg += c[1]
                tb += c[2]
                ta += a
                n += 1
        return tr / n, tg / n, tb / n, ta / n
    bpp = FMT_BPP.get(fmt)
    if not bpp or len(buf) < w * h * bpp:
        return None
    order = {0: "rgba", 1: "abgr", 2: "rgb", 3: "bgr", 5: "i", 6: "ia", 11: "argb",
             12: "bgra", 16: "bgrx", 9: "rgb", 10: "bgr"}.get(fmt)
    if order is None:
        return None
    for i in range(w * h):
        px = buf[i * bpp:(i + 1) * bpp]
        d = dict(zip(order, px))
        if "i" in d:
            r = g = b = d["i"]
        else:
            r, g, b = d["r"], d["g"], d["b"]
        a = d.get("a", 255)
        tr += r
        tg += g
        tb += b
        ta += a
        n += 1
    return tr / n, tg / n, tb / n, ta / n


def vtf_average(data):
    if not data or data[:4] != b"VTF\0" or len(data) < 64:
        return None
    _maj, minor, hsize = struct.unpack_from("<III", data, 4)
    w, h = struct.unpack_from("<HH", data, 16)
    flags = struct.unpack_from("<I", data, 20)[0]
    frames = struct.unpack_from("<H", data, 24)[0]
    refl = struct.unpack_from("<3f", data, 32)
    hi_fmt = struct.unpack_from("<i", data, 52)[0]
    mips = data[56]
    lo_fmt = struct.unpack_from("<i", data, 57)[0]
    lo_w, lo_h = data[61], data[62]
    lo_ofs = hi_ofs = None
    if minor >= 3 and len(data) >= 80:
        nres = struct.unpack_from("<I", data, 68)[0]
        for i in range(min(nres, 32)):
            tag = data[80 + i * 8:83 + i * 8]
            ofs = struct.unpack_from("<I", data, 84 + i * 8)[0]
            if tag == b"\x01\x00\x00":
                lo_ofs = ofs
            elif tag == b"\x30\x00\x00":
                hi_ofs = ofs
    else:
        lo_ofs = hsize
        hi_ofs = hsize + (img_size(lo_fmt, lo_w, lo_h) if lo_fmt >= 0 else 0)
    # 1) smallest mip of the full image that is >= 4x4 (true colours incl. alpha)
    if hi_ofs is not None and hi_fmt >= 0:
        sizes = [(max(1, w >> m), max(1, h >> m)) for m in range(mips)]
        # stored smallest mip first; pick a 4..16 px level
        ofs = hi_ofs
        faces = 6 if flags & 0x4000 else 1
        for m in range(mips - 1, -1, -1):
            mw, mh = sizes[m]
            sz = img_size(hi_fmt, mw, mh)
            if (mw >= 4 and mh >= 4) or m == 0:
                res = decode_avg(hi_fmt, mw, mh, data[ofs:ofs + sz])
                if res:
                    return res, True
                break
            ofs += sz * max(1, frames) * faces
    # 2) the low-res thumbnail (always DXT1, no alpha)
    if lo_ofs is not None and lo_fmt >= 0 and lo_w and lo_h:
        res = decode_avg(lo_fmt, lo_w, lo_h, data[lo_ofs:lo_ofs + img_size(lo_fmt, lo_w, lo_h)])
        if res:
            return res, False
    # 3) the reflectivity vector (linear)
    if any(refl):
        return tuple(255.0 * max(0.0, min(1.0, c)) ** (1 / 2.2) for c in refl) + (255.0,), False
    return None


# --------------------------------------------------------------------------
# GoldSrc / Quake textures (WAD3 files and miptex embedded in a BSP)
# --------------------------------------------------------------------------

def miptex_average(blob):
    """Average colour of a GoldSrc miptex (header + 4 mips + palette).
    Returns ((r, g, b), coverage) or None. Palette index 255 is the
    transparent colour of '{' textures and is left out of the average."""
    if len(blob) < 40:
        return None
    w, h = struct.unpack_from("<II", blob, 16)
    ofs = struct.unpack_from("<4I", blob, 24)
    if not w or not h or w * h > (1 << 24) or ofs[0] == 0:
        return None
    mw, mh = max(1, w // 8), max(1, h // 8)
    pal_ofs = ofs[3] + mw * mh + 2
    if pal_ofs + 768 > len(blob) or ofs[3] + mw * mh > len(blob):
        return None
    pal = blob[pal_ofs:pal_ofs + 768]
    pixels = blob[ofs[3]:ofs[3] + mw * mh]
    hist = [0] * 256
    for p in pixels:
        hist[p] += 1
    masked = hist[255] if pal[765:768] == b"\x00\x00\xff" else 0
    total = sum(hist) - masked
    if total <= 0:
        return None
    r = g = b = 0
    for i, n in enumerate(hist):
        if n and not (masked and i == 255):
            r += pal[i * 3] * n
            g += pal[i * 3 + 1] * n
            b += pal[i * 3 + 2] * n
    return (r / total, g / total, b / total), total / float(len(pixels))


class GoldSrcTextures(object):
    """Name -> average colour for textures from WAD3 files and BSP miptex lumps.
    Sources added first win, like the GoldSrc engine's wad search order."""

    def __init__(self):
        self.textures = {}

    def __len__(self):
        return len(self.textures)

    def _add(self, name, blob):
        name = name.lower()
        if name in self.textures:
            return
        res = miptex_average(blob)
        if res:
            self.textures[name] = res

    def add_wad(self, path):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return 0
        if data[:4] not in (b"WAD3", b"WAD2"):
            return 0
        count, dir_ofs = struct.unpack_from("<ii", data, 4)
        before = len(self.textures)
        for i in range(count):
            pos, disk, size, typ, comp, _pad, name = struct.unpack_from("<iiibbh16s", data, dir_ofs + 32 * i)
            if typ in (0x43, 0x44) and comp == 0:
                self._add(name.split(b"\0")[0].decode("latin-1"), data[pos:pos + disk])
        return len(self.textures) - before

    def add_bsp(self, path):
        """Textures embedded in a GoldSrc (v30) or Quake (v29) BSP."""
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return 0
        if len(data) < 124 or struct.unpack_from("<i", data, 0)[0] not in (29, 30):
            return 0
        # lump 2 = textures (Blue Shift only swaps lumps 0 and 1)
        ofs, ln = struct.unpack_from("<ii", data, 4 + 2 * 8)
        lump = data[ofs:ofs + ln]
        if len(lump) < 4:
            return 0
        before = len(self.textures)
        n = struct.unpack_from("<i", lump, 0)[0]
        for i in range(n):
            o = struct.unpack_from("<i", lump, 4 + 4 * i)[0]
            if 0 <= o < len(lump):
                self._add(lump[o:o + 16].split(b"\0")[0].decode("latin-1"), lump[o:])
        return len(self.textures) - before

    def describe(self, name):
        """Material info for a GoldSrc texture name, or None if unknown."""
        base = name.rsplit("/", 1)[-1].lower()
        info = {"shader": "goldsrc"}
        compile_flags = {
            "null": "nodraw", "skip": "skip", "hint": "hint", "origin": "origin",
            "clip": "clip", "aaatrigger": "trigger", "sky": "sky",
        }
        if base in compile_flags:
            info["compile"] = [compile_flags[base]]
        elif base.startswith("!") or base.startswith("*"):
            info["compile"] = ["water"]
            info["shader"] = "water"
        res = self.textures.get(base)
        if res is None:
            return info if "compile" in info else None
        (r, g, b), coverage = res
        info["color"] = [int(round(r)), int(round(g)), int(round(b))]
        if base.startswith("{"):
            info["translucent"] = True
            info["coverage"] = round(coverage, 3)
        return info


def goldsrc_game_dirs():
    roots = [r"C:\Program Files (x86)\Steam\steamapps\common", r"C:\Program Files\Steam\steamapps\common",
             r"D:\SteamLibrary\steamapps\common", r"E:\SteamLibrary\steamapps\common"]
    mods = ["cstrike_downloads", "cstrike", "czero", "valve_downloads", "valve", "gearbox", "bshift",
            "dod", "tfc", "ricochet", "dmc"]
    out = []
    for r in roots:
        for m in mods:
            p = os.path.join(r, "Half-Life", m)
            if os.path.isdir(p):
                out.append(p)
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def vmf_materials(path):
    with open(path, encoding="latin-1") as f:
        text = f.read()
    names = set()
    for m in re.finditer(r'"material"\s+"([^"]+)"', text, re.I):
        names.add(m.group(1).replace("\\", "/").lower())
    return sorted(names)


def map_wad_names(path):
    """WAD file names listed in the worldspawn "wad" key, in order."""
    with open(path, encoding="latin-1") as f:
        m = re.search(r'"wad"\s+"([^"]*)"', f.read(), re.I)
    if not m:
        return []
    return [w.replace("\\", "/").rsplit("/", 1)[-1] for w in m.group(1).split(";") if w.strip()]


def tex_path(v):
    v = v.replace("\\", "/").lower().strip()
    if v.startswith("materials/"):
        v = v[len("materials/"):]
    if v.endswith(".vtf"):
        v = v[:-4]
    return v


def describe(fs, name, cache):
    shader, kv = load_vmt(fs, name)
    if kv is None:
        return None
    info = {"shader": shader}
    for key, out in (("$surfaceprop", "surfaceprop"), ("$surfaceprop2", "surfaceprop2")):
        if kv.get(key):
            info[out] = kv[key].lower()
    for flag in ("%compilewater", "%compilenodraw", "%compileclip", "%compileplayerclip",
                 "%compilenpcclip", "%compiletrigger", "%compilesky", "%compile2dsky",
                 "%compileskip", "%compilehint", "%compileareaportal", "%compileinvisible",
                 "%compileladder", "%compilenonsolid", "%compiledetail", "%compileorigin",
                 "%compilefog", "%compileclip", "%compilegrenadeclip", "%compilemonsterclip"):
        if str(kv.get(flag, "0")).strip() not in ("0", ""):
            info.setdefault("compile", []).append(flag[8:])
    translucent = kv.get("$translucent", "0") not in ("0", "") or \
        kv.get("$alphatest", "0") not in ("0", "")
    if translucent:
        info["translucent"] = True
    try:
        if kv.get("$alpha"):
            info["alpha"] = round(float(kv["$alpha"]), 3)
    except ValueError:
        pass
    tint = None
    m = re.match(r"[\[{]\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)", kv.get("$color", "") or kv.get("$color2", ""))
    if m:
        vals = [float(x) for x in m.groups()]
        if kv.get("$color", "").startswith("{") or max(vals) > 1.0:
            vals = [x / 255.0 for x in vals]
        tint = vals
    for key, out in (("$basetexture", "color"), ("$basetexture2", "color2")):
        tp = kv.get(key)
        if not tp:
            continue
        tp = tex_path(tp)
        if tp not in cache:
            cache[tp] = vtf_average(fs.read("materials/%s.vtf" % tp))
        res = cache[tp]
        if res:
            (r, g, b, a), has_alpha = res
            if tint and out == "color":
                r, g, b = r * tint[0], g * tint[1], b * tint[2]
            info[out] = [int(round(r)), int(round(g)), int(round(b))]
            if out == "color" and has_alpha and translucent:
                info["coverage"] = round(a / 255.0, 3)
    if "color" not in info and shader == "water":
        fog = re.match(r"[\[{]\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)", kv.get("$fogcolor", ""))
        if fog:
            vals = [float(x) for x in fog.groups()]
            if max(vals) <= 1.0:
                vals = [x * 255 for x in vals]
            info["color"] = [int(v) for v in vals]
    return info


def steam_game_dirs():
    roots = [r"C:\Program Files (x86)\Steam\steamapps\common", r"C:\Program Files\Steam\steamapps\common",
             r"D:\SteamLibrary\steamapps\common", r"E:\SteamLibrary\steamapps\common"]
    known = ["Half-Life 2/hl2", "Half-Life 2/episodic", "Half-Life 2/ep2", "Half-Life 2/lostcoast",
             "Portal/portal", "Portal 2/portal2", "Counter-Strike Source/cstrike",
             "Counter-Strike Global Offensive/csgo", "Team Fortress 2/tf", "Left 4 Dead 2/left4dead2",
             "GarrysMod/garrysmod", "GarrysMod/sourceengine", "Half-Life 2/hl2_complete"]
    out = []
    for r in roots:
        for k in known:
            p = os.path.join(r, *k.split("/"))
            if os.path.isdir(p):
                out.append(p)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract material colours/types for the Roblox VMF importer.")
    ap.add_argument("vmf", help="the .vmf whose materials should be described")
    ap.add_argument("--game", action="append", default=[],
                    help="game content folder (contains *_dir.vpk / materials), repeatable, first wins")
    ap.add_argument("--pak", action="append", default=[], help="extra loose folder, e.g. <map>_pakfile")
    ap.add_argument("--wad", action="append", default=[],
                    help="GoldSrc .wad file or folder of .wad files, repeatable, first wins")
    ap.add_argument("--bsp", help="GoldSrc .bsp with embedded textures (default: <vmf>.bsp if present)")
    ap.add_argument("-o", "--output", help="output .json (default: <vmf>_materials.json)")
    a = ap.parse_args(argv)

    names = vmf_materials(a.vmf)
    is_goldsrc = sum(1 for n in names if "/" not in n) > len(names) // 2

    fs = FileSystem()
    paks = list(a.pak)
    auto_pak = os.path.splitext(a.vmf)[0] + "_pakfile"
    if not paks and os.path.isdir(auto_pak):
        paks.append(auto_pak)
    for p in paks:
        fs.add_folder(p)
    games = a.game or ([] if is_goldsrc else steam_game_dirs())
    for g in games:
        fs.add_game(g)
    print("search path: %d pak folder(s), %d game folder(s)" % (len(paks), len(games)))
    for g in games:
        print("   ", g)

    # GoldSrc maps use bare texture names ("te_wall1b") that live in WAD files
    goldsrc = GoldSrcTextures()
    if is_goldsrc:
        stem = os.path.splitext(a.vmf)[0]
        bsp = a.bsp or (stem + ".bsp" if os.path.isfile(stem + ".bsp") else None)
        if bsp:
            print("  %d textures embedded in %s" % (goldsrc.add_bsp(bsp), os.path.basename(bsp)))
        wads = []
        for w in a.wad:
            wads += sorted(glob.glob(os.path.join(w, "*.wad"))) if os.path.isdir(w) else [w]
        if os.path.isfile(stem + "_embedded.wad"):
            wads.insert(0, stem + "_embedded.wad")
        mod_dirs = goldsrc_game_dirs()
        listed = map_wad_names(a.vmf)
        for d in mod_dirs:
            wads += [os.path.join(d, w) for w in listed if os.path.isfile(os.path.join(d, w))]
        for d in mod_dirs:
            wads += sorted(glob.glob(os.path.join(d, "*.wad")))
        seen = set()
        for w in wads:
            key = os.path.normcase(os.path.abspath(w))
            if key not in seen:
                seen.add(key)
                goldsrc.add_wad(w)
        print("  %d GoldSrc textures from %d wad file(s)" % (len(goldsrc), len(seen)))

    cache = {}
    out = {}
    missing = []
    for n in names:
        try:
            info = describe(fs, n, cache)
            if info is None and len(goldsrc):
                info = goldsrc.describe(n)
        except Exception as ex:
            info = None
            print("  error reading %s: %s" % (n, ex))
        if info is None:
            missing.append(n)
        else:
            out[n] = info
    with_color = sum(1 for v in out.values() if "color" in v)
    dest = a.output or os.path.splitext(a.vmf)[0] + "_materials.json"
    with open(dest, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "materials": out}, f, indent=1, sort_keys=True)
    print("%d materials in map: %d described (%d with texture colour), %d not found"
          % (len(names), len(out), with_color, len(missing)))
    if missing:
        print("  not found:", ", ".join(missing[:15]) + (" ..." if len(missing) > 15 else ""))
    print("  ->", dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
