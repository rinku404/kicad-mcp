"""Read KiCad boards (.kicad_pcb): footprints, pads, nets and a board summary."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr
from .sexpr import Node


@dataclass
class Footprint:
    ref: str
    value: str
    footprint: str
    layer: str
    x: float
    y: float
    rotation: float
    attributes: list[str] = field(default_factory=list)
    pads: dict[str, str] = field(default_factory=dict)  # pad number -> net name

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "value": self.value,
            "footprint": self.footprint,
            "side": "bottom" if self.layer.startswith("B.") else "top",
            "x_mm": self.x,
            "y_mm": self.y,
            "rotation": self.rotation,
            "attributes": self.attributes,
            "pads": self.pads,
        }


@dataclass
class Board:
    path: Path
    footprints: dict[str, Footprint]
    copper_layers: list[str]
    nets: list[str]
    tracks: int
    vias: int
    zones: int
    outline_mm: tuple[float, float] | None  # width, height of the Edge.Cuts bounding box

    def summary(self) -> dict:
        return {
            "file": self.path.name,
            "footprints": len(self.footprints),
            "copper_layers": self.copper_layers,
            "nets": len(self.nets),
            "track_segments": self.tracks,
            "vias": self.vias,
            "zones": self.zones,
            "board_size_mm": (
                {"width": self.outline_mm[0], "height": self.outline_mm[1]} if self.outline_mm else None
            ),
            "sides": dict(Counter("bottom" if f.layer.startswith("B.") else "top" for f in self.footprints.values())),
        }


def _pad_net(pad: Node, numbered: dict[str, str]) -> str:
    n = sexpr.child(pad, "net")
    if n is None:
        return ""
    # KiCad <= 9: (net 3 "GND"); newer files may only carry the name
    if len(n) >= 3:
        return n[2]
    return numbered.get(n[1], n[1]) if len(n) == 2 else ""


def _xy(node: Node, tag: str) -> tuple[float, float] | None:
    c = sexpr.child(node, tag)
    if c is None or len(c) < 3:
        return None
    return float(c[1]), float(c[2])


def load_board(path: str | Path) -> Board:
    path = Path(path).resolve()
    doc = sexpr.parse(path.read_text(encoding="utf-8"))

    numbered = {n[1]: n[2] for n in sexpr.children(doc, "net") if len(n) >= 3}
    copper = [
        l[1]
        for l in (sexpr.child(doc, "layers") or ["layers"])[1:]
        if isinstance(l, list) and len(l) >= 3 and l[1].endswith(".Cu")
    ]

    footprints: dict[str, Footprint] = {}
    for fp in sexpr.children(doc, "footprint"):
        props = sexpr.properties(fp)
        ref = props.get("Reference", "")
        if not ref:  # KiCad 6/7 used fp_text for the reference
            for t in sexpr.children(fp, "fp_text"):
                if len(t) > 2 and t[1] == "reference":
                    ref = t[2]
        x, y, rot = sexpr.at(fp)
        attr = sexpr.child(fp, "attr")
        f = Footprint(
            ref=ref,
            value=props.get("Value", ""),
            footprint=fp[1] if len(fp) > 1 and isinstance(fp[1], str) else "",
            layer=sexpr.value(fp, "layer", "F.Cu") or "F.Cu",
            x=x,
            y=y,
            rotation=rot,
            attributes=[a for a in attr[1:] if isinstance(a, str)] if attr else [],
        )
        for pad in sexpr.children(fp, "pad"):
            net = _pad_net(pad, numbered)
            num = pad[1] if len(pad) > 1 and isinstance(pad[1], str) else ""
            if num and (num not in f.pads or net):
                f.pads[num] = net
        key, i = ref, 1
        while key in footprints:  # e.g. several "REF**" or mounting holes
            i += 1
            key = f"{ref}#{i}"
        footprints[key] = f

    xs: list[float] = []
    ys: list[float] = []
    for kind in ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly"):
        for g in sexpr.children(doc, kind):
            if sexpr.value(g, "layer") != "Edge.Cuts":
                continue
            for tag in ("start", "end", "mid", "center"):
                p = _xy(g, tag)
                if p:
                    xs.append(p[0])
                    ys.append(p[1])
            pts = sexpr.child(g, "pts")
            for xy in sexpr.children(pts, "xy") if pts else []:
                xs.append(float(xy[1]))
                ys.append(float(xy[2]))
    outline = (round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)) if xs else None

    net_names = set(numbered.values()) | {n for f in footprints.values() for n in f.pads.values()}
    net_names.discard("")
    return Board(
        path=path,
        footprints=footprints,
        copper_layers=copper,
        nets=sorted(net_names),
        tracks=sum(1 for _ in sexpr.children(doc, "segment")) + sum(1 for _ in sexpr.children(doc, "arc")),
        vias=sum(1 for _ in sexpr.children(doc, "via")),
        zones=sum(1 for _ in sexpr.children(doc, "zone")),
        outline_mm=outline,
    )
