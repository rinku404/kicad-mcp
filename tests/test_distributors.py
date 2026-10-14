import fakes
import pytest

from kicad_mcp import distributors


def test_price_parsing():
    p = distributors._price
    assert p("$0.10") == 0.1
    assert p("R$ 1,23") == 1.23
    assert p("1.234,56 €") == 1234.56
    assert p("$1,234") == 1234
    assert p("R$ 0,012") == 0.012
    assert p("") is None


def test_search_both_distributors(monkeypatch):
    fakes.install(monkeypatch)
    out = distributors.search("LM358")
    assert out["errors"] == {}
    by = {r["source"]: r for r in out["results"]}
    assert by["mouser"]["distributor_pn"] == "595-LM358DR"
    assert by["mouser"]["price_breaks"][1] == {"quantity": 10, "unit_price": 0.35, "currency": "USD"}
    assert by["mouser"]["stock"] == 12345
    # DigiKey: cut tape is preferred over the full reel
    assert by["digikey"]["distributor_pn"] == "296-1395-1-ND"
    assert by["digikey"]["package"] == "8-SOIC"


def test_unit_price_breaks(monkeypatch):
    fakes.install(monkeypatch)
    part = distributors.lookup("LM358DR", "mouser")[0]
    assert part.unit_price(1).unit_price == 0.5
    assert part.unit_price(25).unit_price == 0.35
    assert part.unit_price(1000).unit_price == 0.2


def test_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_MCP_CONFIG", str(tmp_path / "none.json"))
    for k in ("MOUSER_API_KEY", "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(distributors.DistributorError, match="MOUSER_API_KEY"):
        distributors.search("x")
    assert distributors.status()["mouser"] == "set MOUSER_API_KEY"


def test_keys_from_settings_file(monkeypatch, tmp_path):
    for k in ("MOUSER_API_KEY", "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text('{"MOUSER_API_KEY": "from-file"}')
    monkeypatch.setenv("KICAD_MCP_CONFIG", str(cfg))
    assert distributors.Mouser.from_env().api_key == "from-file"
    monkeypatch.setenv("MOUSER_API_KEY", "from-env")
    assert distributors.Mouser.from_env().api_key == "from-env"
