"""Purchase-ready bill of materials from the schematic's fields."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import distributors
from .schematic import Component, Schematic, _natural_key

# Field names people (and vendor libraries) commonly use, normalised to lowercase without separators.
ALIASES = {
    "mpn": ["mpn", "manufacturerpartnumber", "mfrpn", "mfrpartnumber", "manufacturerpn", "partnumber", "pn"],
    "manufacturer": ["manufacturer", "mfr", "manufacturername", "mfg"],
    "mouser": ["mouserpn", "mouser", "mouserpartnumber", "mouserpart", "mouserno"],
    "digikey": ["digikeypn", "digikey", "digikeypartnumber", "digikeypart", "dkpn"],
    "mouser_url": ["mouserurl", "mouserlink"],
    "digikey_url": ["digikeyurl", "digikeylink"],
}

# Field names this server writes.
FIELD_NAMES = {
    "mpn": "MPN",
    "manufacturer": "Manufacturer",
    "mouser": "Mouser PN",
    "mouser_url": "Mouser URL",
    "digikey": "DigiKey PN",
    "digikey_url": "DigiKey URL",
}


def _norm(name: str) -> str:
    return re.sub(r"[\s_\-.]", "", name.lower())


def get_field(comp: Component, key: str) -> str:
    by_norm = {_norm(k): v for k, v in comp.fields.items() if v and v != "~"}
    for alias in ALIASES[key]:
        if alias in by_norm:
            return by_norm[alias].strip()
    return ""


@dataclass
class BomLine:
    refs: list[str]
    value: str
    footprint: str
    description: str
    datasheet: str
    manufacturer: str
    mpn: str
    mouser: str
    digikey: str
    dnp: bool
    pricing: dict = field(default_factory=dict)

    @property
    def qty(self) -> int:
        return len(self.refs)


def collect(sch: Schematic) -> list[BomLine]:
    groups: dict[tuple, BomLine] = {}
    for comp in sch.components.values():
        if not comp.in_bom:
            continue
        line = BomLine(
            refs=[comp.ref],
            value=comp.value,
            footprint=comp.footprint,
            description=comp.description,
            datasheet="" if comp.datasheet == "~" else comp.datasheet,
            manufacturer=get_field(comp, "manufacturer"),
            mpn=get_field(comp, "mpn"),
            mouser=get_field(comp, "mouser"),
            digikey=get_field(comp, "digikey"),
            dnp=comp.dnp,
        )
        key = (line.value, line.footprint, line.mpn, line.manufacturer, line.mouser, line.digikey, line.dnp)
        if key in groups:
            groups[key].refs.append(comp.ref)
        else:
            groups[key] = line
    lines = list(groups.values())
    for line in lines:
        line.refs.sort(key=_natural_key)
    lines.sort(key=lambda b: (b.dnp, _natural_key(b.refs[0])))
    return lines


def _price(line: BomLine, qty: int, cache: dict) -> dict:
    """Best offer for one line from the configured distributors (by its own PNs, else its MPN)."""
    lookups: list[tuple[str, str]] = []
    if line.mouser:
        lookups.append(("mouser", line.mouser))
    if line.digikey:
        lookups.append(("digikey", line.digikey))
    if not lookups and line.mpn:
        lookups = [(c.name, line.mpn) for c in distributors.configured()]
    offers, errors = [], []
    for source, pn in lookups:
        key = (source, pn)
        if key not in cache:
            try:
                cache[key] = distributors.lookup(pn, source)
            except distributors.DistributorError as e:
                cache[key] = e
        found = cache[key]
        if isinstance(found, Exception):
            errors.append(f"{source}: {found}")
            continue
        for part in found[:1]:
            pb = part.unit_price(qty)
            if pb is None:
                continue
            offers.append(
                {
                    "source": source,
                    "distributor_pn": part.distributor_pn,
                    "unit_price": pb.unit_price,
                    "currency": pb.currency,
                    "extended": round(pb.unit_price * qty, 4),
                    "stock": part.stock,
                    "enough_stock": part.stock is None or part.stock >= qty,
                    "url": part.product_url,
                }
            )
    best = min(
        offers, key=lambda o: (not o["enough_stock"], o["extended"]), default=None
    )
    return {"best": best, "offers": offers, "errors": errors}


COLUMNS = [
    "Item", "Qty per board", "Qty to order", "References", "Value", "Footprint", "Manufacturer", "MPN",
    "Mouser PN", "DigiKey PN", "Description", "Datasheet", "Supplier", "Supplier PN", "Unit price",
    "Extended price", "Currency", "Stock", "Supplier link",
]  # fmt: skip


def export(
    sch: Schematic, out_path: Path, boards: int = 1, spare_percent: float = 0, with_pricing: bool = False
) -> dict:
    lines = collect(sch)
    cache: dict = {}
    rows = []
    totals: dict[str, float] = {}
    for i, line in enumerate([ln for ln in lines if not ln.dnp], start=1):
        qty = line.qty * boards
        qty = int(-(-qty * (1 + spare_percent / 100) // 1))
        best = None
        if with_pricing:
            line.pricing = _price(line, qty, cache)
            best = line.pricing["best"]
            if best:
                totals[best["currency"]] = round(totals.get(best["currency"], 0) + best["extended"], 2)
        supplier = [best[k] for k in ("source", "distributor_pn", "unit_price", "extended", "currency", "stock", "url")] if best else [""] * 7
        rows.append(
            [i, line.qty, qty, ", ".join(line.refs), line.value, line.footprint, line.manufacturer, line.mpn]
            + [line.mouser, line.digikey, line.description, line.datasheet, *supplier]
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM-marked UTF-8 opens cleanly in Excel
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)

    fitted = [ln for ln in lines if not ln.dnp]
    issues = {
        "missing_mpn": [", ".join(ln.refs) for ln in fitted if not ln.mpn],
        "missing_distributor_pn": [", ".join(ln.refs) for ln in fitted if not (ln.mouser or ln.digikey)],
        "missing_footprint": [", ".join(ln.refs) for ln in fitted if not ln.footprint],
    }
    result: dict = {
        "file": str(out_path),
        "boards": boards,
        "lines": len(rows),
        "parts_per_board": sum(ln.qty for ln in fitted),
        "dnp": [", ".join(ln.refs) for ln in lines if ln.dnp],
        "issues": {k: v for k, v in issues.items() if v},
    }
    if with_pricing:
        result["total"] = totals
        problems = [
            {"refs": ", ".join(ln.refs), "errors": ln.pricing["errors"]}
            for ln in fitted
            if ln.pricing.get("errors") or not ln.pricing.get("best")
        ]
        if problems:
            result["pricing_problems"] = problems
        short = [", ".join(ln.refs) for ln in fitted if ln.pricing.get("best") and not ln.pricing["best"]["enough_stock"]]
        if short:
            result["not_enough_stock"] = short
    return result
