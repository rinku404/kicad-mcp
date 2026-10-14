"""Place symbols and wire pins in .kicad_sch files.

Connections are made the way a careful person would on a busy sheet: a short
wire stub leaves each pin and ends in a net label (or a power symbol for
supply and ground nets). Labels never cross other parts, so they cannot short
anything by accident, and the result is re-checked before it is kept.
"""

from __future__ import annotations

import math
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import libraries, sexpr
from .schematic import _key, _lib_pins, _LibPin, _on_segment, _transform, load_schematic
from .schematic_edit import _edit_symbol, _write_atomic

GRID = 1.27
PAPER = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594),
         "A0": (1189, 841), "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8),
         "D": (863.6, 558.8), "E": (1117.6, 863.6), "USLetter": (279.4, 215.9),
         "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4)}  # fmt: skip


class PlaceError(ValueError):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _f(v: float) -> str:
    return f"{round(v, 4):g}"


def _snap(v: float, step: float = GRID) -> float:
    return round(round(v / step) * step, 4)


def _head(text: str, s: int) -> str:
    m = re.match(r"\(\s*([^\s()]+)", text[s : s + 64])
    return m.group(1) if m else ""


def _indent(block: str, tabs: int) -> str:
    return block.replace("\n", "\n" + "\t" * tabs)


# --------------------------------------------------------------------------- library symbols


def _lib_tables(folder: Path) -> dict[str, Path]:
    """Nicknames of the project's own symbol libraries (sym-lib-table) mapped to their files."""
    table = folder / "sym-lib-table"
    out: dict[str, Path] = {}
    if not table.exists():
        return out
    share = libraries.share_dir()

    def var(m: re.Match) -> str:
        key = m.group(1)
        if key == "KIPRJMOD":
            return str(folder)
        if re.fullmatch(r"KICAD\d*_SYMBOL_DIR", key) and share:
            return str(share / "symbols")
        return os.environ.get(key, m.group(0))

    for lib in sexpr.children(sexpr.parse(table.read_text(encoding="utf-8")), "lib"):
        name, uri = sexpr.value(lib, "name"), sexpr.value(lib, "uri")
        if name and uri:
            out[name] = Path(re.sub(r"\$\{([^}]+)\}", var, uri))
    return out


def _flatten(chain: list[str], lib: str) -> str:
    """One self-contained symbol from a derived-symbol chain (root parent first), named Lib:Name."""
    root, target = chain[0], chain[-1]
    root_name = re.match(r'\(symbol\s+"((?:[^"\\]|\\.)*)"', root).group(1)
    name = re.match(r'\(symbol\s+"((?:[^"\\]|\\.)*)"', target).group(1)
    if len(chain) > 1:
        props: dict[str, str] = {}
        for block in chain:
            for s, e in sexpr.child_spans(block):
                m = re.match(r'\(property\s+"((?:[^"\\]|\\.)*)"', block[s:e])
                if m:
                    props[m.group(1)] = block[s:e]
        base = libraries._rename(root, root_name, name)
        parts, done_props = [], False
        for s, e in sexpr.child_spans(base):
            head = _head(base, s)
            if head == "property":
                if not done_props:
                    parts += list(props.values())
                    done_props = True
                continue
            if head == "extends":
                continue
            parts.append(base[s:e])
        target = f"(symbol {sexpr.quote(name)}\n\t\t" + "\n\t\t".join(parts) + "\n\t)"
    full = sexpr.quote(f"{lib}:{name}")
    return re.sub(r'^\(symbol\s+"(?:[^"\\]|\\.)*"', lambda _: f"(symbol {full}", target, count=1)


