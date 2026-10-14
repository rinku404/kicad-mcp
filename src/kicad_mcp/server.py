"""MCP server exposing a KiCad project to LLM clients."""

from __future__ import annotations

import functools
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, bom, config, datasheets, distributors, kicad_cli, libraries
from .project import Project, ProjectError
from .schematic import Component, _natural_key
from .schematic_edit import set_symbol_fields

INSTRUCTIONS = """\
Tools for reading, checking and sourcing parts for a KiCad 9/10 design.

Start with open_project (a .kicad_pro, .kicad_sch, .kicad_pcb or project folder);
every other tool then works on that project unless you pass project_path.
Files are re-read automatically when they change on disk, so save in KiCad
before asking about recent edits. Pins are written REF.PIN, for example U1.3
or U1.VCC (pin number or pin name).

Sourcing: search_parts / get_part find parts on Mouser and DigiKey; assign_part
writes MPN, manufacturer, distributor part numbers, links and the datasheet into
a schematic symbol (and saves the datasheet PDF in the project); export_bom
writes a purchase-ready CSV. For symbols and footprints, prefer KiCad's stock
libraries (find_library_symbols / find_library_footprints); otherwise import a
vendor ZIP (import_part_zip) or build a symbol from the datasheet pin table
(create_symbol). Tools that write files tell the user to reload in KiCad.
"""

mcp = MCPServer("kicad-mcp", instructions=INSTRUCTIONS, version=__version__)



