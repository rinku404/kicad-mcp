"""assign_part / set_fields / export_bom / datasheets against a copy of the fixture project."""

import csv
import http.server
import json
import shutil
import threading

import fakes
import pytest
from conftest import DATA

from kicad_mcp import kicad_cli, server
from kicad_mcp.schematic import load_schematic

PDF = b"%PDF-1.4\n% fake datasheet\n%%EOF\n"


@pytest.fixture
def project(tmp_path):
    dst = tmp_path / "divider"
    shutil.copytree(DATA / "divider", dst)
    return dst


@pytest.fixture
def pdf_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith(".pdf"):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.end_headers()
                self.wfile.write(PDF)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b'<html><a href="/real/datasheet.pdf">PDF</a></html>')

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_download_datasheet_follows_html_page(project, pdf_server):
    server.open_project(str(project / "divider.kicad_pro"))
    out = server.download_datasheet(f"{pdf_server}/product-page", "LM358DR")
    assert (project / "datasheets" / "LM358DR.pdf").read_bytes() == PDF
    assert out["kicad_field"] == "${KIPRJMOD}/datasheets/LM358DR.pdf"


def test_set_fields_keeps_file_loadable(project):
    server.open_project(str(project / "divider.kicad_pro"))
    out = server.set_fields("R1", {"Value": "12k", "MPN": "RC0603FR-0712KL", "Note": 'say "hi"'})
    assert out["changed_fields"] == ["MPN", "Note", "Value"]
    comp = load_schematic(project / "divider.kicad_sch").components["R1"]
    assert comp.value == "12k"
    assert comp.fields["MPN"] == "RC0603FR-0712KL"
    assert comp.fields["Note"] == 'say "hi"'
    # the project reloads after the edit, and nets are unchanged
    assert server.get_component("R1")["value"] == "12k"
    assert server.check_connection("R1.2", "R2.1")["connected"]
    with pytest.raises(ValueError):
        server.set_fields("R1", {"Reference": "R9"})


def test_assign_part_writes_both_distributors_and_datasheet(project, pdf_server, monkeypatch):
    fakes.install(monkeypatch, datasheet_url=f"{pdf_server}/lm358.pdf")
    server.open_project(str(project / "divider.kicad_pro"))
    out = server.assign_part("R3", "LM358DR")
    f = load_schematic(project / "divider.kicad_sch").components["R3"].fields
    assert f["MPN"] == "LM358DR"
    assert f["Manufacturer"] == "Texas Instruments"
    assert f["Mouser PN"] == "595-LM358DR"
    assert f["DigiKey PN"] == "296-1395-1-ND"
    assert f["Datasheet"] == f"{pdf_server}/lm358.pdf"
    assert f["Datasheet File"] == "${KIPRJMOD}/datasheets/LM358DR.pdf"
    assert out["datasheet_file"].endswith("LM358DR.pdf")


def test_export_bom_with_pricing(project, monkeypatch):
    fakes.install(monkeypatch)
    server.open_project(str(project / "divider.kicad_pro"))
    server.set_fields("R1", {"MPN": "LM358DR", "Mouser PN": "595-LM358DR"})
    out = server.export_bom(boards=10, with_pricing=True)
    assert out["lines"] == 3 and out["parts_per_board"] == 3
    rows = list(csv.DictReader((project / "bom" / "divider_bom.csv").open(encoding="utf-8-sig")))
    r1 = next(r for r in rows if r["References"] == "R1")
    assert (r1["Qty to order"], r1["Supplier"], r1["Unit price"]) == ("10", "mouser", "0.35")
    assert out["total"] == {"USD": 3.5}
    assert out["issues"]["missing_mpn"] == ["R2", "R3"]


@pytest.mark.skipif(kicad_cli.find_kicad_cli() is None, reason="kicad-cli not installed")
def test_edited_schematic_loads_in_kicad(project):
    server.open_project(str(project / "divider.kicad_pro"))
    server.set_fields("R2", {"MPN": "X", "Value": "4.7k"})
    nets = kicad_cli.export_netlist(project / "divider.kicad_sch")
    assert {(n["ref"], n["pin"]) for n in nets["/MID"]} == {("R1", "2"), ("R2", "1"), ("R3", "2")}


def test_download_all_datasheets(project, pdf_server, monkeypatch):
    fakes.install(monkeypatch, datasheet_url=f"{pdf_server}/lm358.pdf")
    server.open_project(str(project / "divider.kicad_pro"))
    server.set_fields("R1", {"Datasheet": f"{pdf_server}/docs/generic-resistor.pdf"})
    server.set_fields("R2", {"Datasheet": f"{pdf_server}/docs/generic-resistor.pdf"})
    server.set_fields("R3", {"Datasheet": "~", "MPN": "LM358DR"})  # no link: found through the MPN
    out = server.download_all_datasheets()
    assert out["downloaded"] == {
        "R1": "datasheets/generic-resistor.pdf",
        "R2": "datasheets/generic-resistor.pdf",
        "R3": "datasheets/LM358DR.pdf",
    }
    assert out["files"] == 2
    index = json.loads((project / "datasheets" / "index.json").read_text(encoding="utf-8"))
    assert index["R2"] == "datasheets/generic-resistor.pdf"
    # the datasheet tools use the index, and a second run downloads nothing
    assert server._datasheet_file(server._project(), "R2").name == "generic-resistor.pdf"
    again = server.download_all_datasheets()
    assert "downloaded" not in again and set(again["already_there"]) == {"R1", "R2", "R3"}