def library_symbol(folder: Path, lib_id: str) -> str:
    """Text of ``lib_id`` ready for a schematic's lib_symbols (flattened, named Lib:Name)."""
    if ":" not in lib_id:
        raise PlaceError(f"symbol id should look like Library:Name, e.g. Device:R, got {lib_id!r}")
    lib, name = lib_id.split(":", 1)
    own = _lib_tables(folder)
    if lib in own:
        path = own[lib]
        if not path.exists():
            raise PlaceError(f"library {lib!r} points at {path}, which does not exist")
        blocks = libraries._symbol_blocks(path.read_text(encoding="utf-8"))
        chain: list[str] = []
        cur = name
        while cur:
            if cur not in blocks:
                raise PlaceError(f"no symbol {cur!r} in library {lib!r}")
            chain.insert(0, blocks[cur])
            m = re.search(r'\(extends\s+"((?:[^"\\]|\\.)*)"\)', blocks[cur])
            cur = m.group(1) if m else ""
    else:
        try:
            chain = libraries.stock_symbol_blocks(lib_id)
        except libraries.LibraryError as e:
            raise PlaceError(str(e)) from e
    return _flatten(chain, lib)


def _graphics_points(lib_sym: list) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []

    def walk(node: list) -> None:
        for c in node[1:]:
            if not isinstance(c, list) or not c:
                continue
            if c[0] in ("property",):
                continue
            if c[0] in ("xy", "start", "end", "center", "mid", "at") and len(c) >= 3:
                try:
                    pts.append((float(c[1]), float(c[2])))
                except ValueError:
                    pass
            elif c[0] == "circle":
                cx, cy = (float(v) for v in (sexpr.child(c, "center") or ["c", 0, 0])[1:3])
                r = float(sexpr.value(c, "radius", "0") or 0)
                pts.extend([(cx - r, cy - r), (cx + r, cy + r)])
                continue
            walk(c)

    for sub in sexpr.children(lib_sym, "symbol"):
        walk(sub)
    return pts


# --------------------------------------------------------------------------- a sheet in memory


@dataclass
class _Placed:
    node: list
    lib: list | None
    pins: list[_LibPin]
    refs: set[str]


