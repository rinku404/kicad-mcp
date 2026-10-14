"""PROJECT.md: what a design is, who made it and why, kept next to the KiCad files.

The file is plain Markdown the user can edit by hand. Its fields table is the
source of truth; title, revision, date, company, designer and purpose are also
written into the title block of every schematic sheet so they appear on the
printed pages.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from . import sexpr
from .schematic_edit import _write_atomic

FILE = "PROJECT.md"

# (field, question to ask, required)
FIELDS = [
    ("Title", "What is the board called?", True),
    ("Designer", "Who is designing it?", True),
    ("Purpose", "What is it for, in a sentence or two?", True),
    ("Company", "Company or organisation (or leave empty)?", False),
    ("Revision", "Which revision is this (for example A or 1.0)?", False),
    ("Date", "Date for the title block (empty means today)?", False),
    ("Requirements", "Key requirements: input power, size, environment, interfaces?", False),
    ("Status", "Status: concept, prototype or production?", False),
]
NAMES = [f[0] for f in FIELDS]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>").strip()


def _uncell(text: str) -> str:
    return text.replace("<br>", "\n").replace("\\|", "|").strip()


def read(folder: Path) -> tuple[dict[str, str], str]:
    """Fields and the free-form notes of PROJECT.md (empty when it does not exist)."""
    path = folder / FILE
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    fields: dict[str, str] = {}
    for m in re.finditer(r"^\|\s*([^|]+?)\s*\|\s*((?:[^|\\]|\\.)*?)\s*\|\s*$", text, re.MULTILINE):
        key, value = m.group(1).strip(), _uncell(m.group(2))
        if key in NAMES:
            fields[key] = value
    notes = ""
    m = re.search(r"^## Notes\s*\n(.*)", text, re.MULTILINE | re.DOTALL)
    if m:
        notes = m.group(1).strip()
    return fields, notes


def write(folder: Path, fields: dict[str, str], notes: str) -> Path:
    title = fields.get("Title") or folder.name
    rows = "\n".join(f"| {k} | {_cell(fields.get(k, ''))} |" for k in NAMES)
    text = (
        f"# {title}\n\n"
        "<!-- Project information for kicad-mcp. Edit freely: Title, Designer, Purpose, Company,\n"
        "     Revision and Date are copied into the schematic title block by save_project_info. -->\n\n"
        f"| Field | Value |\n| --- | --- |\n{rows}\n\n## Notes\n\n{notes.strip()}\n"
    )
    path = folder / FILE
    path.write_text(text, encoding="utf-8")
    return path


def missing(fields: dict[str, str]) -> list[dict]:
    return [
        {"field": name, "question": q, "required": req}
        for name, q, req in FIELDS
        if not fields.get(name, "").strip()
    ]


# --------------------------------------------------------------------------- title block


def _short(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def title_block_values(fields: dict[str, str]) -> dict:
    out: dict = {}
    if fields.get("Title"):
        out["title"] = fields["Title"]
    if fields.get("Revision"):
        out["rev"] = fields["Revision"]
    out["date"] = fields.get("Date") or datetime.date.today().isoformat()
    if fields.get("Company"):
        out["company"] = fields["Company"]
    comments = {}
    if fields.get("Designer"):
        comments["Designer"] = _short(fields["Designer"])
    if fields.get("Purpose"):
        comments["Purpose"] = _short(fields["Purpose"])
    out["comments"] = comments
    return out


def _render(entries: dict, comments: dict[int, str], indent: str) -> str:
    inner = indent + "\t"
    lines = [f"{indent}(title_block"]
    for key in ("title", "date", "rev", "company"):
        if entries.get(key):
            lines.append(f"{inner}({key} {sexpr.quote(entries[key])})")
    for n in sorted(comments):
        lines.append(f"{inner}(comment {n} {sexpr.quote(comments[n])})")
    lines.append(f"{indent})")
    return "\n".join(lines).lstrip("\t")


def set_title_block(path: Path, values: dict, set_title: bool) -> bool:
    """Write ``values`` into one sheet's title block, keeping entries it does not manage."""
    text = path.read_text(encoding="utf-8")
    spans = sexpr.child_spans(text)
    tb_span = next(((s, e) for s, e in spans if text.startswith("(title_block", s)), None)
    entries: dict = {}
    comments: dict[int, str] = {}
    if tb_span:
        node = sexpr.parse(text[tb_span[0] : tb_span[1]])
        for key in ("title", "date", "rev", "company"):
            v = sexpr.value(node, key)
            if v:
                entries[key] = v
        for c in sexpr.children(node, "comment"):
            if len(c) >= 3 and str(c[1]).isdigit():
                comments[int(c[1])] = c[2]
    for key in ("date", "rev", "company"):
        if values.get(key):
            entries[key] = values[key]
    if values.get("title") and (set_title or not entries.get("title")):
        entries["title"] = values["title"]
    for label, value in values.get("comments", {}).items():
        slot = next((n for n, c in comments.items() if c.startswith(f"{label}:")), None)
        if slot is None:
            slot = next((n for n in range(1, 10) if not comments.get(n)), None)
        if slot is not None:
            comments[slot] = f"{label}: {value}"
    if tb_span:
        line_start = text.rfind("\n", 0, tb_span[0]) + 1
        indent = text[line_start : tb_span[0]]
        new = text[: tb_span[0]] + _render(entries, comments, indent) + text[tb_span[1] :]
    else:
        after = next((e for s, e in spans if text.startswith("(paper", s)), None)
        if after is None:
            after = max((e for s, e in spans if text.startswith(("(version", "(generator", "(uuid"), s)), default=None)
        if after is None:
            raise ValueError(f"{path.name} does not look like a KiCad schematic")
        new = text[:after] + "\n\t" + _render(entries, comments, "\t") + text[after:]
    if new == text:
        return False
    _write_atomic(path, new)
    return True
