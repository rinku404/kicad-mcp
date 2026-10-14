import pytest
from conftest import DATA

from kicad_mcp import kicad_cli, server
from kicad_mcp.project import ProjectError

PRO = str(DATA / "divider" / "divider.kicad_pro")


def test_open_project_summary():
    s = server.open_project(PRO)
    assert s["components"] == 3
    assert s["board"] == "divider.kicad_pcb"
    assert s["parts_missing_footprint"] == []


def test_components_and_query():
    server.open_project(PRO)
    assert [c["ref"] for c in server.list_components()] == ["R1", "R2", "R3"]
    assert [c["ref"] for c in server.list_components("4k7")] == ["R2"]


def test_get_component_has_connections_and_pcb():
    server.open_project(PRO)
    c = server.get_component("r1")
    pin2 = next(p for p in c["pins"] if p["number"] == "2")
    assert pin2["net"] == "/MID"
    assert sorted(pin2["connected_to"]) == ["R2.1", "R3.2"]
    assert c["pcb"]["pads"] == {"1": "VIN", "2": "/MID"}


def test_nets_and_connection_check():
    server.open_project(PRO)
    assert server.get_net("MID")["name"] == "/MID"
    assert server.check_connection("R1.2", "R3:2")["connected"] is True
    assert server.check_connection("R1.1", "R2.2")["connected"] is False
    with pytest.raises(ProjectError):
        server.check_connection("R9.1", "R1.1")


@pytest.mark.skipif(kicad_cli.find_kicad_cli() is None, reason="kicad-cli not installed")
def test_drc_finds_the_short():
    # the fixture board has a deliberate track short between /MID and GND
    server.open_project(PRO)
    r = server.run_drc()
    assert "shorting_items" in r["summary"]["by_type"]


@pytest.mark.skipif(kicad_cli.find_kicad_cli() is None, reason="kicad-cli not installed")
def test_erc_runs():
    server.open_project(PRO)
    r = server.run_erc()
    assert r["summary"]["errors"] + r["summary"]["warnings"] == len(r["violations"])
