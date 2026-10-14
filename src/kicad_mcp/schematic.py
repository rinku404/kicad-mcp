"""Read KiCad schematics (.kicad_sch) and work out components and nets.

This is a pure-Python reader so the server still works when ``kicad-cli`` is not
installed. When it is installed, :mod:`kicad_mcp.project` prefers the netlist
exported by KiCad itself, which is authoritative; this module then only
supplies component details.

Supported: hierarchical sheets (including sheets reused several times),
multi-unit symbols, local / global / hierarchical labels, power symbols,
hidden power pins, junctions and wires that end on another wire.
Not yet supported: buses (labels with bus syntax are treated as plain names).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr
from .sexpr import Node

_EPS = 1e-4  # mm; KiCad stores coordinates in 1e-4 mm units


@dataclass
class Pin:
    number: str
    name: str
    type: str
    unit: int = 0
    net: str | None = None


@dataclass
class Component:
    ref: str
    value: str
    lib_id: str
    footprint: str = ""
    datasheet: str = ""
    description: str = ""
    sheet: str = "/"
    fields: dict[str, str] = field(default_factory=dict)
    pins: dict[str, Pin] = field(default_factory=dict)
    units: list[int] = field(default_factory=list)
    in_bom: bool = True
    on_board: bool = True
    dnp: bool = False
    power: bool = False
    unit_count: int = 1

    def to_dict(self, with_pins: bool = True) -> dict:
        d = {
            "ref": self.ref,
            "value": self.value,
            "lib_id": self.lib_id,
            "footprint": self.footprint,
            "datasheet": self.datasheet,
            "description": self.description,
            "sheet": self.sheet,
            "dnp": self.dnp,
            "in_bom": self.in_bom,
            "on_board": self.on_board,
        }
        extra = {
            k: v
            for k, v in self.fields.items()
            if k not in ("Reference", "Value", "Footprint", "Datasheet", "Description") and v
        }
        if extra:
            d["fields"] = extra
        if with_pins:
            d["pins"] = [
                {"number": p.number, "name": p.name, "type": p.type, "net": p.net}
                for p in sorted(self.pins.values(), key=lambda p: _natural_key(p.number))
            ]
        return d


@dataclass
class Net:
    name: str
    nodes: list[tuple[str, str]] = field(default_factory=list)  # (ref, pin number)


@dataclass
class Schematic:
    root: Path
    title: str
    components: dict[str, Component]
    nets: dict[str, Net]
    sheets: list[dict]
    no_connects: list[tuple[str, str]]  # pins explicitly marked "no connect"
    warnings: list[str]


def _natural_key(s: str):
    import re

    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# net-name driver priorities, as in KiCad's connection graph
P_SHEET_PIN, P_HIER_LABEL, P_LOCAL_LABEL, P_LOCAL_POWER, P_GLOBAL_POWER, P_GLOBAL = 2, 3, 4, 5, 6, 7


def _escape(name: str) -> str:
    return name.replace("/", "{slash}")


def _local_name(sheet: str, text: str) -> str:
    return sheet.rstrip("/") + "/" + text


def _pick_name(candidates: list[tuple[int, int, str]]) -> str:
    """Strongest driver wins; ties go to the one highest in the hierarchy, then by name."""
    return min(candidates, key=lambda c: (-c[0], c[1], c[2]))[2]


# --------------------------------------------------------------------------- geometry


def _key(x: float, y: float) -> tuple[int, int]:
    return round(x / _EPS), round(y / _EPS)


def _transform(px: float, py: float, sym: Node) -> tuple[float, float]:
    """Map a pin position from symbol-library space to sheet space."""
    x0, y0, angle = sexpr.at(sym)
    # library Y axis points up, schematic Y axis points down
    x, y = px, -py
    a = math.radians(angle)
    c, s = round(math.cos(a)), round(math.sin(a))
    x, y = x * c + y * s, -x * s + y * c
    mirror = sexpr.value(sym, "mirror")
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    return x0 + x, y0 + y


def _on_segment(p: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> bool:
    (px, py), (ax, ay), (bx, by) = p, a, b
    if (bx - ax) * (py - ay) - (by - ay) * (px - ax) != 0:
        return False
    return min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


# --------------------------------------------------------------------------- library symbols


@dataclass
class _LibPin:
    number: str
    name: str
    type: str
    x: float
    y: float
    hidden: bool
    unit: int
    style: int


def _lib_pins(lib_sym: Node) -> list[_LibPin]:
    pins: list[_LibPin] = []
    for sub in sexpr.children(lib_sym, "symbol"):
        # sub-symbol names look like "Name_<unit>_<bodystyle>"
        parts = sub[1].rsplit("_", 2)
        try:
            unit, style = int(parts[-2]), int(parts[-1])
        except (ValueError, IndexError):
            unit, style = 0, 0
        for p in sexpr.children(sub, "pin"):
            x, y, _ = sexpr.at(p)
            nm = sexpr.child(p, "name")
            num = sexpr.child(p, "number")
            pins.append(
                _LibPin(
                    number=num[1] if num else "",
                    name=nm[1] if nm and nm[1] != "~" else "",
                    type=p[1] if len(p) > 1 and isinstance(p[1], str) else "unspecified",
                    x=x,
                    y=y,
                    hidden=sexpr.flag(p, "hide"),
                    unit=unit,
                    style=style,
                )
            )
    return pins


# --------------------------------------------------------------------------- loader


class _Loader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache: dict[Path, Node] = {}
        self.uf = _UnionFind()
        self.components: dict[str, Component] = {}
        self.sheets: list[dict] = []
        self.warnings: list[str] = []
        self.no_connects: list[tuple[str, str]] = []
        # union-find node -> label names attached to it, for naming nets
        self.names: dict = {}
        self.pin_nodes: dict = {}  # pin node id -> (ref, number)
        self.root_uuid = ""
        self.local_labels: set = set()

    def doc(self, path: Path) -> Node:
        path = path.resolve()
        if path not in self.cache:
            self.cache[path] = sexpr.parse(path.read_text(encoding="utf-8"))
        return self.cache[path]

    def name_node(self, node, priority: int, depth: int, name: str) -> None:
        self.names.setdefault(node, []).append((priority, depth, name))

    def load(self) -> Schematic:
        doc = self.doc(self.root)
        self.root_uuid = sexpr.value(doc, "uuid", "") or ""
        tb = sexpr.child(doc, "title_block")
        title = (sexpr.value(tb, "title", "") if tb else "") or self.root.stem
        self.load_sheet(self.root, "/" + self.root_uuid, "/", parent_pins=None, depth=0)
        # a local label named like a global net (power symbol or global label) joins it
        for key in self.local_labels:
            if ("gname", key[2]) in self.uf.parent:
                self.uf.union(key, ("gname", key[2]))
        nets = self.build_nets()
        return Schematic(
            root=self.root,
            title=title,
            components=self.components,
            nets=nets,
            sheets=self.sheets,
            no_connects=self.no_connects,
            warnings=self.warnings,
        )

    def load_sheet(
        self,
        path: Path,
        inst_path: str,
        human: str,
        parent_pins: dict[str, object] | None,
        depth: int,
    ) -> None:
        if depth > 32:
            self.warnings.append(f"sheet nesting too deep at {human}")
            return
        doc = self.doc(path)
        self.sheets.append({"path": human, "file": str(path.relative_to(self.root.parent))})
        scope = inst_path  # local labels are unique per sheet instance
        lib = {s[1]: s for s in sexpr.children(sexpr.child(doc, "lib_symbols") or ["lib_symbols"], "symbol")}

        points: list[tuple[tuple[int, int], object]] = []  # (position, union-find node)

        def point(x: float, y: float, node) -> None:
            k = _key(x, y)
            points.append((k, node))
            self.uf.find(node)

        # wires
        segments: list[tuple[tuple[int, int], tuple[int, int], object]] = []
        for i, w in enumerate(sexpr.children(doc, "wire")):
            pts = sexpr.child(w, "pts")
            xy = [c for c in sexpr.children(pts, "xy")] if pts else []
            if len(xy) < 2:
                continue
            node = ("wire", inst_path, i)
            a = _key(float(xy[0][1]), float(xy[0][2]))
            b = _key(float(xy[-1][1]), float(xy[-1][2]))
            segments.append((a, b, node))
            points.append((a, node))
            points.append((b, node))
            self.uf.find(node)

        for i, j in enumerate(sexpr.children(doc, "junction")):
            x, y, _ = sexpr.at(j)
            point(x, y, ("junction", inst_path, i))

        nc_points = {_key(*sexpr.at(n)[:2]) for n in sexpr.children(doc, "no_connect")}

        # labels; a local label and a hierarchical label with the same text on
        # the same sheet are the same net
        for kind in ("label", "hierarchical_label", "global_label"):
            for i, lab in enumerate(sexpr.children(doc, kind)):
                text = _escape(lab[1])
                x, y, _ = sexpr.at(lab)
                node = (kind, inst_path, i)
                point(x, y, node)
                if kind == "global_label":
                    self.uf.union(node, ("gname", text))
                    self.name_node(("gname", text), P_GLOBAL, depth, text)
                    continue
                key = ("lname", scope, text)
                self.uf.union(node, key)
                prio = P_LOCAL_LABEL if kind == "label" else P_HIER_LABEL
                self.name_node(key, prio, depth, _local_name(human, text))
                self.local_labels.add(key)
                if kind == "hierarchical_label" and parent_pins is not None:
                    if text in parent_pins:
                        self.uf.union(key, parent_pins[text])
                    else:
                        self.warnings.append(f"hierarchical label '{text}' in {human} has no matching sheet pin")

        # symbols
        for idx, sym in enumerate(sexpr.children(doc, "symbol")):
            self.add_symbol(sym, lib, inst_path, human, idx, point, nc_points, depth)

        # child sheets
        for sh in sexpr.children(doc, "sheet"):
            props = sexpr.properties(sh)
            sname = props.get("Sheetname") or props.get("Sheet name") or "sheet"
            sfile = props.get("Sheetfile") or props.get("Sheet file")
            suuid = sexpr.value(sh, "uuid", "") or sname
            child_inst = inst_path + "/" + suuid
            child_human = (human.rstrip("/") + "/" + sname + "/") if human != "/" else "/" + sname + "/"
            pins: dict[str, object] = {}
            for i, sp in enumerate(sexpr.children(sh, "pin")):
                x, y, _ = sexpr.at(sp)
                pname = _escape(sp[1])
                node = ("sheetpin", child_inst, pname)
                point(x, y, node)
                pins[pname] = node
                self.name_node(node, P_SHEET_PIN, depth, _local_name(human, pname))
            if not sfile:
                continue
            fpath = path.parent / sfile
            if not fpath.exists():
                fpath = self.root.parent / sfile
            if not fpath.exists():
                self.warnings.append(f"sheet file not found: {sfile}")
                continue
            self.load_sheet(fpath, child_inst, child_human, pins, depth + 1)

        # connect everything that touches on this sheet
        by_pos: dict[tuple[int, int], object] = {}
        for k, node in points:
            if k in by_pos:
                self.uf.union(by_pos[k], node)
            else:
                by_pos[k] = node
        for a, b, wnode in segments:
            for k, node in by_pos.items():
                if k != a and k != b and _on_segment(k, a, b):
                    self.uf.union(wnode, node)

    def add_symbol(self, sym, lib, inst_path, human, idx, point, nc_points, depth) -> None:
        lib_id = sexpr.value(sym, "lib_id", "") or ""
        lib_name = sexpr.value(sym, "lib_name") or lib_id
        lsym = lib.get(lib_name) or lib.get(lib_id)
        props = sexpr.properties(sym)
        ref = props.get("Reference", "?")
        unit = int(sexpr.value(sym, "unit", "1") or 1)
        # per-instance reference for reused sheets
        inst = sexpr.child(sym, "instances")
        if inst is not None:
            for proj in sexpr.children(inst, "project"):
                for p in sexpr.children(proj, "path"):
                    if p[1] == inst_path:
                        ref = sexpr.value(p, "reference", ref) or ref
                        unit = int(sexpr.value(p, "unit", str(unit)) or unit)
        if lsym is None:
            self.warnings.append(f"{ref}: library symbol {lib_id} missing from schematic cache")
            return
        is_power = sexpr.flag(lsym, "power")
        body_style = int(sexpr.value(sym, "body_style", None) or sexpr.value(sym, "convert", "1") or 1)
        value = props.get("Value", "")

        lpins = [
            p for p in _lib_pins(lsym) if p.unit in (0, unit) and p.style in (0, body_style)
        ]

        if is_power and all(p.type == "power_in" for p in lpins):
            # power symbols (GND, +5V, ...) name a net; KiCad 9 "local" power
            # symbols only do so within their sheet
            pw = sexpr.child(lsym, "power")
            local = pw is not None and "local" in pw[1:]
            value = _escape(value)
            key = ("lname", inst_path, value) if local else ("gname", value)
            for p in lpins:
                x, y = _transform(p.x, p.y, sym)
                node = ("pwrpin", inst_path, idx, p.number)
                point(x, y, node)
                self.uf.union(node, key)
            self.name_node(key, P_LOCAL_POWER if local else P_GLOBAL_POWER, depth, value)
            return
        if ref.startswith("#"):
            # virtual parts such as PWR_FLAG: they join what they touch but
            # are not real components and do not name the net
            for p in lpins:
                x, y = _transform(p.x, p.y, sym)
                node = ("pwrpin", inst_path, idx, p.number)
                point(x, y, node)
                self.hidden_power_pin(node, p, depth)
            return

        comp = self.components.get(ref)
        if comp is None:
            comp = Component(
                ref=ref,
                value=value,
                lib_id=lib_id,
                footprint=props.get("Footprint", ""),
                datasheet=props.get("Datasheet", ""),
                description=props.get("Description", ""),
                sheet=human,
                fields=props,
                in_bom=sexpr.value(sym, "in_bom", "yes") != "no",
                on_board=sexpr.value(sym, "on_board", "yes") != "no",
                dnp=sexpr.value(sym, "dnp", "no") == "yes",
                unit_count=max([p.unit for p in _lib_pins(lsym)] + [1]),
            )
            self.components[ref] = comp
        if unit not in comp.units:
            comp.units.append(unit)
        for p in lpins:
            x, y = _transform(p.x, p.y, sym)
            node = ("pin", ref, p.number)
            point(x, y, node)
            self.pin_nodes[node] = (ref, p.number)
            comp.pins[p.number] = Pin(number=p.number, name=p.name, type=p.type, unit=p.unit)
            self.hidden_power_pin(node, p, depth)
            if _key(x, y) in nc_points:
                self.no_connects.append((ref, p.number))

    def hidden_power_pin(self, node, p: _LibPin, depth: int) -> None:
        """Invisible power-input pins join the global net named after the pin."""
        if p.hidden and p.type == "power_in" and p.name:
            name = _escape(p.name)
            self.uf.union(node, ("gname", name))
            self.name_node(("gname", name), P_GLOBAL_POWER, depth, name)

    def default_name(self, nodes: list[tuple[str, str]]) -> str:
        """KiCad-style name for a net with no label, e.g. ``Net-(U1A-G)``."""

        def label(rn: tuple[str, str]) -> tuple[bool, str, str, str]:
            comp = self.components[rn[0]]
            pin = comp.pins[rn[1]]
            named = pin.name not in ("", "~", pin.number)
            ref = comp.ref
            if named and comp.unit_count > 1 and pin.unit > 0:
                ref += chr(ord("A") + pin.unit - 1)
            return not named, ref, _escape(pin.name) if named else "", pin.number

        _, ref, pin_name, num = min(label(rn) for rn in nodes)
        # stacked pins of one part (same name) still count as unconnected
        if len({(r, self.components[r].pins[n].name or n) for r, n in nodes}) == 1:
            mid = f"{pin_name}-" if pin_name else ""
            return f"unconnected-({ref}-{mid}Pad{num})"
        return f"Net-({ref}-{pin_name or 'Pad' + num})"

    def build_nets(self) -> dict[str, Net]:
        groups: dict = {}
        for node, (ref, num) in self.pin_nodes.items():
            # like KiCad's netlist, leave out parts excluded from the board
            if self.components[ref].on_board:
                groups.setdefault(self.uf.find(node), []).append((ref, num))
        group_names: dict = {}
        for node, names in self.names.items():
            group_names.setdefault(self.uf.find(node), []).extend(names)

        nets: dict[str, Net] = {}
        for root, nodes in groups.items():
            nodes.sort(key=lambda rn: (_natural_key(rn[0]), _natural_key(rn[1])))
            candidates = group_names.get(root)
            if candidates:
                name = _pick_name(candidates)
            else:
                name = self.default_name(nodes)
            base, i = name, 1
            while name in nets:
                i += 1
                name = f"{base}_{i}"
            nets[name] = Net(name=name, nodes=nodes)
            for r, n in nodes:
                self.components[r].pins[n].net = name
        return nets


def load_schematic(path: str | Path) -> Schematic:
    """Load a root schematic and every sheet below it."""
    return _Loader(Path(path).resolve()).load()
