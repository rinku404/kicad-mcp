"""Symbol and footprint libraries: KiCad's stock libraries and a per-project library.

Each project gets its own library, ``libs/<project>.kicad_sym`` and
``libs/<project>.pretty`` (3D models in ``libs/3dmodels``), registered in the
project's sym-lib-table and fp-lib-table under the project's name. Everything
lives inside the project folder, so the design is self-contained.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import kicad_cli, sexpr
from .schematic_edit import _edit_symbol, _write_atomic

LIB_HEADER = '(kicad_symbol_lib\n\t(version 20241209)\n\t(generator "kicad_mcp")\n\t(generator_version "9.0")\n)\n'
PIN_TYPES = {
    "input", "output", "bidirectional", "tri_state", "passive", "free",
    "unspecified", "power_in", "power_out", "open_collector", "open_emitter", "no_connect",
}  # fmt: skip


class LibraryError(ValueError):
    pass


# --------------------------------------------------------------------------- KiCad install


def share_dir() -> Path | None:
    """KiCad's shared data folder (with symbols/, footprints/, 3dmodels/)."""
    env = os.environ.get("KICAD_SHARE_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    for var in ("KICAD10_SYMBOL_DIR", "KICAD9_SYMBOL_DIR"):
        if os.environ.get(var) and Path(os.environ[var]).is_dir():
            return Path(os.environ[var]).parent
    candidates: list[Path] = []
    cli = kicad_cli.find_kicad_cli()
    if cli:
        exe = Path(cli).resolve()
        candidates += [exe.parent.parent / "share" / "kicad", exe.parent.parent / "SharedSupport"]
    if platform.system() == "Linux":
        candidates += [Path("/usr/share/kicad"), Path("/usr/local/share/kicad")]
    return next((c for c in candidates if (c / "symbols").is_dir()), None)


def _cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    d = Path(base) / "kicad-mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- stock symbol index

_SYM_START = re.compile(r'^\t\(symbol "((?:[^"\\]|\\.)*)"\s*$')
_PROP_LINE = re.compile(r'^\t\t\(property "(Description|ki_keywords|Footprint|ki_fp_filters|Datasheet)" "((?:[^"\\]|\\.)*)"')
_EXTENDS = re.compile(r'^\t\t\(extends "((?:[^"\\]|\\.)*)"\)')


def _scan_symbol_lib(path: Path) -> list[dict]:
    out: list[dict] = []
    cur: dict | None = None
    with path.open(encoding="utf-8") as f:
        for line in f:
            m = _SYM_START.match(line)
            if m:
                cur = {"id": f"{path.stem}:{m.group(1)}"}
                out.append(cur)
                continue
            if cur is None:
                continue
            m = _PROP_LINE.match(line)
            if m:
                key = {"Description": "description", "ki_keywords": "keywords", "Footprint": "footprint",
                       "ki_fp_filters": "footprint_filters", "Datasheet": "datasheet"}[m.group(1)]  # fmt: skip
                if m.group(2) and m.group(2) != "~":
                    cur[key] = m.group(2)
                continue
            m = _EXTENDS.match(line)
            if m:
                cur["extends"] = m.group(1)
    return out


def symbol_index() -> list[dict]:
    """Names, descriptions and keywords of every stock symbol, cached on disk."""
    share = share_dir()
    if share is None:
        raise LibraryError("KiCad's library folder was not found; set KICAD_SHARE_DIR to KiCad's share/kicad folder")
    libs = sorted((share / "symbols").glob("*.kicad_sym"))
    stamp = [str(share), len(libs), max((p.stat().st_mtime for p in libs), default=0)]
    cache = _cache_dir() / "symbol-index.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data["stamp"] == stamp:
            return data["symbols"]
    except (OSError, ValueError, KeyError):
        pass
    symbols = [s for lib in libs for s in _scan_symbol_lib(lib)]
    cache.write_text(json.dumps({"stamp": stamp, "symbols": symbols}), encoding="utf-8")
    return symbols


def _score(query_tokens: list[str], name: str, text: str) -> int | None:
    name_l, text_l = name.lower(), text.lower()
    if not all(t in text_l for t in query_tokens):
        return None
    score = sum(3 for t in query_tokens if t in name_l)
    if " ".join(query_tokens) == name_l.split(":")[-1]:
        score += 20
    elif name_l.split(":")[-1].startswith(query_tokens[0]):
        score += 5
    return score - len(name) // 20


def find_symbols(query: str, limit: int = 20) -> list[dict]:
    tokens = query.lower().split()
    if not tokens:
        raise LibraryError("empty query")
    hits = []
    for s in symbol_index():
        text = " ".join(str(s.get(k, "")) for k in ("id", "description", "keywords"))
        sc = _score(tokens, s["id"], text)
        if sc is not None:
            hits.append((sc, s))
    hits.sort(key=lambda h: (-h[0], h[1]["id"]))
    return [h[1] for h in hits[:limit]]


def find_footprints(query: str, limit: int = 20) -> list[str]:
    """Search stock footprints by library and file name, e.g. "SOIC-8", "0603 resistor", "USB C"."""
    share = share_dir()
    if share is None:
        raise LibraryError("KiCad's library folder was not found; set KICAD_SHARE_DIR to KiCad's share/kicad folder")
    tokens = query.lower().replace("_", " ").split()
    if not tokens:
        raise LibraryError("empty query")
    hits = []
    for lib in (share / "footprints").glob("*.pretty"):
        for fp in lib.glob("*.kicad_mod"):
            fid = f"{lib.stem}:{fp.stem}"
            sc = _score(tokens, fid, fid.replace("_", " "))
            if sc is not None:
                hits.append((sc, fid))
    hits.sort(key=lambda h: (-h[0], len(h[1]), h[1]))
    return [h[1] for h in hits[:limit]]


def stock_symbol_blocks(lib_id: str) -> list[str]:
    """Text of a stock symbol, preceded by the symbols it extends (parents first)."""
    share = share_dir()
    if share is None or ":" not in lib_id:
        raise LibraryError(f"symbol id should look like Library:Name, got {lib_id!r}")
    lib, name = lib_id.split(":", 1)
    path = share / "symbols" / f"{lib}.kicad_sym"
    if not path.exists():
        raise LibraryError(f"no stock symbol library named {lib!r}")
    blocks = _symbol_blocks(path.read_text(encoding="utf-8"))
    chain: list[str] = []
    while name:
        if name not in blocks:
            raise LibraryError(f"no symbol {name!r} in library {lib!r}")
        chain.insert(0, blocks[name])
        m = re.search(r'\(extends\s+"((?:[^"\\]|\\.)*)"\)', blocks[name])
        name = m.group(1) if m else ""
    return chain


# --------------------------------------------------------------------------- symbol library files


def _symbol_blocks(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for s, e in sexpr.child_spans(text):
        m = re.match(r'\(symbol\s+"((?:[^"\\]|\\.)*)"', text[s:e])
        if m:
            out[m.group(1)] = text[s:e]
    return out


def _rename(block: str, old: str, new: str) -> str:
    q_old, q_new = sexpr.quote(old)[1:-1], sexpr.quote(new)[1:-1]
    block = block.replace(f'(symbol "{q_old}"', f'(symbol "{q_new}"', 1)
    return re.sub(rf'\(symbol "{re.escape(q_old)}_(\d+_\d+)"', lambda m: f'(symbol "{q_new}_{m.group(1)}"', block)


def upsert_symbols(lib_path: Path, blocks: list[str]) -> list[str]:
    """Add symbol blocks to a .kicad_sym, replacing same-named symbols. Returns the names."""
    text = lib_path.read_text(encoding="utf-8") if lib_path.exists() else LIB_HEADER
    names = []
    for block in blocks:
        name = re.match(r'\(symbol\s+"((?:[^"\\]|\\.)*)"', block).group(1)
        names.append(name)
        existing = None
        for s, e in sexpr.child_spans(text):
            if re.match(rf'\(symbol\s+"{re.escape(sexpr.quote(name)[1:-1])}"', text[s:e]):
                existing = (s, e)
        if existing:
            text = text[: existing[0]] + block + text[existing[1] :]
        else:
            end = text.rstrip().rfind(")")
            text = text[:end].rstrip("\n") + "\n\t" + block + "\n" + text[end:]
    lib_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(lib_path, text)
    return names


# --------------------------------------------------------------------------- project library


@dataclass
class ProjectLibrary:
    folder: Path
    nickname: str

    @property
    def symbol_file(self) -> Path:
        return self.folder / "libs" / f"{self.nickname}.kicad_sym"

    @property
    def footprint_dir(self) -> Path:
        return self.folder / "libs" / f"{self.nickname}.pretty"

    @property
    def model_dir(self) -> Path:
        return self.folder / "libs" / "3dmodels"

    def ensure(self) -> None:
        """Create the library files and register them in the project's library tables."""
        if not self.symbol_file.exists():
            self.symbol_file.parent.mkdir(parents=True, exist_ok=True)
            self.symbol_file.write_text(LIB_HEADER, encoding="utf-8")
        self.footprint_dir.mkdir(parents=True, exist_ok=True)
        _register(self.folder / "sym-lib-table", "sym_lib_table", self.nickname,
                  f"${{KIPRJMOD}}/libs/{self.nickname}.kicad_sym")  # fmt: skip
        _register(self.folder / "fp-lib-table", "fp_lib_table", self.nickname,
                  f"${{KIPRJMOD}}/libs/{self.nickname}.pretty")  # fmt: skip


def project_library(folder: Path, project_name: str) -> ProjectLibrary:
    nick = re.sub(r"[^A-Za-z0-9_-]+", "_", project_name).strip("_") or "project"
    return ProjectLibrary(folder, nick)


def _register(table: Path, kind: str, name: str, uri: str) -> None:
    entry = f'\t(lib (name {sexpr.quote(name)}) (type "KiCad") (uri {sexpr.quote(uri)}) (options "") (descr "Project parts added by kicad-mcp"))\n'
    if table.exists():
        text = table.read_text(encoding="utf-8")
        doc = sexpr.parse(text)
        if any(sexpr.value(lib, "name") == name for lib in sexpr.children(doc, "lib")):
            return
        end = text.rstrip().rfind(")")
        text = text[:end].rstrip("\n") + "\n" + entry + text[end:]
    else:
        text = f"({kind}\n\t(version 7)\n{entry})\n"
    _write_atomic(table, text)


def set_symbol_properties(block: str, fields: dict[str, str]) -> str:
    fields = {k: v for k, v in fields.items() if v is not None}
    return _edit_symbol(block, fields)[0] if fields else block


def copy_stock_symbol(lib: ProjectLibrary, lib_id: str, new_name: str | None, fields: dict[str, str]) -> str:
    """Copy a stock symbol into the project library, optionally renamed, with fields set."""
    lib.ensure()
    chain = stock_symbol_blocks(lib_id)
    name = lib_id.split(":", 1)[1]
    target = new_name or name
    last = _rename(chain[-1], name, target) if target != name else chain[-1]
    last = set_symbol_properties(last, {"Value": target if new_name else None, **fields})
    upsert_symbols(lib.symbol_file, [*chain[:-1], last])
    return f"{lib.nickname}:{target}"


# --------------------------------------------------------------------------- generated symbols


def _fmt(v: float) -> str:
    return f"{round(v, 4):g}"


def generate_symbol(
    name: str,
    pins: list[dict],
    reference: str = "U",
    footprint: str = "",
    datasheet: str = "",
    description: str = "",
    fields: dict[str, str] | None = None,
) -> str:
    """Build a rectangular symbol from a pin table (e.g. read from a datasheet).

    Each pin is ``{"number", "name", "type", "side"}``; ``type`` is a KiCad pin type
    (input, output, bidirectional, power_in, power_out, passive, ...) and ``side`` is
    left/right/top/bottom. Without a side, power pins go top (supplies) or bottom
    (grounds), outputs right and everything else left.
    """
    if not pins:
        raise LibraryError("a symbol needs at least one pin")
    numbers = [str(p.get("number", "")).strip() for p in pins]
    if any(not n for n in numbers) or len(set(numbers)) != len(numbers):
        raise LibraryError("every pin needs a unique number")
    sides: dict[str, list[dict]] = {"left": [], "right": [], "top": [], "bottom": []}
    for p in pins:
        ptype = (p.get("type") or "passive").lower().replace("-", "_").replace(" ", "_")
        if ptype not in PIN_TYPES:
            raise LibraryError(f"pin {p.get('number')}: unknown type {ptype!r}; use one of {sorted(PIN_TYPES)}")
        pname = str(p.get("name") or "~")
        side = (p.get("side") or "").lower()
        if side not in sides:
            ground = re.match(r"(GND|VSS|AGND|DGND|PGND|V-|VEE|EP|PAD)", pname.upper())
            if ptype in ("power_in", "power_out") and ground:
                side = "bottom"
            elif ptype == "power_in":
                side = "top"
            elif ptype in ("output", "power_out", "open_collector", "open_emitter", "tri_state"):
                side = "right"
            else:
                side = "left"
        sides[side].append({**p, "type": ptype, "name": pname, "number": str(p["number"]).strip()})

    g = 2.54
    char = 1.27 * 0.75
    longest = lambda ps: max((len(p["name"]) for p in ps if p["name"] != "~"), default=0)
    half_w = max(
        (longest(sides["left"]) + longest(sides["right"])) * char / 2 + g,
        (max(len(sides["top"]), len(sides["bottom"])) + 1) * g / 2,
        2 * g,
    )
    # pins sit on the 2.54 mm grid: n pins span from +((n-1)//2) to -(n-1-(n-1)//2) steps
    def extent(n: int) -> float:
        return max((n - 1) // 2, n - 1 - (n - 1) // 2) * g if n else 0

    half_h = max(extent(len(sides["left"])), extent(len(sides["right"]))) + g
    # vertical pin names on the top and bottom must clear the side pin names
    for edge in ("top", "bottom"):
        if sides[edge]:
            half_h = max(half_h, extent(max(len(sides["left"]), len(sides["right"]))) + 2.54 + longest(sides[edge]) * char)
    half_w = max(half_w, extent(len(sides["top"])) + g, extent(len(sides["bottom"])) + g)
    half_w = g * -(-half_w // g)
    half_h = max(g * -(-half_h // g), 2 * g)

    pin_lines = []

    def place(ps: list[dict], side: str) -> None:
        first = (len(ps) - 1) // 2
        for i, p in enumerate(ps):
            offset = (first - i) * g
            if side == "left":
                x, y, a = -half_w - g, offset, 0
            elif side == "right":
                x, y, a = half_w + g, offset, 180
            elif side == "top":
                x, y, a = -offset, half_h + g, 270
            else:
                x, y, a = -offset, -half_h - g, 90
            pin_lines.append(
                f"\t\t\t(pin {p['type']} line\n\t\t\t\t(at {_fmt(x)} {_fmt(y)} {a})\n\t\t\t\t(length 2.54)\n"
                f"\t\t\t\t(name {sexpr.quote(p['name'])}\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n"
                f"\t\t\t\t(number {sexpr.quote(p['number'])}\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n\t\t\t)"
            )

    for side in ("left", "right", "top", "bottom"):
        place(sides[side], side)

    def prop(key: str, val: str, x: float, y: float, hide: bool) -> str:
        h = "\n\t\t\t(hide yes)" if hide else ""
        return (
            f"\t\t(property {sexpr.quote(key)} {sexpr.quote(val)}\n\t\t\t(at {_fmt(x)} {_fmt(y)} 0){h}\n"
            f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)"
        )

    props = [
        prop("Reference", reference, 0, half_h + g + (g if sides["top"] else 0) + 1.27, False),
        prop("Value", name, 0, -half_h - g - (g if sides["bottom"] else 0) - 1.27, False),
        prop("Footprint", footprint, 0, 0, True),
        prop("Datasheet", datasheet, 0, 0, True),
        prop("Description", description, 0, 0, True),
    ] + [prop(k, v, 0, 0, True) for k, v in (fields or {}).items() if k not in ("Reference", "Value", "Footprint", "Datasheet", "Description")]
    q = sexpr.quote(name)
    body = sexpr.quote(f"{name}_0_1")
    unit = sexpr.quote(f"{name}_1_1")
    return (
        f"(symbol {q}\n\t\t(pin_names\n\t\t\t(offset 1.016)\n\t\t)\n\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n"
        + "\n".join(props)
        + f"\n\t\t(symbol {body}\n\t\t\t(rectangle\n\t\t\t\t(start {_fmt(-half_w)} {_fmt(half_h)})\n\t\t\t\t(end {_fmt(half_w)} {_fmt(-half_h)})\n"
        "\t\t\t\t(stroke\n\t\t\t\t\t(width 0.254)\n\t\t\t\t\t(type default)\n\t\t\t\t)\n\t\t\t\t(fill\n\t\t\t\t\t(type background)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n"
        f"\t\t(symbol {unit}\n" + "\n".join(pin_lines) + "\n\t\t)\n\t\t(embedded_fonts no)\n\t)"
    )


# --------------------------------------------------------------------------- vendor zip import


def import_ecad_zip(lib: ProjectLibrary, zip_path: Path) -> dict:
    """Import KiCad symbols, footprints and 3D models from a vendor download.

    Works with the ZIPs from Mouser/SamacSys (Component Search Engine), SnapEDA and
    Ultra Librarian when they contain KiCad files. Legacy .lib symbol files are
    converted with kicad-cli when it is available.
    """
    if not zip_path.exists():
        raise LibraryError(f"file not found: {zip_path}")
    lib.ensure()
    result: dict = {"symbols": [], "footprints": [], "models": [], "skipped": []}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                target = (root / info.filename).resolve()
                if not str(target).startswith(str(root.resolve())):
                    raise LibraryError(f"unsafe path in zip: {info.filename}")
            z.extractall(root)

        # 3D models first, so footprints can point at them
        models: dict[str, str] = {}
        for f in root.rglob("*"):
            if f.suffix.lower() in (".step", ".stp", ".wrl"):
                lib.model_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, lib.model_dir / f.name)
                models[f.stem.lower()] = f.name
                result["models"].append(f.name)

        fp_names = []
        for f in root.rglob("*.kicad_mod"):
            text = f.read_text(encoding="utf-8", errors="replace")
            text = _relink_models(text, models)
            _write_atomic(lib.footprint_dir / f.name, text)
            fp_names.append(f.stem)
        result["footprints"] = [f"{lib.nickname}:{n}" for n in fp_names]

        sym_files = list(root.rglob("*.kicad_sym"))
        if not sym_files:
            for legacy in root.rglob("*.lib"):
                converted = _upgrade_legacy_lib(legacy)
                if converted:
                    sym_files.append(converted)
                else:
                    result["skipped"].append(f"{legacy.name} (legacy symbol format; install KiCad to convert it)")
        for f in sym_files:
            blocks = list(_symbol_blocks(f.read_text(encoding="utf-8", errors="replace")).values())
            if len(fp_names) == 1:
                blocks = [_point_footprint(b, f"{lib.nickname}:{fp_names[0]}") for b in blocks]
            result["symbols"] += [f"{lib.nickname}:{n}" for n in upsert_symbols(lib.symbol_file, blocks)]
    if not (result["symbols"] or result["footprints"]):
        raise LibraryError("no KiCad symbols or footprints found in the zip (was it downloaded in KiCad format?)")
    return result


def _relink_models(fp_text: str, models: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        stem = Path(m.group(1).replace("\\", "/")).stem.lower()
        if stem in models:
            return f'(model "${{KIPRJMOD}}/libs/3dmodels/{models[stem]}"'
        return m.group(0)

    return re.sub(r'\(model\s+"?((?:[^"\\\s)]|\\.)+)"?', repl, fp_text)


def _point_footprint(block: str, fp_id: str) -> str:
    m = re.search(r'\(property\s+"Footprint"\s+"((?:[^"\\]|\\.)*)"', block)
    if m and m.group(1):
        return block[: m.start(1)] + sexpr.quote(fp_id)[1:-1] + block[m.end(1) :]
    return set_symbol_properties(block, {"Footprint": fp_id})


def _upgrade_legacy_lib(path: Path) -> Path | None:
    cli = kicad_cli.find_kicad_cli()
    if not cli:
        return None
    out = path.with_suffix(".kicad_sym")
    proc = subprocess.run(
        [cli, "sym", "upgrade", "--force", "-o", str(out), str(path)], capture_output=True, text=True, check=False
    )
    return out if proc.returncode == 0 and out.exists() else None
