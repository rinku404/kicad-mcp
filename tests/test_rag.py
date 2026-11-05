"""ask_datasheets: cited passages across every datasheet in the project."""

import shutil

import pytest
from conftest import DATA
from pdfgen import make_pdf

from kicad_mcp import rag, server

LDO = [
    ["LDO1117 800-mA Low-Dropout Regulator", "1 Features", "Input voltage up to 15 V", "Output 3.3 V fixed"],
    ["7 Absolute Maximum Ratings", "VIN input voltage -0.3 V to 20 V", "Operating junction temperature TJ -40 to 125 C"],
    ["9 Application Information", "Use a 10 uF tantalum capacitor on the output for stability."],
]
OPAMP = [
    ["OPA000 Dual Operational Amplifier", "Supply voltage 3 V to 32 V"],
    ["6 Absolute Maximum Ratings", "Supply voltage VCC 36 V", "Differential input voltage 32 V"],
]


class FakeEmbedder:
    """Meaning-ish vectors: words that mean the same thing share a dimension."""

    name = "fake"
    GROUPS = ({"hot", "temperature", "thermal", "heat"}, {"voltage", "volts", "supply"}, {"capacitor", "decoupling"})

    def _vec(self, text):
        words = set(rag.tokens(text))
        return [float(len(words & g) > 0) for g in self.GROUPS] + [0.01]

    def passages(self, texts):
        return [self._vec(t) for t in texts]

    def query(self, text):
        return self._vec(text)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, "_embedder", FakeEmbedder())
    monkeypatch.setattr(rag, "_embedder_error", None)
    dst = tmp_path / "divider"
    shutil.copytree(DATA / "divider", dst)
    ds = dst / "datasheets"
    ds.mkdir()
    (ds / "LDO1117.pdf").write_bytes(make_pdf(LDO, [("Features", 1), ("Absolute Maximum Ratings", 2),
                                                    ("Application Information", 3)], first_label=1))  # fmt: skip
    (ds / "OPA000.pdf").write_bytes(make_pdf(OPAMP))
    (ds / "index.json").write_text('{"R1": "datasheets/LDO1117.pdf", "R2": "datasheets/OPA000.pdf"}')
    server.open_project(str(dst / "divider.kicad_pro"))
    return dst


def test_answers_with_citations_across_all_datasheets(project):
    r = server.ask_datasheets("absolute maximum input voltage")
    assert r["search"] == "keywords + meaning"
    assert sorted(r["newly_indexed"]) == ["datasheets/LDO1117.pdf", "datasheets/OPA000.pdf"]
    top = next(x for x in r["results"] if x["page"] == 2)
    assert top["cite"] == "LDO1117.pdf p.2, Absolute Maximum Ratings"
    assert "VIN input voltage -0.3 V to 20 V" in top["text"]
    assert top["parts"] == ["R1"]
    assert {x["file"] for x in r["results"]} == {"datasheets/LDO1117.pdf", "datasheets/OPA000.pdf"}


def test_meaning_finds_other_words(project):
    r = server.ask_datasheets("how hot can it get")
    assert r["results"][0]["page"] == 2
    assert "junction temperature" in r["results"][0]["text"]


def test_limit_to_parts_and_reuse_index(project):
    server.ask_datasheets("voltage")
    r = server.ask_datasheets("voltage", parts=["R2"])
    assert "newly_indexed" not in r
    assert {x["file"] for x in r["results"]} == {"datasheets/OPA000.pdf"}


def test_keyword_only_without_embeddings(project, monkeypatch):
    monkeypatch.setattr(rag, "_embedder", None)
    r = server.ask_datasheets("tantalum capacitor output")
    assert r["search"] == "keywords" and "KICAD_MCP_EMBEDDINGS" in r["note"]
    assert r["results"][0]["cite"] == "LDO1117.pdf p.3, Application Information"


def test_index_follows_added_and_removed_files(project):
    server.ask_datasheets("voltage")
    (project / "datasheets" / "OPA000.pdf").unlink()
    (project / "datasheets" / "new.pdf").write_bytes(make_pdf([["Bluetooth radio", "2.4 GHz"]]))
    r = server.ask_datasheets("bluetooth")
    assert r["newly_indexed"] == ["datasheets/new.pdf"]
    assert r["results"][0]["file"] == "datasheets/new.pdf"
    assert all(x["file"] != "datasheets/OPA000.pdf" for x in server.ask_datasheets("voltage")["results"])
