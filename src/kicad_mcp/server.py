"""MCP server exposing a KiCad project to LLM clients."""

from __future__ import annotations

import functools
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, kicad_cli
from .project import Project, ProjectError
from .schematic import Component, _natural_key

INSTRUCTIONS = """\
Tools for reading and checking a KiCad 9/10 design.

Start with open_project (a .kicad_pro, .kicad_sch, .kicad_pcb or project folder);
every other tool then works on that project unless you pass project_path.
Files are re-read automatically when they change on disk, so save in KiCad
before asking about recent edits. Pins are written REF.PIN, for example U1.3
or U1.VCC (pin number or pin name).
"""

mcp = MCPServer("kicad-mcp", instructions=INSTRUCTIONS, version=__version__)



def tool(fn):
    """Register an MCP tool whose expected failures reach the model as readable errors."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ProjectError, kicad_cli.KiCadCliError, OSError, ValueError) as e:
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
