"""PROJECT.md and the schematic title block."""

import os
import shutil

import pytest
from conftest import DATA

from kicad_mcp import kicad_cli, project_info, server, sexpr


@pytest.fixture
def project(tmp_path):
    dst = tmp_path / "divider"
    shutil.copytree(DATA / "divider", dst)
    server.open_project(str(dst / "divider.kicad_pro"))
    return dst


def title_block(project):
    doc = sexpr.parse((project / "divider.kicad_sch").read_text(encoding="utf-8"))
    tb = sexpr.child(doc, "title_block")
    return {k: sexpr.value(tb, k) for k in ("title", "date", "rev", "company")}, {
        int(c[1]): c[2] for c in sexpr.children(tb, "comment")
    }


def test_new_project_asks_the_questions(project):
    info = server.project_info()
    assert info["exists"] is False
    assert [a["field"] for a in info["ask_user"] if a["required"]] == ["Title", "Designer", "Purpose"]
    assert "ask_user" in server.open_project(str(project))


def test_save_writes_markdown_and_title_block(project):
    out = server.save_project_info(
        {"Title": "Sensor board", "Designer": "Paulo", "Purpose": "Reads a QRE1113 | line sensor", "Revision": "A",
         "Date": "2026-10-14", "Requirements": "5 V USB input\n30 x 20 mm"},
        notes="First spin.",
    )  # fmt: skip
    assert out["title_block_written"] == ["divider.kicad_sch"]
    assert "still_missing" not in out
    md = (project / "PROJECT.md").read_text(encoding="utf-8")
    assert md.startswith("# Sensor board")
    fields, notes = project_info.read(project)
    assert fields["Purpose"] == "Reads a QRE1113 | line sensor"
    assert fields["Requirements"] == "5 V USB input\n30 x 20 mm"
    assert notes == "First spin."
    entries, comments = title_block(project)
    assert entries == {"title": "Sensor board", "date": "2026-10-14", "rev": "A", "company": None}
    assert comments == {1: "Designer: Paulo", 2: "Purpose: Reads a QRE1113 | line sensor"}
    assert "ask_user" not in server.project_info()


def test_update_keeps_other_fields_and_comments(project):
    server.save_project_info({"Title": "Sensor board", "Designer": "Paulo", "Purpose": "Line sensor"})
    text = (project / "divider.kicad_sch").read_text(encoding="utf-8")
    (project / "divider.kicad_sch").write_text(
        text.replace('(comment 2 "Purpose: Line sensor")', '(comment 2 "Purpose: Line sensor")\n\t\t(comment 3 "Mine")'),
        encoding="utf-8",
    )
    server.save_project_info({"Designer": "Paulo Amaral", "Revision": "B"})
    entries, comments = title_block(project)
    assert entries["title"] == "Sensor board" and entries["rev"] == "B"
    assert comments == {1: "Designer: Paulo Amaral", 2: "Purpose: Line sensor", 3: "Mine"}
    assert project_info.read(project)[0]["Purpose"] == "Line sensor"
    with pytest.raises(ValueError, match="unknown field"):
        server.save_project_info({"Colour": "red"})


def test_project_is_found_from_the_working_folder(project, monkeypatch):
    monkeypatch.setattr(server, "_current", None)
    monkeypatch.chdir(project)
    assert server.project_info()["exists"] is False
    assert os.path.samefile(server._project().folder, project)


@pytest.mark.skipif(kicad_cli.find_kicad_cli() is None, reason="kicad-cli not installed")
def test_kicad_still_reads_the_schematic(project):
    server.save_project_info({"Title": "Sensor board", "Designer": "Paulo", "Purpose": 'Says "hi"'})
    assert kicad_cli.export_netlist(project / "divider.kicad_sch")
