from conftest import DATA

from kicad_mcp.schematic import load_schematic

# Expected nets come from `kicad-cli sch export netlist` (KiCad 10.0.6) on the same file.
EXPECTED = {
    "/MID": [("R1", "2"), ("R2", "1"), ("R3", "2")],
    "GND": [("R2", "2")],
    "VIN": [("R1", "1")],
    "unconnected-(R3-Pad1)": [("R3", "1")],
}


def test_divider_matches_kicad_netlist():
    sch = load_schematic(DATA / "divider" / "divider.kicad_sch")
    assert {name: net.nodes for name, net in sch.nets.items()} == EXPECTED
    assert sch.warnings == []


def test_components_and_fields():
    sch = load_schematic(DATA / "divider" / "divider.kicad_sch")
    assert sorted(sch.components) == ["R1", "R2", "R3"]  # power symbols are not components
    r1 = sch.components["R1"]
    assert r1.value == "10k"
    assert r1.footprint == "Resistor_SMD:R_0603_1608Metric"
    assert r1.datasheet == "https://example.com/r.pdf"
    assert r1.pins["2"].net == "/MID"
    assert sch.title == "Divider test"
