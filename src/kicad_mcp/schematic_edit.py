"""Small, targeted edits to .kicad_sch files.

Edits change only the bytes they need to, so the file keeps KiCad's own
formatting and diffs stay readable. Every edited file is re-parsed before it
is written, and written atomically.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from . import sexpr

_PROP = re.compile(r'\(property\s+"((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"')
_PROTECTED = {"Reference"}


class EditError(ValueError):
    pass


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def _refs(node: list) -> set[str]:
    refs = {sexpr.properties(node).get("Reference", "")}
    inst = sexpr.child(node, "instances")
    if inst is not None:
        for proj in sexpr.children(inst, "project"):
            for p in sexpr.children(proj, "path"):
                r = sexpr.value(p, "reference")
                if r:
                    refs.add(r)
    refs.discard("")
    return refs


def _write_atomic(path: Path, text: str) -> None:
    sexpr.parse(text)  # never write a file KiCad could not read back
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _edit_symbol(block: str, fields: dict[str, str]) -> tuple[str, list[str]]:
    """Return the symbol block with ``fields`` set, plus the names that changed."""
    spans = sexpr.child_spans(block)
    props: dict[str, tuple[int, int]] = {}
    last_prop_end = None
    for s, e in spans:
        m = _PROP.match(block, s)
        if m:
            props[_unescape(m.group(1))] = (m.start(2), m.end(2))
            last_prop_end = e
    if last_prop_end is None:
        raise EditError("symbol has no properties to anchor new fields")

    x, y, _ = sexpr.at(sexpr.parse(block))
    line_start = block.rfind("\n", 0, spans[0][0]) + 1
    indent = block[line_start : spans[0][0]] or "\t\t"
    inner = indent + "\t"

    edits: list[tuple[int, int, str]] = []
    changed: list[str] = []
    insert = ""
    for name, value in fields.items():
        quoted = sexpr.quote(value)[1:-1]
        if name in props:
            s, e = props[name]
            if block[s:e] != quoted:
                edits.append((s, e, quoted))
                changed.append(name)
        else:
            insert += (
                f"\n{indent}(property {sexpr.quote(name)} {sexpr.quote(value)}"
                f"\n{inner}(at {x:g} {y:g} 0)"
                f"\n{inner}(hide yes)"
                f"\n{inner}(effects\n{inner}\t(font\n{inner}\t\t(size 1.27 1.27)\n{inner}\t)\n{inner})"
                f"\n{indent})"
            )
            changed.append(name)
    if insert:
        edits.append((last_prop_end, last_prop_end, insert))
    for s, e, new in sorted(edits, reverse=True):
        block = block[:s] + new + block[e:]
    return block, changed


def set_symbol_fields(sheet_files: list[Path], ref: str, fields: dict[str, str]) -> dict:
    """Set fields (properties) on every unit of symbol ``ref`` across the given sheet files.

    Existing fields keep their position and visibility; new ones are added hidden.
    """
    bad = _PROTECTED & set(fields)
    if bad:
        raise EditError(f"cannot change {', '.join(sorted(bad))} with this tool")
    if not fields:
        raise EditError("no fields given")

    touched: list[str] = []
    changed: set[str] = set()
    units = 0
    shared_sheet = False
    for path in dict.fromkeys(sheet_files):
        text = path.read_text(encoding="utf-8")
        new_text = text
        for s, e in reversed(sexpr.child_spans(text)):
            if not text.startswith("(symbol", s) or text[s + 7] not in " \t\r\n":
                continue
            node = sexpr.parse(text[s:e])
            refs = _refs(node)
            if ref not in refs:
                continue
            units += 1
            shared_sheet |= len(refs) > 1
            block, names = _edit_symbol(text[s:e], fields)
            changed.update(names)
            new_text = new_text[:s] + block + new_text[e:]
        if new_text != text:
            _write_atomic(path, new_text)
            touched.append(path.name)
    if units == 0:
        raise EditError(f"no symbol {ref!r} found in the schematic")
    result = {"ref": ref, "units_found": units, "changed_fields": sorted(changed), "files_written": touched}
    if shared_sheet:
        result["note"] = (
            "this symbol sits on a sheet used more than once, so the new fields apply to every copy of it"
        )
    return result
