"""folder-icons - give every project folder a Material Icon Theme icon in Windows Explorer.

Detects the kind of project in each subfolder of a root (Python, JS, Godot, ...),
picks the matching Material Icon Theme folder icon (or composes one from a file
icon when the theme has no folder for that language), and points the folder at
it through desktop.ini. Per-folder overrides pick icons by meaning. Reversible
with `undo`.

    python folder_icons.py fetch                       # clone the icon set into .material/
    python folder_icons.py preview E:\\Projects        # contact sheet only, no changes
    python folder_icons.py apply   E:\\Projects        # write icons + desktop.ini
    python folder_icons.py undo    E:\\Projects        # remove what apply wrote
    python folder_icons.py icons                       # list available icon names
    python folder_icons.py selftest

Requires Pillow, Node.js and @resvg/resvg-js (`npm install`). Windows only for
apply/undo (desktop.ini is an Explorer feature).
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ICON_SET = HERE / ".material" / "icons"
THEME_TS = HERE / ".material" / "src" / "core" / "icons" / "folderIcons.ts"
# Build output, caches and logs get wiped by tools; a read-only folder makes Python's
# shutil.rmtree fail with "Access is denied", so --deep neither icons nor enters them.
GENERATED = {"folder-dist", "folder-temp", "folder-coverage", "folder-debug", "folder-log",
             "folder-target", "folder-dump", "folder-trash", "folder-node"}
NO_DESCEND = {".git", "node_modules", "venv", ".venv", "__pycache__", ".material",
                               ".godot", ".next", ".cache", "site-packages"}
ICON_REPO = "https://github.com/material-extensions/vscode-material-icon-theme"
MARK = "; written by folder-icons"
MANIFEST = "applied.json"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
             ".next", "target", "bin", "obj", ".godot", ".cache", "vendor"}

# Folder outline shared by every Material folder icon, and its neutral color.
FOLDER_PATH = ("m6.922 3.768-.644-.536A1 1 0 0 0 5.638 3H2a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h12"
               "a1 1 0 0 0 1-1V5a1 1 0 0 0-1-1H7.562a1 1 0 0 1-.64-.232")
FOLDER_GRAY = "#90a4ae"

# kind -> icon spec. "folder-x" is a theme folder icon, "+x" puts file icon x.svg on a plain folder.
KINDS = {
    "python": "folder-python",
    "javascript": "folder-javascript",
    "typescript": "folder-typescript",
    "web": "+html",
    "godot": "folder-godot",
    "csharp": "+csharp",
    "cpp": "+cpp",
    "java": "folder-java",
    "php": "folder-php",
    "rust": "folder-rust",
    "hardware": "+arduino",
    "script": "folder-scripts",
    "sql": "folder-database",
    "docs": "folder-docs",
    "archive": "folder-archive",
    "unknown": "+",
}

# Marker files decide before extension counting does.
MARKERS = [
    ("godot", lambda names: "project.godot" in names),
    ("rust", lambda names: "Cargo.toml" in names),
    ("csharp", lambda names: any(n.endswith((".csproj", ".sln")) for n in names)),
    ("hardware", lambda names: any(n.endswith((".ino", ".kicad_pcb")) or n == "platformio.ini" for n in names)),
    ("typescript", lambda names: "tsconfig.json" in names),
]

EXT_KIND = {
    ".py": "python", ".pyw": "python", ".ipynb": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".html": "web", ".css": "web", ".scss": "web",
    ".gd": "godot", ".gdshader": "godot",
    ".cs": "csharp",
    ".c": "cpp", ".cpp": "cpp", ".cc": "cpp", ".h": "cpp", ".hpp": "cpp",
    ".java": "java", ".kt": "java",
    ".php": "php",
    ".rs": "rust",
    ".ino": "hardware", ".kicad_pcb": "hardware", ".kicad_sch": "hardware",
    ".ps1": "script", ".psm1": "script", ".bat": "script", ".cmd": "script",
    ".vbs": "script", ".sh": "script",
    ".sql": "sql",
}
DOC_EXT = {".md", ".txt", ".pdf", ".docx", ".odt", ".png", ".jpg", ".jpeg"}


# ---------------------------------------------------------------- detection

def scan(folder: Path, max_depth: int = 3, max_files: int = 4000):
    """Return (top-level file names, Counter of kinds, doc file count)."""
    top = {p.name for p in folder.iterdir() if p.is_file()} if folder.is_dir() else set()
    kinds: Counter = Counter()
    docs = 0
    seen = 0
    stack = [(folder, 0)]
    while stack and seen < max_files:
        d, depth = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if depth < max_depth and e.name not in SKIP_DIRS and not e.name.startswith("."):
                    stack.append((Path(e.path), depth + 1))
                continue
            seen += 1
            ext = os.path.splitext(e.name)[1].lower()
            if ext in EXT_KIND:
                kinds[EXT_KIND[ext]] += 1
            elif ext in DOC_EXT:
                docs += 1
    return top, kinds, docs


def detect(folder: Path) -> str:
    if folder.name.startswith("_"):
        return "archive"
    top, kinds, docs = scan(folder)
    for kind, test in MARKERS:
        if test(top):
            return kind
    if "package.json" in top and kinds["typescript"] > kinds["javascript"]:
        return "typescript"
    if "package.json" in top:
        return "javascript"
    # Helper scripts and web assets ride along with most projects, so they only
    # win when nothing heavier is present.
    code = {k: n for k, n in kinds.items() if k not in ("script", "web", "sql")}
    if code:
        return max(code, key=code.get)
    for light in ("web", "sql", "script"):
        if kinds[light]:
            return light
    return "docs" if docs else "unknown"


# ---------------------------------------------------------------- icon specs

_SVG_RE = re.compile(r"<svg\b([^>]*)>(.*)</svg>\s*$", re.S)


def _split_svg(text: str):
    m = _SVG_RE.search(text)
    if not m:
        raise ValueError("not an svg document")
    vb = re.search(r'viewBox="([^"]+)"', m.group(1))
    return (vb.group(1) if vb else "0 0 16 16"), m.group(2)


def icon_svg(spec: str, icon_set: Path = ICON_SET) -> str:
    """Resolve an icon spec to a complete SVG document."""
    if not spec.startswith("+"):
        p = icon_set / f"{spec}.svg"
        if not p.exists():
            raise FileNotFoundError(f"no icon '{spec}' in {icon_set} (see: folder_icons.py icons)")
        return p.read_text(encoding="utf-8")
    badge = ""
    if spec[1:]:
        p = icon_set / f"{spec[1:]}.svg"
        if not p.exists():
            raise FileNotFoundError(f"no file icon '{spec[1:]}' in {icon_set}")
        vb, inner = _split_svg(p.read_text(encoding="utf-8"))
        # same corner the theme's own folder motifs occupy
        badge = f'<svg x="7" y="5.5" width="8.5" height="8.5" viewBox="{vb}">{inner}</svg>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            f'<path fill="{FOLDER_GRAY}" d="{FOLDER_PATH}"/>{badge}</svg>')


def rasterize(svgs: dict[str, str], sizes, workdir: Path) -> dict[tuple[str, int], Path]:
    """Render {key: svg} at every size in one Node run; returns {(key, size): png}."""
    jobs, out = [], {}
    for key, svg in svgs.items():
        slug = hashlib.sha1(key.encode()).hexdigest()[:12]
        for s in sizes:
            png = workdir / f"{slug}-{s}.png"
            jobs.append({"svg": svg, "out": str(png), "size": s})
            out[(key, s)] = png
    if jobs:
        r = subprocess.run(["node", str(HERE / "render.js")], input=json.dumps(jobs),
                           capture_output=True, text=True, cwd=HERE)
        if r.returncode != 0:
            raise RuntimeError(f"render.js failed: {r.stderr.strip()}")
    return out


# ---------------------------------------------------------------- catppuccin
#
# Specs are written in Material's vocabulary ("folder-robot", "+cpp"). Catppuccin
# has fewer folder icons, so a spec is translated: same concept name, its plural
# or singular, then the folder names Material lists for that icon looked up in
# Catppuccin's own map, then a file icon of the concept on a plain folder.

CAT_ROOT = HERE / ".catppuccin"
CAT_REPO = "https://github.com/catppuccin/vscode-icons"
FLAVORS = ("mocha", "macchiato", "frappe", "latte")
CAT_FOLDER_ALIAS = {"secure": "security", "television": "video", "theme": "themes",
                    "keys": "private", "log": "debug", "mobile": "android", "vm": "docker",
                    "desktop": "windows", "simulations": "animation", "syntax": "src",
                    "cart": "packages", "store": "packages", "mail": "messages", "review": "audit"}
CAT_FILE_ALIAS = {"robot": "robots", "claude": "robots", "contract": "license",
                  "prompts": "markdown", "archive": "zip", "backup": "zip", "tasks": "todo"}
SETS = ("material",) + tuple(f"catppuccin-{f}" for f in FLAVORS)


@lru_cache(maxsize=8)
def cat_name_map(ts: Path) -> dict[str, str]:
    """Folder name -> catppuccin icon key, from src/defaults/folderIcons.ts."""
    if not ts.exists():
        return {}
    out: dict[str, str] = {}
    for key, body in re.findall(r"'([\w.-]+)':\s*\{\s*folderNames:\s*\[(.*?)\]", ts.read_text(encoding="utf-8"), re.S):
        for n in re.findall(r"'([^']+)'", body):
            out.setdefault(n.lower(), key)
    return out


def _paths(svg: str) -> list[str]:
    return re.findall(r"<path\b[^>]*/>", svg)


def cat_compose(d: Path, badge: str) -> str:
    """Catppuccin outline folder with a file icon in the corner the theme uses for motifs."""
    if not badge:
        return (d / "_folder.svg").read_text(encoding="utf-8")
    # folder_docs draws the outline already cut around the corner badge; reuse that outline
    outline = _paths((d / "folder_docs.svg").read_text(encoding="utf-8"))[0]
    vb, inner = _split_svg((d / f"{badge}.svg").read_text(encoding="utf-8"))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            f'<g fill="none" stroke-linecap="round" stroke-linejoin="round">{outline}</g>'
            f'<svg x="8" y="8" width="8" height="8" viewBox="{vb}">{inner}</svg></svg>')


def cat_svg(spec: str, flavor: str, cat_root: Path = CAT_ROOT, material_names: dict | None = None) -> str:
    d = cat_root / "icons" / flavor
    if not d.exists():
        raise FileNotFoundError(f"no catppuccin flavor '{flavor}' in {cat_root} (run: folder_icons.py fetch)")
    if spec.startswith("+"):
        badge = spec[1:]
        return cat_compose(d, badge if (d / f"{badge}.svg").exists() else "")
    concept = spec.removeprefix("folder-")
    names = cat_name_map(cat_root / "src" / "defaults" / "folderIcons.ts")
    material_names = name_map() if material_names is None else material_names
    aliases = [n for n, icon in material_names.items() if icon == spec]
    def folder(cands):
        for c in cands:
            if c and (d / f"folder_{c}.svg").exists():
                return (d / f"folder_{c}.svg").read_text(encoding="utf-8")

    def file_badge(cands):
        for c in cands:
            if c and (d / f"{c}.svg").exists():
                return cat_compose(d, c)

    # A file icon of the concept itself (python.svg, typescript.svg) beats the name map:
    # Material files 'types' under typescript, which would land on Catppuccin's tag folder.
    return (folder([CAT_FOLDER_ALIAS.get(concept), concept, concept + "s", concept.rstrip("s")])
            or file_badge([CAT_FILE_ALIAS.get(concept), concept])
            or folder([names.get(n) for n in [concept, *aliases]])
            or cat_compose(d, ""))


# ---------------------------------------------------------------- palette (experimental)
#
# Catppuccin's css-variables flavor draws every icon with var(--vscode-ctp-<color>).
# A palette set fills those variables from an outside palette (livery's six colors):
# the folder outline takes the palette's text color, and each motif color is its
# Mocha reference mixed toward the accent by `strength`. Motifs keep some variety at
# low strength and become one-colored at 1.0.

PALETTE_KEYS = ("bg", "accent", "secondary", "text", "dim", "faint")
_CTP_VAR = re.compile(r"var\(--vscode-ctp-([a-z0-9]+)\)")


@lru_cache(maxsize=8)
def ctp_reference(cat_root: Path = CAT_ROOT) -> dict[str, str]:
    """Catppuccin color name -> Mocha hex, read by pairing the css-variables and mocha
    versions of the same icon (so no palette is copied into this file)."""
    css, mocha = cat_root / "icons" / "css-variables", cat_root / "icons" / "mocha"
    ref: dict[str, str] = {}
    for f in sorted(css.glob("*.svg")):
        m = mocha / f.name
        if not m.exists():
            continue
        names = _CTP_VAR.findall(f.read_text(encoding="utf-8"))
        hexes = re.findall(r'(?:stroke|fill)="(#[0-9a-fA-F]{6})"', m.read_text(encoding="utf-8"))
        if len(names) == len(hexes):
            for n, h in zip(names, hexes):
                ref.setdefault(n, h.lower())
    return ref


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = _hex(a), _hex(b)
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def _hex(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def palette_colors(theme: dict, strength: float, ref: dict[str, str]) -> dict[str, str]:
    out = {}
    for name, hexv in ref.items():
        if name == "text":
            out[name] = theme["text"]
        elif name.startswith(("overlay", "surface", "subtext")):
            out[name] = theme["dim"]
        else:
            out[name] = _mix(hexv, theme["accent"], strength)
    return out


def palette_id(theme: dict, strength: float) -> str:
    key = json.dumps({k: theme[k].lower() for k in PALETTE_KEYS} | {"s": round(strength, 2)}, sort_keys=True)
    return "palette-" + hashlib.sha1(key.encode()).hexdigest()[:10]


def save_palette(icon_dir: Path, theme: dict, strength: float) -> str:
    missing = [k for k in PALETTE_KEYS if k not in theme]
    if missing:
        raise ValueError(f"palette is missing {', '.join(missing)}")
    pid = palette_id(theme, strength)
    d = icon_dir / "palettes"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({"theme": theme, "strength": strength}, indent=2), encoding="utf-8")
    return pid


def palette_svg(spec: str, pid: str, icon_dir: Path, cat_root: Path = CAT_ROOT,
                material_names: dict | None = None) -> str:
    p = json.loads((icon_dir / "palettes" / f"{pid}.json").read_text(encoding="utf-8"))
    colors = palette_colors(p["theme"], p["strength"], dict(ctp_reference(cat_root)))
    svg = cat_svg(spec, "css-variables", cat_root, material_names)
    return _CTP_VAR.sub(lambda m: colors.get(m.group(1), p["theme"]["accent"]), svg)


def resolve_svg(spec: str, icon_set_id: str = "material", icon_set: Path = ICON_SET,
                cat_root: Path = CAT_ROOT, material_names: dict | None = None,
                icon_dir: Path | None = None) -> str:
    if icon_set_id == "material":
        return icon_svg(spec, icon_set)
    if icon_set_id.startswith("palette-"):
        return palette_svg(spec, icon_set_id, icon_dir or HERE / "icons", cat_root, material_names)
    if icon_set_id.startswith("catppuccin-"):
        return cat_svg(spec, icon_set_id.split("-", 1)[1], cat_root, material_names)
    raise ValueError(f"unknown icon set '{icon_set_id}' (choose from: {', '.join(SETS)})")


def write_icos(specs: list[str], icon_dir: Path, icon_set: Path = ICON_SET,
               icon_set_id: str = "material", cat_root: Path = CAT_ROOT):
    """Build one .ico per spec that does not exist yet, in icon_dir/<set>/."""
    out_dir = icon_dir / icon_set_id
    out_dir.mkdir(parents=True, exist_ok=True)
    mnames = name_map(icon_set=icon_set) if icon_set_id != "material" else None
    todo = {s: resolve_svg(s, icon_set_id, icon_set, cat_root, mnames, icon_dir)
            for s in specs if not (out_dir / icon_name(s)).exists()}
    with tempfile.TemporaryDirectory(prefix="folder-icons-") as tmp:
        pngs = rasterize(todo, ICO_SIZES, Path(tmp))
        for spec in todo:
            imgs = [Image.open(pngs[(spec, s)]).convert("RGBA") for s in ICO_SIZES]
            imgs[-1].save(out_dir / icon_name(spec), format="ICO",
                          sizes=[(s, s) for s in ICO_SIZES], append_images=imgs[:-1])


def icon_name(spec: str) -> str:
    return ("compose-" + (spec[1:] or "plain") if spec.startswith("+") else spec) + ".ico"


# ---------------------------------------------------------------- theme name map

def name_map(ts: Path = THEME_TS, icon_set: Path = ICON_SET) -> dict[str, str]:
    """Folder name -> icon, read from the theme's own folderIcons.ts.

    Entries limited to optional icon packs (enabledFor) and clones without a
    shipped SVG are left out; the first entry claiming a name wins.
    """
    if not ts.exists():
        return {}
    text = ts.read_text(encoding="utf-8")
    starts = list(re.finditer(r"name:\s*'(folder-[\w.-]+)'", text))
    out: dict[str, str] = {}
    for i, m in enumerate(starts):
        seg = text[m.end():starts[i + 1].start() if i + 1 < len(starts) else len(text)]
        fn = re.search(r"folderNames:\s*\[(.*?)\]", seg, re.S)
        if not fn or "enabledFor" in seg or not (icon_set / f"{m.group(1)}.svg").exists():
            continue
        for n in re.findall(r"'([^']+)'", fn.group(1)):
            out.setdefault(n.lower(), m.group(1))
    return out


def normalize(name: str) -> str:
    """'.github' / '_docs' / '__tests__' match the bare names 'github' / 'docs' / 'tests'."""
    n = name.lower().lstrip("._")
    return n[:-2] if n.endswith("__") else n


# ---------------------------------------------------------------- config

def load_overrides() -> dict:
    """config.json next to this script: {"folders": {"Name": {"kind"|"icon"|"skip"}}}."""
    p = HERE / "config.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("folders", {})


def plan(root: Path, overrides: dict | None = None, names: dict | None = None,
         deep: int = 0):
    """Top level: override > theme name > detected kind. Dot folders only get a
    theme-name icon. With deep > 0, subfolders of each project up to that depth
    get an icon when (and only when) their name is in the theme's map."""
    overrides = load_overrides() if overrides is None else overrides
    names = name_map() if names is None else names
    out = []
    for f in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not f.is_dir():
            continue
        ov = overrides.get(f.name, {})
        if ov.get("skip"):
            continue
        by_name = names.get(normalize(f.name))
        if f.name.startswith("."):
            if ov.get("icon") or by_name:
                out.append({"folder": f, "kind": "name", "icon": ov.get("icon") or by_name})
            continue
        if ov.get("icon"):
            kind, spec = ov.get("kind", "override"), ov["icon"]
        elif ov.get("kind"):
            kind, spec = ov["kind"], KINDS.get(ov["kind"], KINDS["unknown"])
        else:  # project names are not generic folder names ("IT", "Math"), so no theme lookup here
            kind = detect(f)
            spec = KINDS.get(kind, KINDS["unknown"])
        out.append({"folder": f, "kind": kind, "icon": spec})
        if deep and not ov.get("shallow"):
            out.extend(_deep(f, names, deep))
    return out


