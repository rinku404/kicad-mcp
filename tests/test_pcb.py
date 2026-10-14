from conftest import DATA

from kicad_mcp.pcb import load_board


def test_board():
    b = load_board(DATA / "divider" / "divider.kicad_pcb")
    assert sorted(b.footprints) == ["R1", "R2", "R3"]
    assert b.footprints["R3"].to_dict()["side"] == "bottom"
    assert b.footprints["R2"].pads == {"1": "/MID", "2": "GND"}
    assert b.copper_layers == ["F.Cu", "B.Cu"]
    s = b.summary()
    assert s["board_size_mm"] == {"width": 20.0, "height": 10.0}
    assert (s["track_segments"], s["vias"]) == (1, 1)