@dataclass
class Sheet:
    path: Path
    text: str
    instance: str  # instance path of this sheet, e.g. "/<root uuid>"
    human: str  # "/" or "/Power/"
    project_name: str
    doc: list = field(init=False)
    lib: dict[str, list] = field(init=False)
    symbols: list[_Placed] = field(init=False)
    new_items: list[str] = field(default_factory=list)
    new_lib: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.reparse()

    def reparse(self) -> None:
        self.doc = sexpr.parse(self.text)
        self.lib = {s[1]: s for s in sexpr.children(sexpr.child(self.doc, "lib_symbols") or ["x"], "symbol")}
        for block in self.new_lib:
            node = sexpr.parse(block)
            self.lib[node[1]] = node
        self.symbols = []
        for sym in sexpr.children(self.doc, "symbol"):
            self._add_placed(sym)
        for item in self.new_items:
            node = sexpr.parse(item)
            if node[0] == "symbol":
                self._add_placed(node)

    def _add_placed(self, sym: list) -> None:
        lib_id = sexpr.value(sym, "lib_name") or sexpr.value(sym, "lib_id", "") or ""
        lsym = self.lib.get(lib_id)
        unit = int(sexpr.value(sym, "unit", "1") or 1)
        style = int(sexpr.value(sym, "body_style", None) or sexpr.value(sym, "convert", "1") or 1)
        pins = [p for p in _lib_pins(lsym) if p.unit in (0, unit) and p.style in (0, style)] if lsym else []
        refs = {sexpr.properties(sym).get("Reference", "")}
        for proj in sexpr.children(sexpr.child(sym, "instances") or ["x"], "project"):
            for p in sexpr.children(proj, "path"):
                refs.add(sexpr.value(p, "reference", "") or "")
        refs.discard("")
        self.symbols.append(_Placed(sym, lsym, pins, refs))

    # ---- geometry

    def connection_points(self) -> tuple[set[tuple[int, int]], list[tuple[tuple[int, int], tuple[int, int]]]]:
        """Every point something can connect at, and every wire segment, on this sheet."""
        points: set[tuple[int, int]] = set()
        segments = []
        nodes = list(self.doc[1:]) + [sexpr.parse(t) for t in self.new_items]
        for n in nodes:
            if not isinstance(n, list) or not n:
                continue
            if n[0] in ("wire", "bus"):
                xy = list(sexpr.children(sexpr.child(n, "pts") or ["x"], "xy"))
                if len(xy) >= 2:
                    a = _key(float(xy[0][1]), float(xy[0][2]))
                    b = _key(float(xy[-1][1]), float(xy[-1][2]))
                    points |= {a, b}
                    segments.append((a, b))
            elif n[0] in ("label", "global_label", "hierarchical_label", "junction", "no_connect", "bus_entry"):
                x, y, _ = sexpr.at(n)
                points.add(_key(x, y))
            elif n[0] == "sheet":
                for sp in sexpr.children(n, "pin"):
                    x, y, _ = sexpr.at(sp)
                    points.add(_key(x, y))
        for s in self.symbols:
            for p in s.pins:
                points.add(_key(*_transform(p.x, p.y, s.node)))
        return points, segments

    def boxes(self) -> list[tuple[float, float, float, float]]:
        """Rough outlines of what is already drawn, in sheet millimetres."""
        out = []
        for s in self.symbols:
            if s.lib is None:
                continue
            pts = [_transform(x, y, s.node) for x, y in _graphics_points(s.lib)]
            pts += [_transform(p.x, p.y, s.node) for p in s.pins]
            if pts:
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                out.append((min(xs), min(ys), max(xs), max(ys)))
        nodes = list(self.doc[1:]) + [sexpr.parse(t) for t in self.new_items if not t.lstrip().startswith("(symbol")]
        for n in nodes:
            if not isinstance(n, list) or not n:
                continue
            if n[0] == "wire":
                xy = list(sexpr.children(sexpr.child(n, "pts") or ["x"], "xy"))
                if len(xy) >= 2:
                    (x1, y1), (x2, y2) = (float(xy[0][1]), float(xy[0][2])), (float(xy[-1][1]), float(xy[-1][2]))
                    out.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
            elif n[0] in ("label", "global_label", "hierarchical_label", "text"):
                x, y, a = sexpr.at(n)
                length = 1.0 * len(str(n[1])) + 2
                dx, dy = round(math.cos(math.radians(a))), -round(math.sin(math.radians(a)))
                just = " ".join(str(v) for v in (sexpr.child(sexpr.child(n, "effects") or ["x"], "justify") or [])[1:])
                sign = -1 if "right" in just else 1
                ex, ey = x + dx * length * sign, y + dy * length * sign
                out.append((min(x, ex) - 1, min(y, ey) - 1.5, max(x, ex) + 1, max(y, ey) + 1.5))
            elif n[0] == "sheet":
                x, y, _ = sexpr.at(n)
                size = sexpr.child(n, "size")
                if size:
                    out.append((x, y, x + float(size[1]), y + float(size[2])))
        return out

    def paper(self) -> tuple[float, float]:
        p = sexpr.child(self.doc, "paper")
        if p and len(p) > 1:
            if p[1] == "User" and len(p) >= 4:
                w, h = float(p[2]), float(p[3])
            else:
                w, h = PAPER.get(p[1], PAPER["A4"])
            if "portrait" in p:
                w, h = h, w
            return w, h
        return PAPER["A4"]

    def find(self, ref: str, pin_number: str) -> tuple[_Placed, _LibPin]:
        units = [s for s in self.symbols if ref in s.refs]
        for s in units:
            for p in s.pins:
                if p.number == pin_number:
                    return s, p
        raise PlaceError(f"{ref} pin {pin_number} is not on sheet {self.human}")

    # ---- writing

    def save(self) -> bool:
        if not (self.new_items or self.new_lib):
            return False
        text = self.text
        spans = sexpr.child_spans(text)
        if self.new_lib:
            lib_span = next(((s, e) for s, e in spans if _head(text, s) == "lib_symbols"), None)
            blocks = "".join("\n\t\t" + _indent(b, 1) for b in self.new_lib)
            if lib_span:
                close = lib_span[1] - 1
                text = text[:close].rstrip() + blocks + "\n\t" + text[close:]
            else:
                after = max((e for s, e in spans if _head(text, s) in ("version", "generator", "generator_version", "uuid", "paper", "title_block")), default=None)
                at = after if after is not None else len("(kicad_sch")
                text = text[:at] + "\n\t(lib_symbols" + blocks + "\n\t)" + text[at:]
            spans = sexpr.child_spans(text)
        stop = next((s for s, e in spans if _head(text, s) in ("sheet", "sheet_instances", "embedded_fonts")), None)
        if stop is None:
            stop = text.rstrip().rfind(")")
            items = "".join("\t" + it + "\n" for it in self.new_items)
            text = text[:stop].rstrip("\n\t ") + "\n" + items + text[stop:]
        else:
            items = "".join(it + "\n\t" for it in self.new_items)
            text = text[:stop] + items + text[stop:]
        _write_atomic(self.path, text)
        self.text = text
        self.new_items, self.new_lib = [], []
        self.reparse()
        return True


