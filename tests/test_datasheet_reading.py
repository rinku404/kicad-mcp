"""datasheet_contents / search_datasheet / read_datasheet on a generated PDF."""

import shutil

import pytest
from conftest import DATA
from pdfgen import make_pdf

from kicad_mcp import datasheets, server
from kicad_mcp.project import ProjectError

PAGES = [
    ["TPS000 3-A Buck Converter", "1 Features", "3 V to 17 V input range"],
    ["6 Pin Configuration and Functions", "Pin 1 VIN Supply input", "Pin 2 GND Ground", "Pin 3 EN Enable, active high"],
    ["7 Absolute Maximum Ratings", "VIN to GND -0.3 V to 20 V", "EN to GND -0.3 V to 7 V"],
    ["9 Application Information", "Place a 10 uF ceramic decoupling capacitor close to VIN."],
]
BOOKMARKS = [("Features", 1), ("Pin Configuration and Functions", 2), ("Absolute Maximum Ratings", 3),
             ("Application Information", 4)]  # fmt: skip


@pytest.fixture
def project(tmp_path):
    dst = tmp_path / "divider"
    shutil.copytree(DATA / "divider", dst)
    (dst / "datasheets").mkdir()
    (dst / "datasheets" / "TPS000.pdf").write_bytes(make_pdf(PAGES, BOOKMARKS, first_label=3))
    server.open_project(str(dst / "divider.kicad_pro"))
    server.set_fields("R1", {"Datasheet File": "${KIPRJMOD}/datasheets/TPS000.pdf"})
    return dst


def test_contents_lists_sections_with_pages(project):
    c = server.datasheet_contents("R1")
    assert c["file"] == "datasheets/TPS000.pdf"
    assert c["pages"] == 4
    assert {"title": "Absolute Maximum Ratings", "page": 3, "level": 0} in c["sections"]


def test_search_finds_the_page_and_the_lines(project):
    r = server.search_datasheet("R1", "absolute maximum EN")
    top = r["results"][0]
    assert top["page"] == 3
    assert top["printed_page"] == "5"
    assert any("EN to GND -0.3 V to 7 V" in s for s in top["snippets"])
    # word prefixes match, filler words are ignored
    r = server.search_datasheet("R1", "where is the decoupl capacitor")
    assert r["results"][0]["page"] == 4
    assert r["results"][0]["sections"] == ["Application Information"]


def test_search_with_no_match_says_so(project):
    r = server.search_datasheet("R1", "bluetooth")
    assert r["results"] == [] and "note" in r


def test_read_pages_plain_and_layout(project):
    r = server.read_datasheet("R1", "2-3")
    assert [p["page"] for p in r["pages"]] == [2, 3]
    assert "Pin 3 EN Enable, active high" in r["pages"][0]["text"]
    assert r["pages"][0]["section"] == "Pin Configuration and Functions"
    r = server.read_datasheet("datasheets/TPS000.pdf", "2", layout=True)
    assert "Pin 1 VIN Supply input" in r["pages"][0]["text"]
    with pytest.raises(datasheets.DatasheetError):
        server.read_datasheet("R1", "9")


def test_part_without_datasheet(project):
    server.set_fields("R3", {"Datasheet": "~"})
    with pytest.raises(ProjectError, match="no datasheet"):
        server.search_datasheet("R3", "anything")