def tool(fn):
    """Register an MCP tool whose expected failures reach the model as readable errors."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (
            ProjectError,
            kicad_cli.KiCadCliError,
            distributors.DistributorError,
            datasheets.DatasheetError,
            OSError,
            ValueError,
        ) as e:
            raise ToolError(str(e)) from e

    mcp.tool()(wrapper)
    return fn


_projects: dict[Path, Project] = {}
_current: Project | None = None


def _project(project_path: str | None = None) -> Project:
    global _current
    if project_path:
        proj = Project(project_path)
        key = proj.schematic_path or proj.board_path or proj.folder
        _projects.setdefault(key, proj)
        _current = _projects[key]
        return _current
    if _current is None:
        raise ProjectError("no project is open; call open_project first")
    return _current


def _component(proj: Project, ref: str) -> Component:
    comps = proj.schematic.components
    if ref in comps:
        return comps[ref]
    by_upper = {k.upper(): v for k, v in comps.items()}
    if ref.upper() in by_upper:
        return by_upper[ref.upper()]
    raise ProjectError(f"no component {ref!r} in the schematic")


def _pin(proj: Project, spec: str) -> tuple[Component, str]:
    """Resolve 'U1.3', 'U1:3' or 'U1.VCC' to (component, pin number)."""
    m = re.match(r"^\s*([^\s.:]+)\s*[.:\s]\s*(.+?)\s*$", spec)
    if not m:
        raise ProjectError(f"pin {spec!r} should look like REF.PIN, e.g. U1.3 or U1.VCC")
    comp = _component(proj, m.group(1))
    want = m.group(2)
    if want in comp.pins:
        return comp, want
    by_name = [p.number for p in comp.pins.values() if p.name.upper() == want.upper()]
    if len(by_name) == 1:
        return comp, by_name[0]
    if by_name:
        raise ProjectError(f"{comp.ref} has several pins named {want}: {', '.join(by_name)}; use a pin number")
    raise ProjectError(f"{comp.ref} has no pin {want!r}")


def _node(proj: Project, ref: str, num: str) -> dict:
    comp = proj.schematic.components.get(ref)
    pin = comp.pins.get(num) if comp else None
    return {
        "ref": ref,
        "pin": num,
        "pin_name": pin.name if pin else "",
        "pin_type": pin.type if pin else "",
        "value": comp.value if comp else "",
    }


@tool
def open_project(path: str) -> dict:
    """Open a KiCad project and return an overview of it.

    path: a .kicad_pro, .kicad_sch or .kicad_pcb file, or the project folder.
    """
    return _project(path).summary()


@tool
def project_summary(project_path: str | None = None) -> dict:
    """Overview of the open project: sheets, part and net counts, missing footprints or datasheets, board stats."""
    return _project(project_path).summary()


@tool
def list_components(query: str | None = None, project_path: str | None = None) -> list[dict]:
    """List schematic components (not power symbols).

    query: optional case-insensitive text matched against reference, value, footprint,
    library id, description and other fields (for example "U", "100n", "LM358", "0603").
    """
    proj = _project(project_path)
    rows = []
    q = query.lower() if query else None
    for comp in sorted(proj.schematic.components.values(), key=lambda c: _natural_key(c.ref)):
        d = comp.to_dict(with_pins=False)
        if q and q not in " ".join(str(v) for v in [*d.values(), *comp.fields.values()]).lower():
            continue
        rows.append(d)
    return rows


@tool
def get_component(ref: str, project_path: str | None = None) -> dict:
    """Everything about one component: fields, datasheet link, every pin with its net, and its PCB placement."""
    proj = _project(project_path)
    comp = _component(proj, ref)
    d = comp.to_dict(with_pins=True)
    no_connects = set(proj.schematic.no_connects)
    for p in d["pins"]:
        if p["net"]:
            peers = [
                f"{r}.{n}" for r, n in proj.schematic.nets[p["net"]].nodes if (r, n) != (comp.ref, p["number"])
            ]
            p["connected_to"] = peers[:25]
            if len(peers) > 25:
                p["connected_to_more"] = len(peers) - 25
        p["no_connect_flag"] = (comp.ref, p["number"]) in no_connects
    if proj.has_board():
        fp = proj.board.footprints.get(comp.ref)
        d["pcb"] = fp.to_dict() if fp else "not placed on the board"
    return d


@tool
def list_nets(query: str | None = None, project_path: str | None = None) -> list[dict]:
    """List nets with how many pins each connects and which parts are on it.

    query: optional case-insensitive filter on the net name (for example "GND", "SDA", "/").
    """
    proj = _project(project_path)
    rows = []
    for name in sorted(proj.schematic.nets, key=_natural_key):
        if query and query.lower() not in name.lower():
            continue
        net = proj.schematic.nets[name]
        refs = sorted({r for r, _ in net.nodes}, key=_natural_key)
        rows.append({"name": name, "pins": len(net.nodes), "parts": refs[:30], "more_parts": max(0, len(refs) - 30)})
    return rows


@tool
def get_net(name: str, project_path: str | None = None) -> dict:
    """Every pin on one net, with pin names and electrical types (useful for checking what drives a net)."""
    proj = _project(project_path)
    nets = proj.schematic.nets
    net = nets.get(name)
    if net is None:
        matches = [n for n in nets if n.lower() == name.lower()] or [n for n in nets if n.lower().endswith("/" + name.lower())]
        if len(matches) == 1:
            net = nets[matches[0]]
        else:
            hint = f"; did you mean one of {matches[:10]}?" if matches else ""
            raise ProjectError(f"no net named {name!r}{hint}")
    nodes = [_node(proj, r, n) for r, n in net.nodes]
    types = sorted({x["pin_type"] for x in nodes})
    return {"name": net.name, "pin_types": types, "nodes": nodes}


@tool
def check_connection(pin_a: str, pin_b: str, project_path: str | None = None) -> dict:
    """Check whether two pins are on the same net, e.g. pin_a="U1.VCC", pin_b="C3.1"."""
    proj = _project(project_path)
    ca, na = _pin(proj, pin_a)
    cb, nb = _pin(proj, pin_b)
    a, b = ca.pins[na], cb.pins[nb]
    return {
        "connected": a.net is not None and a.net == b.net,
        "pin_a": {**_node(proj, ca.ref, na), "net": a.net},
        "pin_b": {**_node(proj, cb.ref, nb), "net": b.net},
    }


@tool
def run_erc(project_path: str | None = None) -> dict:
    """Run KiCad's electrical rules check (needs kicad-cli) and return the violations."""
    proj = _project(project_path)
    if not proj.schematic_path:
        raise ProjectError("this project has no schematic")
    return kicad_cli.run_erc(proj.schematic_path)


@tool
def run_drc(project_path: str | None = None) -> dict:
    """Run KiCad's design rules check on the board (needs kicad-cli), including schematic/board parity."""
    proj = _project(project_path)
    if not proj.board_path:
        raise ProjectError("this project has no board (.kicad_pcb)")
    return kicad_cli.run_drc(proj.board_path)


@tool
def list_footprints(query: str | None = None, project_path: str | None = None) -> list[dict]:
    """List footprints on the board with position, side, rotation and pad nets.

    query: optional case-insensitive filter on reference, value or footprint name.
    """
    board = _project(project_path).board
    rows = []
    for key in sorted(board.footprints, key=_natural_key):
        fp = board.footprints[key]
        if query and query.lower() not in f"{fp.ref} {fp.value} {fp.footprint}".lower():
            continue
        rows.append(fp.to_dict())
    return rows