def _deep(project: Path, names: dict, max_depth: int):
    out, stack = [], [(project, 1)]
    while stack:
        d, depth = stack.pop()
        try:
            subs = sorted((e for e in os.scandir(d) if e.is_dir(follow_symlinks=False)),
                          key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in subs:
            spec = names.get(normalize(e.name))
            if e.name in NO_DESCEND or spec in GENERATED or e.name.lower().startswith("cmake-build"):
                continue
            if spec:
                out.append({"folder": Path(e.path), "kind": "name", "icon": spec})
            if depth < max_depth:
                stack.append((Path(e.path), depth + 1))
    return out


# ---------------------------------------------------------------- preview

def _font(px: int):
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        if (fonts / name).exists():
            return ImageFont.truetype(str(fonts / name), px)
    return ImageFont.load_default(px)


def preview(items, out: Path, icon_set_id: str = "material", icon_set: Path = ICON_SET):
    cell, cols, art = 150, 8, 96
    rows = (len(items) + cols - 1) // cols
    bg = (239, 241, 245) if icon_set_id.endswith("latte") else (24, 26, 30)
    fg = (40, 42, 46) if icon_set_id.endswith("latte") else (220, 222, 226)
    sheet = Image.new("RGB", (cols * cell, rows * cell), bg)
    font = _font(13)
    d = ImageDraw.Draw(sheet)
    specs = {it["icon"] for it in items}
    mnames = name_map(icon_set=icon_set) if icon_set_id != "material" else None
    with tempfile.TemporaryDirectory(prefix="folder-icons-") as tmp:
        pngs = rasterize({s: resolve_svg(s, icon_set_id, icon_set, material_names=mnames) for s in specs},
                         (art,), Path(tmp))
        for i, it in enumerate(items):
            x, y = (i % cols) * cell, (i // cols) * cell
            ico = Image.open(pngs[(it["icon"], art)]).convert("RGBA")
            sheet.paste(ico, (x + (cell - art) // 2, y + 6), ico)
            name = it["folder"].name
            if len(name) > 20:
                name = name[:19] + "..."
            d.text((x + cell / 2, y + 116), name, font=font, fill=fg, anchor="mm")
            d.text((x + cell / 2, y + 134), it["icon"].replace("folder-", ""), font=font,
                   fill=(130, 136, 145), anchor="mm")
    sheet.save(out)
    return out


# ---------------------------------------------------------------- apply / undo

FILE_ATTRIBUTE_READONLY, FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM = 0x1, 0x2, 0x4


def _attrs(p: Path) -> int:
    a = ctypes.windll.kernel32.GetFileAttributesW(str(p))
    if a == 0xFFFFFFFF:
        raise OSError(f"GetFileAttributes failed: {p}")
    return a


def _set_attrs(p: Path, a: int):
    if not ctypes.windll.kernel32.SetFileAttributesW(str(p), a):
        raise OSError(f"SetFileAttributes failed: {p}")


def _notify(paths):
    """Tell Explorer the folders changed. UPDATEITEM alone leaves icons that an open window
    already shows; UPDATEDIR on each parent makes that window re-read its items."""
    shell = ctypes.windll.shell32
    SHCNE_UPDATEITEM, SHCNE_UPDATEDIR, SHCNE_ASSOCCHANGED = 0x2000, 0x1000, 0x08000000
    SHCNF_PATHW, SHCNF_FLUSHNOWAIT = 0x5, 0x3000
    paths = [Path(p) for p in paths]
    for p in paths:
        shell.SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW, ctypes.c_wchar_p(str(p)), None)
    for parent in sorted({p.parent for p in paths}):
        shell.SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW | SHCNF_FLUSHNOWAIT, ctypes.c_wchar_p(str(parent)), None)
    shell.SHChangeNotify(SHCNE_ASSOCCHANGED, 0, None, None)


def ours(ini: Path) -> bool:
    try:
        return MARK in ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def apply(root: Path, items, icon_dir: Path, force: bool = False, notify: bool = True,
          icon_set: Path = ICON_SET, icon_set_id: str = "material", cat_root: Path = CAT_ROOT):
    icon_dir.mkdir(parents=True, exist_ok=True)
    write_icos(sorted({it["icon"] for it in items}), icon_dir, icon_set, icon_set_id, cat_root)
    manifest_path = icon_dir / MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    done, skipped = [], []
    for it in items:
        folder = it["folder"]
        ini = folder / "desktop.ini"
        if ini.exists() and not ours(ini) and not force:
            skipped.append((folder.name, "has its own desktop.ini (use --force)"))
            continue
        # each set has its own path, so Explorer's icon cache cannot serve the previous set
        ico = icon_dir / icon_set_id / icon_name(it["icon"])
        key = str(folder)
        if key not in manifest:
            manifest[key] = {"was_readonly": bool(_attrs(folder) & FILE_ATTRIBUTE_READONLY),
                             "had_foreign_ini": ini.exists() and not ours(ini)}
            if manifest[key]["had_foreign_ini"]:
                backup = folder / "desktop.ini.folder-icons-bak"
                _set_attrs(ini, 0x80)
                shutil.copyfile(ini, backup)
        if ini.exists():
            _set_attrs(ini, 0x80)  # NORMAL, otherwise a hidden+system file cannot be rewritten
        ini.write_text(f"{MARK}\r\n[.ShellClassInfo]\r\nIconResource={ico},0\r\n",
                       encoding="utf-8", newline="")
        _set_attrs(ini, FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
        _set_attrs(folder, _attrs(folder) | FILE_ATTRIBUTE_READONLY)
        done.append(folder)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if notify:
        _notify(done)
    return done, skipped


@contextmanager
def exclusive(icon_dir: Path):
    """One apply/use/undo at a time per icons folder. Two at once fight over the same
    desktop.ini files (one hides a file the other is opening); a second run waits instead,
    so the newest request is the one Explorer ends up with."""
    try:
        import msvcrt
    except ImportError:  # not Windows: apply/undo do not run there anyway
        yield
        return
    icon_dir.mkdir(parents=True, exist_ok=True)
    with open(icon_dir / ".lock", "a+b") as f:
        f.seek(0)
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.2)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


TICKET = ".request"


def take_ticket(icon_dir: Path) -> str:
    """Mark this `use` as the newest request (before waiting for the lock)."""
    icon_dir.mkdir(parents=True, exist_ok=True)
    ticket = f"{time.time_ns()}-{os.getpid()}"
    (icon_dir / TICKET).write_text(ticket, encoding="utf-8")
    return ticket


def newest(icon_dir: Path, ticket: str) -> bool:
    """Rapid switches queue up behind the lock; only the last one still needs doing.
    Without this, a queued older switch could finish last and leave its colors."""
    try:
        return (icon_dir / TICKET).read_text(encoding="utf-8").strip() == ticket
    except OSError:
        return True


def undo(root: Path, icon_dir: Path, notify: bool = True):
    manifest_path = icon_dir / MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    undone = []
    candidates = {Path(k) for k in manifest} | {p for p in root.iterdir() if p.is_dir()}
    for f in sorted(candidates):
        ini = f / "desktop.ini"
        if not f.is_dir() or not ini.exists() or not ours(ini):
            continue
        info = manifest.get(str(f), {})
        _set_attrs(ini, 0x80)
        ini.unlink()
        backup = f / "desktop.ini.folder-icons-bak"
        if backup.exists():
            backup.replace(ini)
            _set_attrs(ini, FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
        elif not info.get("was_readonly", False):
            _set_attrs(f, _attrs(f) & ~FILE_ATTRIBUTE_READONLY)
        manifest.pop(str(f), None)
        undone.append(f)
    if manifest_path.exists():
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if notify:
        _notify(undone)
    return undone


def fetch():
    for repo, dest, parts in ((ICON_REPO, HERE / ".material", ["icons", "src/core/icons"]),
                              (CAT_REPO, CAT_ROOT, ["icons", "src/defaults"])):
        if dest.exists():
            cmds = [["git", "-C", str(dest), "pull", "--ff-only", "-q"]]
        else:
            cmds = [["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", repo, str(dest)],
                    ["git", "-C", str(dest), "sparse-checkout", "set", *parts]]
        for cmd in cmds:
            if subprocess.call(cmd):
                return 1
    return 0


STATE = "state.json"


def load_state(icon_dir: Path) -> dict:
    p = icon_dir / STATE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_state(icon_dir: Path, **kw):
    icon_dir.mkdir(parents=True, exist_ok=True)
    (icon_dir / STATE).write_text(json.dumps({**load_state(icon_dir), **kw}, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    passed = failed = 0

    def check(cond, msg):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print("FAIL:", msg)

    tmp = Path(tempfile.mkdtemp(prefix="folder-icons-"))
    try:
        def mk(name, files):
            d = tmp / "root" / name
            d.mkdir(parents=True, exist_ok=True)
            for f in files:
                (d / f).parent.mkdir(parents=True, exist_ok=True)
                (d / f).write_text("x")
            return d

        cases = {
            "py": (["a.py", "b.py", "run.bat", "README.md"], "python"),
            "godot": (["project.godot", "x.py"], "godot"),
            "node": (["package.json", "src/a.js"], "javascript"),
            "ts": (["package.json", "src/a.ts", "src/b.ts", "x.js"], "typescript"),
            "web": (["index.html", "style.css"], "web"),
            "notes": (["a.md", "b.txt"], "docs"),
            "ps": (["a.ps1", "b.vbs", "README.md"], "script"),
            "_old": (["a.py"], "archive"),
            "empty": ([], "unknown"),
            "deps": (["node_modules/x/a.ts", "node_modules/x/b.ts", "main.py"], "python"),
            "pcb": (["board.kicad_pcb", "gen.py", "gen2.py"], "hardware"),
        }
        for name, (files, want) in cases.items():
            got = detect(mk(name, files))
            check(got == want, f"detect {name}: want {want}, got {got}")

        # A tiny fake icon set, so the test does not depend on the real clone.
        iset = tmp / "iset"
        iset.mkdir()
        (iset / "folder-python.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path fill="#42a5f5" d="{FOLDER_PATH}"/></svg>')
        (iset / "cpp.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" fill="#f00"/></svg>')

        composed = icon_svg("+cpp", iset)
        check('viewBox="0 0 32 32"' in composed and FOLDER_PATH in composed, "compose nests badge")
        check(icon_svg("+", iset).count("<svg") == 1, "plain folder has no badge")
        try:
            icon_svg("folder-nope", iset)
            check(False, "missing icon raises")
        except FileNotFoundError:
            check(True, "missing icon raises")
        check(icon_name("+cpp") == "compose-cpp.ico" and icon_name("folder-git") == "folder-git.ico", "icon names")

        ts = tmp / "folderIcons.ts"
        ts.write_text("""
          { name: 'folder-python', folderNames: ['python', 'py'] },
          { name: 'folder-ghost', folderNames: ['ghost'] },
          { name: 'folder-python-pack', folderNames: ['packpy'], enabledFor: [IconPack.X] },
          { name: 'folder-python', folderNames: [
              'scripts-py'
          ], clone: { base: 'folder-x', color: 'red' } },
        """)
        (iset / "folder-python-pack.svg").write_text("<svg></svg>")
        nm = name_map(ts, iset)
        check(nm.get("py") == "folder-python" and nm.get("scripts-py") == "folder-python", f"name map {nm}")
        check("ghost" not in nm, "entry without svg dropped")
        check("packpy" not in nm, "icon-pack entry dropped")
        check(normalize(".Python") == "python" and normalize("__py__") == "py", "normalize prefixes")

        cat = tmp / "cat"
        mocha = cat / "icons" / "mocha"
        mocha.mkdir(parents=True)
        (cat / "src" / "defaults").mkdir(parents=True)
        (cat / "src" / "defaults" / "folderIcons.ts").write_text(
            "const folderIcons = {\n  'types': {\n    folderNames: ['types', 'typings'],\n  },\n}")
        outline = '<path stroke="#cdd6f4" d="M0 0h8"/>'
        (mocha / "_folder.svg").write_text('<svg viewBox="0 0 16 16"><path id="plain" d="M0 0"/></svg>')
        (mocha / "folder_docs.svg").write_text(f'<svg viewBox="0 0 16 16"><g>{outline}<path d="M9 9h6"/></g></svg>')
        (mocha / "folder_security.svg").write_text('<svg viewBox="0 0 16 16"><path id="sec"/></svg>')
        (mocha / "folder_types.svg").write_text('<svg viewBox="0 0 16 16"><path id="tag"/></svg>')
        (mocha / "typescript.svg").write_text('<svg viewBox="0 0 16 16"><path id="ts"/></svg>')
        mn = {"typings": "folder-typescript", "ts": "folder-typescript"}
        check('id="sec"' in cat_svg("folder-secure", "mocha", cat, mn), "catppuccin folder alias")
        ts_svg = cat_svg("folder-typescript", "mocha", cat, mn)
        check('id="ts"' in ts_svg and outline in ts_svg and 'id="tag"' not in ts_svg,
              "language file icon beats the name map")
        mn["typings"] = "folder-typings-only"
        check('id="tag"' in cat_svg("folder-typings-only", "mocha", cat, mn), "name map used as fallback")
        check('id="plain"' in cat_svg("folder-nothing", "mocha", cat, {}), "plain folder last")
        check('id="ts"' in cat_svg("+typescript", "mocha", cat, {}), "+spec composes in catppuccin")

        css = cat / "icons" / "css-variables"
        css.mkdir()
        (css / "folder_docs.svg").write_text(
            '<svg viewBox="0 0 16 16"><g><path stroke="var(--vscode-ctp-text)" d="M0 0"/>'
            '<path stroke="var(--vscode-ctp-blue)" d="M9 9"/></g></svg>')
        (mocha / "folder_docs.svg").write_text(
            '<svg viewBox="0 0 16 16"><g><path stroke="#cdd6f4" d="M0 0"/><path stroke="#89b4fa" d="M9 9"/></g></svg>')
        ref = ctp_reference(cat)
        check(ref == {"text": "#cdd6f4", "blue": "#89b4fa"}, f"ctp reference from paired icons {ref}")
        theme = {"bg": "#101010", "accent": "#ff0000", "secondary": "#00ff00",
                 "text": "#eeeeee", "dim": "#555555", "faint": "#202020"}
        check(palette_colors(theme, 1.0, ref) == {"text": "#eeeeee", "blue": "#ff0000"}, "full strength = accent")
        check(palette_colors(theme, 0.0, ref)["blue"] == "#89b4fa", "zero strength = reference")
        pid = save_palette(tmp / "pal", theme, 0.5)
        check(pid == palette_id(dict(theme), 0.5) and pid.startswith("palette-"), "palette id stable")
        svg = palette_svg("folder-docs", pid, tmp / "pal", cat, {})
        check("var(" not in svg and "#eeeeee" in svg and _mix("#89b4fa", "#ff0000", 0.5) in svg,
              "palette svg has no variables left")
        try:
            save_palette(tmp / "pal", {"bg": "#000000"}, 0.5)
            check(False, "incomplete palette rejected")
        except ValueError:
            check(True, "incomplete palette rejected")
        nroot = tmp / "nroot"
        for d in (".py", ".hidden", "proj/py", "proj/a/b/c/py", "proj/node_modules/py", "py"):
            (nroot / d).mkdir(parents=True)
        (nroot / "proj" / "x.py").write_text("x")
        flat = {str(i["folder"].relative_to(nroot)) for i in plan(nroot, {}, nm)}
        check(flat == {".py", "proj", "py"}, f"top level only, dot folder by name: {flat}")
        top_py = next(i for i in plan(nroot, {}, nm) if i["folder"].name == "py")
        check(top_py["kind"] != "name", "top-level project is detected, not name-matched")
        deep = {str(i["folder"].relative_to(nroot)).replace("\\", "/") for i in plan(nroot, {}, nm, deep=2)}
        check(deep == {".py", "proj", "proj/py", "py"}, f"deep respects depth and node_modules: {deep}")
        (nroot / "proj" / "dist" / "py").mkdir(parents=True)
        nm["dist"] = "folder-dist"
        deep = {str(i["folder"].relative_to(nroot)).replace("\\", "/") for i in plan(nroot, {}, nm, deep=3)}
        check(not any("dist" in d for d in deep), f"generated folders skipped: {deep}")

        icons = tmp / "icons"
        icons.mkdir()
        write_icos(["folder-python", "+cpp"], icons, iset)
        with Image.open(icons / "material" / "folder-python.ico") as im:
            sizes = set(im.info.get("sizes", set()))
        check(set((s, s) for s in ICO_SIZES) <= sizes, f"ico sizes {sizes}")
        with Image.open(icons / "material" / "compose-cpp.ico") as im:
            im.size = (256, 256)
            big = im.convert("RGBA")
        check(big.getpixel((200, 200))[:3] == (255, 0, 0), "badge drawn in its corner")
        check(big.getpixel((2, 2))[3] == 0, "corner transparent")

        if sys.platform == "win32":
            root = tmp / "root"
            items = [it for it in plan(root, overrides={}) if it["folder"].name in ("py", "pcb")]
            for it in items:
                it["icon"] = "folder-python" if it["folder"].name == "py" else "+cpp"
            foreign = root / "pcb" / "desktop.ini"
            foreign.write_text("[.ShellClassInfo]\r\nIconResource=C:\\x.ico,0\r\n")
            done, skipped = apply(root, items, icons, notify=False, icon_set=iset)
            check([f.name for f in done] == ["py"], f"apply done {done}")
            check(len(skipped) == 1 and skipped[0][0] == "pcb", f"foreign ini skipped {skipped}")
            ini = root / "py" / "desktop.ini"
            check(ours(ini), "ini marked")
            check(b"\r\r\n" not in ini.read_bytes(), "ini has plain CRLF")
            check(_attrs(ini) & FILE_ATTRIBUTE_HIDDEN, "ini hidden")
            check(_attrs(root / "py") & FILE_ATTRIBUTE_READONLY, "folder readonly")
            apply(root, items, icons, notify=False, icon_set=iset)  # idempotent re-run
            check(ours(ini), "re-apply ok")
            done, _ = apply(root, items, icons, force=True, notify=False, icon_set=iset)
            check(len(done) == 2, "force overrides foreign ini")
            undone = undo(root, icons, notify=False)
            check(len(undone) == 2, f"undo count {undone}")
            check(not ini.exists(), "ini removed")
            check(not _attrs(root / "py") & FILE_ATTRIBUTE_READONLY, "readonly cleared")
            check(foreign.exists() and not ours(foreign), "foreign ini restored")

            import threading
            order = []

            def hold(tag, secs):
                with exclusive(icons):
                    order.append(tag + "+")
                    time.sleep(secs)
                    order.append(tag + "-")

            first = threading.Thread(target=hold, args=("a", 0.6))
            first.start()
            time.sleep(0.1)
            second = threading.Thread(target=hold, args=("b", 0))
            second.start()
            first.join()
            second.join()
            check(order == ["a+", "a-", "b+", "b-"], f"second run waits for the first: {order}")

            older = take_ticket(icons)
            time.sleep(0.01)
            newer = take_ticket(icons)
            check(not newest(icons, older) and newest(icons, newer), "only the newest request runs")
    finally:
        if sys.platform == "win32":
            for q in [tmp, *tmp.rglob("*")]:
                try:
                    _set_attrs(q, 0x80 if q.is_file() else 0x10)
                except Exception:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"selftest: {passed} passed, {failed} failed")
    return 1 if failed else 0


# ---------------------------------------------------------------- cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Material Icon Theme icons for Windows project folders.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("preview", "apply", "undo", "list"):
        p = sub.add_parser(name)
        p.add_argument("root", type=Path)
        p.add_argument("--icons", type=Path, default=HERE / "icons", help="where .ico files live")
        if name in ("apply", "list", "preview"):
            p.add_argument("--deep", type=int, default=0, metavar="N",
                           help="also icon subfolders up to depth N, by exact theme name only")
        if name == "apply":
            p.add_argument("--force", action="store_true", help="replace foreign desktop.ini (backed up)")
        if name in ("apply", "preview"):
            p.add_argument("--set", dest="icon_set", choices=SETS, default=None,
                           help="icon set (default: the last one applied, else material)")
        if name == "preview":
            p.add_argument("--out", type=Path, default=HERE / "preview.png")
    u = sub.add_parser("use", help="switch the applied folders to another icon set")
    u.add_argument("icon_set", choices=SETS + ("catppuccin", "palette"))
    u.add_argument("--icons", type=Path, default=HERE / "icons")
    u.add_argument("--palette", help="palette: JSON text or a .json file with "
                   + ", ".join(PALETTE_KEYS) + " (experimental, for `use palette`)")
    u.add_argument("--strength", type=float, default=0.6,
                   help="0..1, how far motif colors move toward the accent (default 0.6)")
    sub.add_parser("fetch")
    sub.add_parser("icons")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "fetch":
        return fetch()
    if a.cmd == "use":
        icon_dir = a.icons.resolve()
        st = load_state(icon_dir)
        if "root" not in st:
            print("nothing applied yet; run apply first", file=sys.stderr)
            return 1
        set_id = "catppuccin-mocha" if a.icon_set == "catppuccin" else a.icon_set
        if set_id == "palette":
            if not a.palette:
                print("use palette needs --palette (JSON text or a .json file)", file=sys.stderr)
                return 2
            src = Path(a.palette)
            theme = json.loads(src.read_text(encoding="utf-8") if src.suffix == ".json" and src.exists()
                               else a.palette)
            theme = theme.get("theme", theme)  # accept a whole livery pack too
            set_id = save_palette(icon_dir, theme, max(0.0, min(1.0, a.strength)))
        root = Path(st["root"])
        ticket = take_ticket(icon_dir)
        with exclusive(icon_dir):
            if not newest(icon_dir, ticket):
                print(f"{set_id}: skipped, a newer switch is queued")
                return 0
            done, skipped = apply(root, plan(root, deep=st.get("deep", 0)), icon_dir,
                                  force=st.get("force", False), icon_set_id=set_id)
            save_state(icon_dir, set=set_id)
        print(f"{set_id}: {len(done)} folders")
        return 0
    if a.cmd == "icons":
        names = sorted(p.stem for p in ICON_SET.glob("folder-*.svg") if "-open" not in p.stem)
        print(" ".join(n.replace("folder-", "") for n in names))
        return 0
    root = a.root.resolve()
    if a.cmd == "undo":
        with exclusive(a.icons.resolve()):
            for f in undo(root, a.icons.resolve()):
                print("undone", f.name)
        return 0
    items = plan(root, deep=a.deep)
    set_id = getattr(a, "icon_set", None) or load_state(a.icons.resolve()).get("set", "material")
    if a.cmd == "list":
        for it in items:
            print(f"{str(it['folder'].relative_to(root)):50} {it['kind']:11} {it['icon']}")
        print(f"{len(items)} folders")
    elif a.cmd == "preview":
        print(preview([it for it in items if it["folder"].parent == root], a.out, set_id))
    elif a.cmd == "apply":
        with exclusive(a.icons.resolve()):
            done, skipped = apply(root, items, a.icons.resolve(), force=a.force, icon_set_id=set_id)
            save_state(a.icons.resolve(), root=str(root), deep=a.deep, force=a.force, set=set_id)
        print(f"applied {len(done)} folders ({set_id})")
        for name, why in skipped:
            print(f"skipped {name}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