def open_sheets(root: Path, project_name: str) -> list[Sheet]:
    """Every sheet instance of the design (a sheet used twice appears twice)."""
    sch = load_schematic(root)
    out = []
    for s in sch.sheets:
        path = root.parent / s["file"]
        out.append(Sheet(path, path.read_text(encoding="utf-8"), s["instance"], s["path"], project_name))
    return out


def pick_sheet(sheets: list[Sheet], sheet: str | None) -> Sheet:
    want = (sheet or "/").strip()
    if not want.startswith("/"):
        want = "/" + want
    if not want.endswith("/"):
        want += "/"
    match = [s for s in sheets if s.human.lower() == want.lower()]
    if not match:
        raise PlaceError(f"no sheet {want!r}; sheets are {', '.join(s.human for s in sheets)}")
    target = match[0]
    if sum(s.path == target.path for s in sheets) > 1:
        raise PlaceError(
            f"sheet {want} uses a file ({target.path.name}) that appears more than once in the design; "
            "place parts on reused sheets in KiCad"
        )
    return target


# --------------------------------------------------------------------------- placing symbols


def _sym_node(lib_id: str, x: float, y: float, angle: int, mirror: str | None) -> list:
    node: list = ["symbol", ["lib_id", lib_id], ["at", _f(x), _f(y), str(angle)]]
    if mirror:
        node.append(["mirror", mirror])
    return node