@tool
def kicad_status() -> dict:
    """Report whether kicad-cli was found and its version (ERC, DRC and exact netlists need it)."""
    exe = kicad_cli.find_kicad_cli()
    return {
        "kicad_cli": exe,
        "version": kicad_cli.version() if exe else None,
        "server_version": __version__,
        "hint": None if exe else "Install KiCad 10 or set KICAD_CLI to the full path of kicad-cli.",
    }


# --------------------------------------------------------------------------- step 2: parts, libraries, BOM

RELOAD_NOTE = (
    "If this schematic is open in KiCad, reload it (File > Revert) before making more edits there, "
    "otherwise saving in KiCad will overwrite these changes."
)


def _sheet_files(proj: Project) -> list[Path]:
    return [proj.folder / s["file"] for s in proj.schematic.sheets]


def _library(proj: Project) -> libraries.ProjectLibrary:
    return libraries.project_library(proj.folder, proj.name)


@tool
def distributor_status() -> dict:
    """Which distributor APIs (Mouser, DigiKey) are configured, and where KiCad's libraries are."""
    share = libraries.share_dir()
    return {
        **distributors.status(),
        "settings_file": str(config.config_path()),
        "kicad_libraries": str(share) if share else None,
    }


@tool
def search_parts(query: str, limit: int = 10, source: str | None = None) -> dict:
    """Search Mouser and/or DigiKey by keyword or part number, e.g. "LM358 SOIC", "10k 0603 1%", "USB-C receptacle".

    source: "mouser" or "digikey" to ask only one. Returns price breaks, stock, datasheet and product links.
    """
    return distributors.search(query, max(1, min(limit, 50)), source)


@tool
def get_part(part_number: str, source: str | None = None) -> list[dict]:
    """Full details for one manufacturer or distributor part number, including all parametric attributes."""
    parts = distributors.lookup(part_number, source)
    if not parts:
        raise ProjectError(f"no part {part_number!r} found")
    return [p.to_dict() for p in parts]


@tool
def download_datasheet(url: str, name: str, project_path: str | None = None) -> dict:
    """Save a datasheet PDF into the project's datasheets/ folder. name: file name without .pdf, e.g. the MPN."""
    proj = _project(project_path)
    path = datasheets.download(url, proj.folder / "datasheets", name)
    return {"file": str(path), "kicad_field": "${KIPRJMOD}/" + path.relative_to(proj.folder).as_posix()}


@tool
def assign_part(
    ref: str,
    part_number: str,
    source: str | None = None,
    save_datasheet: bool = True,
    project_path: str | None = None,
) -> dict:
    """Attach a real, orderable part to a schematic symbol.

    Looks the part up on the configured distributors, then writes MPN, Manufacturer,
    Mouser PN / Mouser URL, DigiKey PN / DigiKey URL and Datasheet fields into the
    symbol, and (save_datasheet) stores the PDF under datasheets/ as "Datasheet File".
    It does not change the symbol's value or footprint; check those match the part.
    """
    proj = _project(project_path)
    comp = _component(proj, ref)
    parts = distributors.lookup(part_number, source)
    if not parts:
        raise ProjectError(f"no part {part_number!r} found on {source or 'the configured distributors'}")
    first = parts[0]
    by_source = {p.source: p for p in reversed(parts)}
    # find the same MPN on the other distributor too
    for client in distributors.configured():
        if client.name not in by_source and first.mpn:
            try:
                match = [p for p in client.lookup(first.mpn) if p.mpn.upper() == first.mpn.upper()]
            except distributors.DistributorError:
                match = []
            if match:
                by_source[client.name] = match[0]

    fields = {bom.FIELD_NAMES["mpn"]: first.mpn, bom.FIELD_NAMES["manufacturer"]: first.manufacturer}
    for src, part in by_source.items():
        fields[bom.FIELD_NAMES[src]] = part.distributor_pn
        fields[bom.FIELD_NAMES[f"{src}_url"]] = part.product_url
    datasheet_url = next((p.datasheet_url for p in by_source.values() if p.datasheet_url), "")
    if datasheet_url:
        fields["Datasheet"] = datasheet_url
    result: dict = {"ref": comp.ref, "part": first.to_dict(attributes=False)}
    if save_datasheet and datasheet_url:
        try:
            path = datasheets.download(datasheet_url, proj.folder / "datasheets", first.mpn or part_number)
            fields["Datasheet File"] = "${KIPRJMOD}/" + path.relative_to(proj.folder).as_posix()
            result["datasheet_file"] = str(path)
        except datasheets.DatasheetError as e:
            result["datasheet_error"] = str(e)
    result["fields"] = fields
    result["edit"] = set_symbol_fields(_sheet_files(proj), comp.ref, {k: v for k, v in fields.items() if v})
    result["check"] = f"value is {comp.value!r}, footprint is {comp.footprint!r}; confirm they match {first.mpn}"
    result["note"] = RELOAD_NOTE
    return result


