"""Compare the built-in connectivity solver with KiCad's own netlist export.

Runs on the bundled fixtures, plus every root schematic under the folder named by
the KICAD_MCP_PARITY_DIR environment variable (for example a checkout of KiCad's
``demos`` folder). Skipped when kicad-cli is not installed.
"""

import os
import re
from pathlib import Path

import pytest
from conftest import DATA

from kicad_mcp import kicad_cli
from kicad_mcp.schematic import load_schematic


def _roots(folder: Path) -> list[Path]:
    roots = []
    for d in sorted({p.parent for p in folder.rglob("*.kicad_sch")}):
        files = list(d.glob("*.kicad_sch"))
        used = set()
        for f in files:
            used |= set(re.findall(r'\(property "Sheet ?[Ff]ile" "([^"]+)"', f.read_text(encoding="utf-8")))
        roots += [f for f in files if f.name not in used]
    return roots


def _schematics() -> list[Path]:
    found = _roots(DATA)
    extra = os.environ.get("KICAD_MCP_PARITY_DIR")
    if extra:
        found += _roots(Path(extra))
    return found


@pytest.mark.skipif(kicad_cli.find_kicad_cli() is None, reason="kicad-cli not installed")
@pytest.mark.parametrize("path", _schematics(), ids=lambda p: p.name)
def test_netlist_matches_kicad(path: Path):
    try:
        exported = kicad_cli.export_netlist(path)
    except kicad_cli.KiCadCliError:
        pytest.skip("KiCad itself cannot load this schematic")
    ours = {frozenset(n.nodes): n.name for n in load_schematic(path).nets.values()}
    theirs = {frozenset((x["ref"], x["pin"]) for x in nodes): name for name, nodes in exported.items()}
    if any("{" in name or "[" in name or "." in name for name in theirs.values()) and ours.keys() != theirs.keys():
        pytest.xfail("buses are not supported by the built-in solver yet")
    assert ours.keys() == theirs.keys()
    renamed = {theirs[k]: ours[k] for k in theirs if theirs[k] != ours[k]}
    assert len(renamed) <= max(2, len(theirs) // 50), renamed
