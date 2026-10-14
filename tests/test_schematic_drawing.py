"""place_part / connect_pins / mark_no_connect on a copy of the fixture project."""

import shutil

import pytest
from conftest import DATA

from kicad_mcp import kicad_cli, schematic_place, server
from kicad_mcp.project import ProjectError
from kicad_mcp.schematic import load_schematic

SHARE = DATA / "kicad_share"  # a few of KiCad's stock symbols (Device, power, Amplifier_Operational)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_SHARE_DIR", str(SHARE))
    dst = tmp_path / "divider"
    shutil.copytree(DATA / "divider", dst)
    server.open_project(str(dst / "divider.kicad_pro"))
    return dst


def nets(project):
    return {n: sorted(f"{r}.{p}" for r, p in net.nodes) for n, net in load_schematic(project / "divider.kicad_sch").nets.items()}


def test_place_part_in_a_free_spot(project):
    r = server.place_part("Device:C", value="100n", footprint="Capacitor_SMD:C_0603_1608Metric", near="R3")
    assert r["ref"] == "C1" and r["pins"] == ["1", "2"]
    c = server.get_component("C1")
    assert (c["value"], c["footprint"]) == ("100n", "Capacitor_SMD:C_0603_1608Metric")
    assert all(p["net"].startswith("unconnected-") for p in c["pins"])
    # nothing that was there before changed
    n = nets(project)
    assert n["/MID"] == ["R1.2", "R2.1", "R3.2"]
    # next one gets the next reference and its own spot
    r2 = server.place_part("Device:C")
    assert r2["ref"] == "C2" and r2["at"] != r["at"]


def test_connect_with_label_power_symbol_and_inferred_net(project):
    server.place_part("Device:C", value="100n", near="R3")
    r = server.connect_pins(["C1.1", "R3.1"], "VSENSE")
    assert r["connected_with"] == "label"
    assert r["pins_on_net"] == ["C1.1", "R3.1"]
    r = server.connect_pins(["C1.2"], "GND")
    assert r["connected_with"] == "power symbol"
    n = nets(project)
    assert n["GND"] == ["C1.2", "R2.2"]
    assert n["/VSENSE"] == ["C1.1", "R3.1"]
    assert n["/MID"] == ["R1.2", "R2.1", "R3.2"]
    # net name taken from the pin that already has one; pins already there are skipped
    server.place_part("Device:R", value="1k")
    r = server.connect_pins(["R4.1", "R1.2"])
    assert r["net"] == "MID" and r["already_connected"] == ["R1.2"]
    assert nets(project)["/MID"] == ["R1.2", "R2.1", "R3.2", "R4.1"]


def test_connect_refuses_to_merge_named_nets(project):
    with pytest.raises(ProjectError, match="R1.1 is on VIN"):
        server.connect_pins(["R1.1"], "GND")
    with pytest.raises(ProjectError, match="give a net name"):
        server.place_part("Device:C")
        server.connect_pins(["C1.1", "C1.2"])


def test_multi_unit_derived_symbol(project):
    a = server.place_part("Amplifier_Operational:LM358", near="R3")
    b = server.place_part("Amplifier_Operational:LM358", ref=a["ref"], unit=2)
    assert a["ref"] == b["ref"] == "U1"
    with pytest.raises(ValueError, match="already used"):
        server.place_part("Amplifier_Operational:LM358", ref="U1", unit=2)
    server.connect_pins(["U1.1", "U1.2"], "FB")  # unit A: output to inverting input
    server.connect_pins(["U1.3"], "MID")
    server.mark_no_connect(["U1.5", "U1.6", "U1.7"])
    n = nets(project)
    assert n["/FB"] == ["U1.1", "U1.2"]
    assert "U1.3" in n["/MID"]
    sch = load_schematic(project / "divider.kicad_sch")
    assert {("U1", "5"), ("U1", "6"), ("U1", "7")} <= set(sch.no_connects)


def test_bad_edit_is_undone(project, monkeypatch):
    before = (project / "divider.kicad_sch").read_text(encoding="utf-8")
    monkeypatch.setattr(schematic_place, "check_unchanged", lambda *a: ["R1.1 (VIN) would also join R2.2"])
    with pytest.raises(ValueError, match="change undone"):
        server.place_part("Device:C")
    assert (project / "divider.kicad_sch").read_text(encoding="utf-8") == before


def test_pin_on_an_unplaced_unit(project):
    server.place_part("Amplifier_Operational:LM358")
    with pytest.raises(ProjectError, match="unit=N"):
        server.connect_pins(["U1.8"], "+5V")


@pytest.mark.skipif(kicad_cli.find_kicad_cli() is None, reason="kicad-cli not installed")
def test_kicad_reads_the_edits_the_same_way(project):
    server.place_part("Device:C", value="100n", near="R3")
    server.connect_pins(["C1.1", "R3.1"], "VSENSE")
    server.connect_pins(["C1.2"], "GND")
    server.place_part("Amplifier_Operational:LM358")
    server.place_part("Amplifier_Operational:LM358", ref="U1", unit=3)
    server.connect_pins(["U1.1", "U1.2"], "OUT")
    server.connect_pins(["U1.3", "R1.2"], "MID")
    server.connect_pins(["U1.8"], "+5V")
    server.connect_pins(["U1.4"], "GND")
    root = project / "divider.kicad_sch"
    theirs = {n: sorted((x["ref"], x["pin"]) for x in v) for n, v in kicad_cli.export_netlist(root).items()}
    ours = {n: sorted(net.nodes) for n, net in load_schematic(root).nets.items()}
    assert theirs == ours
    assert theirs["+5V"] == [("U1", "8")]
    assert theirs["GND"] == [("C1", "2"), ("R2", "2"), ("U1", "4")]