@tool
def set_fields(ref: str, fields: dict[str, str], project_path: str | None = None) -> dict:
    """Set fields on a schematic symbol, e.g. {"Value": "10k", "Footprint": "Resistor_SMD:R_0603_1608Metric", "MPN": "..."}.

    Existing fields are updated in place; new ones are added hidden. The reference cannot be changed here.
    """
    proj = _project(project_path)
    comp = _component(proj, ref)
    result = set_symbol_fields(_sheet_files(proj), comp.ref, fields)
    result["note"] = RELOAD_NOTE
    return result


@tool
def find_library_symbols(query: str, limit: int = 20) -> list[dict]:
    """Search KiCad's stock symbol libraries by name, description or keywords, e.g. "LM358", "LDO 3.3V", "ESP32"."""
    return libraries.find_symbols(query, max(1, min(limit, 100)))


@tool
def find_library_footprints(query: str, limit: int = 20) -> list[str]:
    """Search KiCad's stock footprints by name, e.g. "SOIC-8", "R_0603", "SOT-23-5", "USB_C Receptacle"."""
    return libraries.find_footprints(query, max(1, min(limit, 100)))


@tool
def add_library_symbol(
    symbol: str,
    new_name: str | None = None,
    fields: dict[str, str] | None = None,
    project_path: str | None = None,
) -> dict:
    """Copy a stock symbol (e.g. "Amplifier_Operational:LM358") into the project's own library.

    new_name renames it (e.g. a specific MPN); fields sets properties such as Footprint, Datasheet, MPN.
    The project library is created and registered in the project's library tables if needed.
    """
    proj = _project(project_path)
    lib = _library(proj)
    lib_id = libraries.copy_stock_symbol(lib, symbol, new_name, fields or {})
    return {"symbol": lib_id, "library_file": str(lib.symbol_file), "note": "Reopen the project in KiCad to see new library entries."}


@tool
def create_symbol(
    name: str,
    pins: list[dict],
    reference: str = "U",
    footprint: str = "",
    datasheet: str = "",
    description: str = "",
    fields: dict[str, str] | None = None,
    project_path: str | None = None,
) -> dict:
    """Create a rectangular symbol in the project library from a pin table, e.g. one read from a datasheet.

    pins: [{"number": "1", "name": "VIN", "type": "power_in", "side": "left"}, ...]. type is a KiCad
    pin type (input, output, bidirectional, tri_state, passive, power_in, power_out, open_collector,
    open_emitter, no_connect, unspecified, free); side (left/right/top/bottom) is optional.
    Double-check every pin number against the datasheet's pinout for the exact package.
    """
    proj = _project(project_path)
    lib = _library(proj)
    lib.ensure()
    block = libraries.generate_symbol(name, pins, reference, footprint, datasheet, description, fields)
    libraries.upsert_symbols(lib.symbol_file, [block])
    return {
        "symbol": f"{lib.nickname}:{name}",
        "pins": len(pins),
        "library_file": str(lib.symbol_file),
        "note": "Reopen the project in KiCad to see new library entries.",
    }


@tool
def import_part_zip(zip_path: str, project_path: str | None = None) -> dict:
    """Import symbols, footprints and 3D models from a vendor ZIP into the project library.

    Works with KiCad-format downloads from Mouser's "ECAD Model" (SamacSys / Component Search
    Engine), SnapEDA and Ultra Librarian. The user downloads the ZIP in their browser and gives its path.
    """
    proj = _project(project_path)
    result = libraries.import_ecad_zip(_library(proj), Path(zip_path).expanduser())
    result["note"] = "Reopen the project in KiCad to see new library entries."
    return result


@tool
def export_bom(
    boards: int = 1,
    spare_percent: float = 0,
    with_pricing: bool = False,
    file_name: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Write a purchase-ready BOM (CSV) to the project's bom/ folder and report anything missing.

    Groups identical parts, multiplies by boards (+ spare_percent extra), and with_pricing looks up
    current price and stock on Mouser/DigiKey using each part's distributor PN or MPN. The CSV
    can be uploaded to Mouser's or DigiKey's BOM tool.
    """
    proj = _project(project_path)
    name = datasheets.safe_name(file_name or f"{proj.name}_bom")
    return bom.export(
        proj.schematic,
        proj.folder / "bom" / f"{name}.csv",
        boards=max(1, boards),
        spare_percent=max(0.0, spare_percent),
        with_pricing=with_pricing,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
