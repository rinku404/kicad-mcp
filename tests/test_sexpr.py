import pytest

from kicad_mcp import sexpr


def test_parse_nested_and_strings():
    doc = sexpr.parse('(root (a 1 2) (s "hello \\"world\\"") (flag yes) bare)')
    assert sexpr.name(doc) == "root"
    assert sexpr.child(doc, "a") == ["a", "1", "2"]
    assert sexpr.value(doc, "s") == 'hello "world"'
    assert sexpr.flag(doc, "flag")
    assert sexpr.flag(doc, "bare")
    assert not sexpr.flag(doc, "missing")


def test_flag_no_and_power_global():
    doc = sexpr.parse("(x (hide no) (power global))")
    assert not sexpr.flag(doc, "hide")
    assert sexpr.flag(doc, "power")


def test_unbalanced():
    with pytest.raises(ValueError):
        sexpr.parse("(a (b)")
    with pytest.raises(ValueError):
        sexpr.parse("(a))")