def _bbox(lsym: list, unit: int, x: float, y: float, angle: int, mirror: str | None, pad: float) -> tuple:
    node = _sym_node("", x, y, angle, mirror)
    pts = [_transform(px, py, node) for px, py in _graphics_points(lsym)]
    pts += [_transform(p.x, p.y, node) for p in _lib_pins(lsym) if p.unit in (0, unit)]
    if not pts:
        pts = [(x, y)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


def _overlaps(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def free_spot(sheet: Sheet, lsym: list, unit: int, angle: int, mirror: str | None, near: tuple | None = None) -> tuple[float, float]:
    """A grid position where the symbol (plus room for wires and labels) overlaps nothing."""
    taken = sheet.boxes()
    w, h = sheet.paper()
    margin, step = 20.32, 5.08
    cands = []
    y = margin + 10.16
    while y < h - margin:
        x = margin + 5.08
        while x < w - margin:
            cands.append((_snap(x, 2.54), _snap(y, 2.54)))
            x += step
        y += step
    if near is None and taken:
        # by default, start just right of what is already drawn
        near = (max(t[2] for t in taken) + 15, min(t[1] for t in taken))
    if near:
        cands.sort(key=lambda c: (c[0] - near[0]) ** 2 + (c[1] - near[1]) ** 2)
    for cx, cy in cands:
        box = _bbox(lsym, unit, cx, cy, angle, mirror, 7.62)
        if box[0] < margin * 0.5 or box[1] < margin * 0.5 or box[2] > w - margin * 0.5 or box[3] > h - margin * 0.5:
            continue
        if not any(_overlaps(box, t) for t in taken):
            return cx, cy
    # sheet is full: go right of everything
    right = max((t[2] for t in taken), default=margin)
    return _snap(right + 25.4, 2.54), _snap(margin + 25.4, 2.54)


def next_reference(sheets: list[Sheet], prefix: str) -> str:
    used = set()
    for s in sheets:
        for p in s.symbols:
            used |= p.refs
    pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    nums = [int(m.group(1)) for r in used if (m := pat.match(r))]
    return f"{prefix}{max(nums, default=0) + 1}"


def _lib_ref_prefix(lsym: list) -> str:
    ref = sexpr.properties(lsym).get("Reference", "U") or "U"
    return re.sub(r"[?\d]+$", "", ref) or "U"


def ensure_lib_symbol(sheet: Sheet, folder: Path, lib_id: str) -> list:
    if lib_id in sheet.lib:
        return sheet.lib[lib_id]
    block = library_symbol(folder, lib_id)
    node = sexpr.parse(block)
    sheet.new_lib.append(block)
    sheet.lib[lib_id] = node
    return node


def symbol_text(
    sheet: Sheet,
    lsym_text_or_node: list,
    lib_id: str,
    ref: str,
    x: float,
    y: float,
    angle: int,
    mirror: str | None,
    unit: int,
    fields: dict[str, str],
) -> str:
    lsym = lsym_text_or_node
    block_lines = [f"(symbol\n\t\t(lib_id {sexpr.quote(lib_id)})\n\t\t(at {_f(x)} {_f(y)} {angle})"]
    if mirror:
        block_lines.append(f"\t\t(mirror {mirror})")
    flags = {k: ("yes" if sexpr.value(lsym, k, "yes") != "no" else "no") for k in ("in_bom", "on_board")}
    block_lines.append(
        f"\t\t(unit {unit})\n\t\t(exclude_from_sim {sexpr.value(lsym, 'exclude_from_sim', 'no') or 'no'})"
        f"\n\t\t(in_bom {flags['in_bom']})\n\t\t(on_board {flags['on_board']})\n\t\t(dnp no)"
        f"\n\t\t(fields_autoplaced yes)\n\t\t(uuid {sexpr.quote(_uuid())})"
    )
    node = _sym_node(lib_id, x, y, angle, mirror)
    for prop in sexpr.children(lsym, "property"):
        name, value = prop[1], prop[2] if len(prop) > 2 else ""
        if name.startswith("ki_"):
            continue
        if name == "Reference":
            value = ref
        px, py, pa = sexpr.at(prop)
        sx, sy = _transform(px, py, node)
        text_angle = int((pa + angle) % 180)
        effects = sexpr.child(prop, "effects")
        hide = sexpr.flag(prop, "hide") or (effects is not None and sexpr.flag(effects, "hide"))
        just = sexpr.child(effects, "justify") if effects else None
        just_txt = ""
        if just and len(just) > 1:
            just_txt = "\n\t\t\t\t(justify " + " ".join(str(v) for v in just[1:] if isinstance(v, str)) + ")"
        block_lines.append(
            f"\t\t(property {sexpr.quote(name)} {sexpr.quote(value)}\n\t\t\t(at {_f(sx)} {_f(sy)} {text_angle})"
            + ("\n\t\t\t(hide yes)" if hide else "")
            + f"\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t){just_txt}\n\t\t\t)\n\t\t)"
        )
    style = 1
    numbers = []
    for p in _lib_pins(lsym):
        if p.unit in (0, unit) and p.style in (0, style) and p.number not in numbers:
            numbers.append(p.number)
    for n in numbers:
        block_lines.append(f"\t\t(pin {sexpr.quote(n)}\n\t\t\t(uuid {sexpr.quote(_uuid())})\n\t\t)")
    block_lines.append(
        f"\t\t(instances\n\t\t\t(project {sexpr.quote(sheet.project_name)}\n\t\t\t\t(path {sexpr.quote(sheet.instance)}"
        f"\n\t\t\t\t\t(reference {sexpr.quote(ref)})\n\t\t\t\t\t(unit {unit})\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)"
    )
    text = "\n".join(block_lines)
    if fields:
        text = _edit_symbol(text, fields)[0]
    return text


def place(
    sheets: list[Sheet],
    sheet: Sheet,
    folder: Path,
    lib_id: str,
    ref: str | None = None,
    x: float | None = None,
    y: float | None = None,
    angle: int = 0,
    mirror: str | None = None,
    unit: int = 1,
    fields: dict[str, str] | None = None,
    near: tuple | None = None,
    exact: bool = False,
) -> dict:
    angle = int(angle) % 360
    if angle not in (0, 90, 180, 270):
        raise PlaceError("angle must be 0, 90, 180 or 270")
    if mirror not in (None, "", "x", "y"):
        raise PlaceError('mirror must be "x" or "y"')
    lsym = ensure_lib_symbol(sheet, folder, lib_id)
    units = max([p.unit for p in _lib_pins(lsym)] + [1])
    if not 1 <= unit <= units:
        raise PlaceError(f"{lib_id} has {units} unit(s); unit {unit} does not exist")
    if ref:
        ref = ref.strip()
        for s in sheets:
            for p in s.symbols:
                if ref in p.refs and not (
                    int(sexpr.value(p.node, "unit", "1") or 1) != unit and sexpr.value(p.node, "lib_id") == lib_id
                ):
                    raise PlaceError(f"reference {ref} is already used")
    else:
        ref = next_reference(sheets, _lib_ref_prefix(lsym))
    warnings = []
    if x is None or y is None:
        x, y = free_spot(sheet, lsym, unit, angle, mirror or None, near)
    elif exact:
        x, y = round(x, 4), round(y, 4)
    else:
        x, y = _snap(x), _snap(y)
        box = _bbox(lsym, unit, x, y, angle, mirror or None, 1.27)
        if any(_overlaps(box, t) for t in sheet.boxes()):
            warnings.append("it overlaps something already on the sheet; move it in KiCad if needed")
    text = symbol_text(sheet, lsym, lib_id, ref, x, y, angle, mirror or None, unit, dict(fields or {}))
    # pins of the new part must not land on existing wires or pins
    node = sexpr.parse(text)
    points, segments = sheet.connection_points()
    for p in _lib_pins(lsym):
        if exact or p.unit not in (0, unit):
            continue
        k = _key(*_transform(p.x, p.y, node))
        if k in points or any(_on_segment(k, a, b) for a, b in segments):
            raise PlaceError(
                f"at ({_f(x)}, {_f(y)}) pin {p.number} of {ref} would touch an existing wire or pin; pick another spot"
            )
    sheet.new_items.append(text)
    sheet._add_placed(node)
    result = {"ref": ref, "symbol": lib_id, "sheet": sheet.human, "at": [x, y], "angle": angle, "unit": unit}
    if mirror:
        result["mirror"] = mirror
    if warnings:
        result["warnings"] = warnings
    return result


# --------------------------------------------------------------------------- wiring


_DIRS = {(1, 0): (0, "left bottom"), (-1, 0): (180, "right bottom"), (0, -1): (90, "left bottom"), (0, 1): (270, "right bottom")}


def pin_geometry(placed: _Placed, pin: _LibPin) -> tuple[tuple[float, float], tuple[int, int]]:
    """Sheet position of a pin's connection point and the direction pointing away from its part."""
    x, y = _transform(pin.x, pin.y, placed.node)
    lib_pin = _find_lib_pin_node(placed.lib, pin)
    a = math.radians(sexpr.at(lib_pin)[2]) if lib_pin else 0.0
    # the pin's angle points from its connection point into the part body
    ox, oy = -math.cos(a), -math.sin(a)
    tx, ty = _transform(pin.x + ox, pin.y + oy, placed.node)
    d = (round(tx - x), round(ty - y))
    if d not in _DIRS:
        d = (-1, 0)
    return (x, y), d


def _find_lib_pin_node(lsym: list | None, pin: _LibPin) -> list | None:
    if lsym is None:
        return None
    for sub in sexpr.children(lsym, "symbol"):
        for p in sexpr.children(sub, "pin"):
            num = sexpr.child(p, "number")
            if num and num[1] == pin.number:
                x, y, _ = sexpr.at(p)
                if abs(x - pin.x) < 1e-6 and abs(y - pin.y) < 1e-6:
                    return p
    return None


def stub_end(sheet: Sheet, start: tuple[float, float], d: tuple[int, int], extra: set | None = None) -> tuple[float, float]:
    """End of a short wire leaving ``start`` in direction ``d`` that touches nothing else."""
    points, segments = sheet.connection_points()
    points = points | (extra or set())
    s = _key(*start)
    for length in (2.54, 5.08, 7.62, 10.16, 12.7):
        end = (start[0] + d[0] * length, start[1] + d[1] * length)
        e = _key(*end)
        if e in points or any(_on_segment(e, a, b) for a, b in segments):
            continue
        if any(p != s and p != e and _on_segment(p, s, e) for p in points):
            continue
        if any(_on_segment(a, s, e) or _on_segment(b, s, e) for a, b in segments):
            continue
        return end
    raise PlaceError(
        f"no clear room to wire the pin at ({_f(start[0])}, {_f(start[1])}); something is drawn right next to it"
    )


def wire_text(a: tuple[float, float], b: tuple[float, float]) -> str:
    return (
        f"(wire\n\t\t(pts\n\t\t\t(xy {_f(a[0])} {_f(a[1])}) (xy {_f(b[0])} {_f(b[1])})\n\t\t)"
        f"\n\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n\t\t(uuid {sexpr.quote(_uuid())})\n\t)"
    )


def label_text(kind: str, name: str, at: tuple[float, float], d: tuple[int, int], shape: str = "passive") -> str:
    angle, just = _DIRS[d]
    x, y = _f(at[0]), _f(at[1])
    if kind == "global_label":
        just = just.split()[0]
        return (
            f"(global_label {sexpr.quote(name)}\n\t\t(shape {shape})\n\t\t(at {x} {y} {angle})\n\t\t(fields_autoplaced yes)"
            f"\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify {just})\n\t\t)"
            f"\n\t\t(uuid {sexpr.quote(_uuid())})"
            f'\n\t\t(property "Intersheetrefs" "${{INTERSHEET_REFS}}"\n\t\t\t(at {x} {y} 0)'
            f"\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t\t(justify {just})\n\t\t\t\t(hide yes)\n\t\t\t)\n\t\t)\n\t)"
        )
    return (
        f"(label {sexpr.quote(name)}\n\t\t(at {x} {y} {angle})\n\t\t(fields_autoplaced yes)"
        f"\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify {just})\n\t\t)"
        f"\n\t\t(uuid {sexpr.quote(_uuid())})\n\t)"
    )


def no_connect_text(at: tuple[float, float]) -> str:
    return f"(no_connect\n\t\t(at {_f(at[0])} {_f(at[1])})\n\t\t(uuid {sexpr.quote(_uuid())})\n\t)"


def power_symbol_id(net: str) -> str | None:
    """The stock power symbol named like ``net`` (GND, +3V3, VCC, ...), if KiCad has one."""
    try:
        chain = libraries.stock_symbol_blocks(f"power:{net}")
    except libraries.LibraryError:
        return None
    flat = sexpr.parse(_flatten(chain, "power"))
    if not sexpr.flag(flat, "power") or sexpr.properties(flat).get("Value") not in (net, None):
        return None
    pins = _lib_pins(flat)
    if len(pins) != 1 or abs(pins[0].x) > 1e-6 or abs(pins[0].y) > 1e-6:
        return None
    return f"power:{net}"


def power_angle(lsym: list, d: tuple[int, int]) -> int:
    """Rotation that points a power symbol's drawing away from the pin it hangs on."""
    pts = _graphics_points(lsym)
    if not pts:
        return 0
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    best, best_dot = 0, -1e9
    for angle in (0, 90, 180, 270):
        node = _sym_node("", 0, 0, angle, None)
        tx, ty = _transform(cx, cy, node)
        dot = tx * d[0] + ty * d[1]
        if dot > best_dot + 1e-9:
            best, best_dot = angle, dot
    return best


def locate(sheets: list[Sheet], ref: str, number: str) -> tuple[Sheet, _Placed, _LibPin]:
    found = [(sh, s, p) for sh in sheets for s in sh.symbols if ref in s.refs for p in s.pins if p.number == number]
    if not found:
        raise PlaceError(f"{ref} pin {number} is not drawn on any sheet (if it belongs to another unit, place that unit first)")
    sh = found[0][0]
    if sum(other.path == sh.path for other in sheets) > 1:
        raise PlaceError(f"{ref} sits on a sheet used more than once in the design; wire it in KiCad")
    return found[0]


def global_names(sheets: list[Sheet]) -> set[str]:
    """Names of nets that reach every sheet: global labels and power symbols."""
    names: set[str] = set()
    for sh in sheets:
        names |= {n[1] for n in sexpr.children(sh.doc, "global_label")}
        for s in sh.symbols:
            if s.lib is not None and sexpr.flag(s.lib, "power"):
                names.add(sexpr.properties(s.node).get("Value", ""))
    names.discard("")
    return names


def connect(sheets: list[Sheet], folder: Path, targets: list[tuple[str, str]], net: str) -> dict:
    """Join pins to ``net`` with a wire stub and a label or power symbol at each pin."""
    net = net.strip()
    if not net or any(c in net for c in "{}") or net.startswith(("Net-(", "unconnected-(")):
        raise PlaceError(f"{net!r} is not a usable net name")
    locs = [locate(sheets, ref, num) for ref, num in targets]
    power = power_symbol_id(net)
    multi = len({sh.path for sh, _, _ in locs}) > 1
    mode = "power symbol" if power else ("global label" if multi or net in global_names(sheets) else "label")
    made = []
    for (sh, placed, pin), (ref, num) in zip(locs, targets):
        if pin.hidden:
            raise PlaceError(f"{ref} pin {num} is hidden; it already joins the {pin.name or num} net by itself")
        start, d = pin_geometry(placed, pin)
        end = stub_end(sh, start, d)
        sh.new_items.append(wire_text(start, end))
        item = {"pin": f"{ref}.{num}", "sheet": sh.human}
        if power:
            lsym = ensure_lib_symbol(sh, folder, power)
            res = place(sheets, sh, folder, power, x=end[0], y=end[1], angle=power_angle(lsym, d), exact=True)
            item["power_symbol"] = res["ref"]
        else:
            kind = "global_label" if mode == "global label" else "label"
            shape = {"input": "input", "output": "output", "bidirectional": "bidirectional", "tri_state": "tri_state"}
            sh.new_items.append(label_text(kind, net, end, d, shape.get(pin.type, "passive")))
        made.append(item)
    return {"net": net, "connected_with": mode, "pins": made}


def mark_no_connect(sheets: list[Sheet], targets: list[tuple[str, str]]) -> list[str]:
    done = []
    for ref, num in targets:
        sh, placed, pin = locate(sheets, ref, num)
        (x, y), _ = pin_geometry(placed, pin)
        sh.new_items.append(no_connect_text((x, y)))
        done.append(f"{ref}.{num}")
    return done


# --------------------------------------------------------------------------- checking an edit


def base_name(net: str | None) -> str:
    """'/Power/VOUT' -> 'VOUT'; global names are unchanged."""
    return (net or "").rstrip("/").rsplit("/", 1)[-1] if (net or "").startswith("/") else (net or "")


def is_named(net: str | None) -> bool:
    return bool(net) and not net.startswith(("Net-(", "unconnected-("))


def net_groups(root: Path) -> dict[tuple[str, str], tuple[str, frozenset]]:
    """Every pin mapped to (its net name, every pin on that net), from the built-in netlist."""
    sch = load_schematic(root)
    out = {}
    for name, net in sch.nets.items():
        group = frozenset(net.nodes)
        for node in group:
            out[node] = (name, group)
    return out


def check_unchanged(before: dict, after: dict, expected: set) -> list[str]:
    """Problems where nets other than ``expected`` (pins allowed to change) were touched."""
    problems = []
    for pin, (name, group) in before.items():
        if pin in expected:
            continue
        new = after.get(pin)
        if new is None:
            problems.append(f"{pin[0]}.{pin[1]} disappeared")
        elif new[1] != group:
            joined = sorted(f"{r}.{n}" for r, n in new[1] - group)
            problems.append(f"{pin[0]}.{pin[1]} ({name}) would also join {', '.join(joined[:6])}")
    return problems
