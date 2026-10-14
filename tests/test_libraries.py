import io
import shutil
import zipfile

import pytest
from conftest import DATA

from kicad_mcp import kicad_cli, libraries, sexpr

TPS_PINS = [
    {"number": "1", "name": "SW", "type": "output"},
    {"number": "2", "name": "PG", "type": "open_collector"},
    {"number": "3", "name": "FB", "type": "input"},
    {"number": "4", "name": "AGND", "type": "power_in"},
    {"number": "8", "name": "AVIN", "type": "power_in"},
    {"number": "13", "name": "EP", "type": "passive", "side": "bottom"},
]


@pytest.fixture
def lib(tmp_path):
    shutil.copytree(DATA / "divider", tmp_path / "divider")
    return libraries.project_library(tmp_path / "divider", "divider")


def _symbol(lib, name):
    doc = sexpr.parse(lib.symbol_file.read_text(encoding="utf-8"))
    return next(s for s in sexpr.children(doc, "symbol") if s[1] == name)


def test_generated_symbol_pins_on_grid(lib):
    lib.ensure()
    block = libraries.generate_symbol("TPS62160", TPS_PINS, footprint="Package_SON:X", fields={"MPN": "TPS62160DSGR"})
    libraries.upsert_symbols(lib.symbol_file, [block])
    sym = _symbol(lib, "TPS62160")
    assert sexpr.properties(sym)["MPN"] == "TPS62160DSGR"
    pins = [p for u in sexpr.children(sym, "symbol") for p in sexpr.children(u, "pin")]
    assert sorted(sexpr.value(p, "number") for p in pins) == sorted(p["number"] for p in TPS_PINS)
    for p in pins:
        x, y, _ = sexpr.at(p)
        assert round(x / 1.27, 6).is_integer() and round(y / 1.27, 6).is_integer()
    sides = {sexpr.value(p, "number"): sexpr.at(p) for p in pins}
    assert sides["4"][1] < 0 and sides["8"][1] > 0  # ground below, supply above
    assert sides["1"][0] > 0 and sides["3"][0] < 0  # output right, input left


def test_upsert_replaces_and_registers_tables(lib):
    lib.ensure()
    lib.ensure()  # idempotent
    table = (lib.folder / "sym-lib-table").read_text()
    assert table.count('(name "divider")') == 1
    libraries.upsert_symbols(lib.symbol_file, [libraries.generate_symbol("A", TPS_PINS[:2])])
    libraries.upsert_symbols(lib.symbol_file, [libraries.generate_symbol("A", TPS_PINS[:3])])
    text = lib.symbol_file.read_text()
    assert text.count('(symbol "A"') == 1
    assert len([p for u in sexpr.children(_symbol(lib, "A"), "symbol") for p in sexpr.children(u, "pin")]) == 3


def test_bad_pins_rejected():
    with pytest.raises(ValueError):
        libraries.generate_symbol("X", [{"number": "1", "type": "wiggly"}])
    with pytest.raises(ValueError):
        libraries.generate_symbol("X", [{"number": "1"}, {"number": "1"}])


def test_import_vendor_zip(lib, tmp_path):
    sym = libraries.generate_symbol("LM358DR", TPS_PINS[:3], footprint="SamacSys:SOIC127P600X175-8N")
    fp = '(footprint "SOIC127P600X175-8N"\n\t(layer "F.Cu")\n\t(model "C:/Users/x/LM358DR.stp"\n\t\t(offset (xyz 0 0 0))\n\t)\n)\n'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("LM358DR/KiCad/LM358DR.kicad_sym", f"(kicad_symbol_lib\n\t(version 20211014)\n\t{sym}\n)\n")
        z.writestr("LM358DR/KiCad/SOIC127P600X175-8N.kicad_mod", fp)
        z.writestr("LM358DR/3D/LM358DR.stp", "ISO-10303-21;")
    zp = tmp_path / "LIB_LM358DR.zip"
    zp.write_bytes(buf.getvalue())
    out = libraries.import_ecad_zip(lib, zp)
    assert out["symbols"] == ["divider:LM358DR"]
    assert out["footprints"] == ["divider:SOIC127P600X175-8N"]
    assert out["models"] == ["LM358DR.stp"]
    assert sexpr.properties(_symbol(lib, "LM358DR"))["Footprint"] == "divider:SOIC127P600X175-8N"
    fp_text = (lib.footprint_dir / "SOIC127P600X175-8N.kicad_mod").read_text()
    assert '(model "${KIPRJMOD}/libs/3dmodels/LM358DR.stp"' in fp_text


def test_zip_slip_rejected(lib, tmp_path):
    zp = tmp_path / "evil.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("../../evil.kicad_mod", "(footprint \"x\")")
    with pytest.raises(ValueError, match="unsafe"):
        libraries.import_ecad_zip(lib, zp)


needs_kicad_libs = pytest.mark.skipif(libraries.share_dir() is None, reason="KiCad libraries not installed")


@needs_kicad_libs
def test_find_and_copy_stock_symbol(lib):
    assert libraries.find_symbols("LM358")[0]["id"] == "Amplifier_Operational:LM358"
    assert "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm" in libraries.find_footprints("SOIC-8")
    lib_id = libraries.copy_stock_symbol(lib, "Amplifier_Operational:LM358", "LM358DR", {"MPN": "LM358DR"})
    assert lib_id == "divider:LM358DR"
    names = [s[1] for s in sexpr.children(sexpr.parse(lib.symbol_file.read_text()), "symbol")]
    assert names == ["LM2904", "LM358DR"]  # the parent it extends comes along


@pytest.mark.skipif(kicad_cli.find_kicad_cli() is None, reason="kicad-cli not installed")
def test_kicad_loads_generated_library(lib, tmp_path):
    lib.ensure()
    libraries.upsert_symbols(lib.symbol_file, [libraries.generate_symbol("TPS62160", TPS_PINS)])
    proc = kicad_cli._run(["sym", "export", "svg", "-o", str(tmp_path / "svg"), str(lib.symbol_file)])
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "svg" / "TPS62160_unit1.svg").exists()
