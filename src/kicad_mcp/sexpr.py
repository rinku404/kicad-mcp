"""Minimal S-expression reader for KiCad files (.kicad_sch, .kicad_pcb, .net, ...).

Every node is a Python list whose first item is the node name, e.g.
``(at 1 2 90)`` -> ``["at", "1", "2", "90"]``. Quoted strings and bare atoms
are both returned as ``str``; callers convert numbers where they need them.
"""

from __future__ import annotations

from collections.abc import Iterator

Node = list  # a parsed list node: [name, *children]

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def parse(text: str) -> Node:
    """Parse a KiCad S-expression document and return its root node."""
    stack: list[list] = [[]]
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            node: list = []
            stack[-1].append(node)
            stack.append(node)
            i += 1
        elif c == ")":
            if len(stack) == 1:
                raise ValueError(f"unbalanced ')' at offset {i}")
            stack.pop()
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(_ESCAPES.get(text[i + 1], text[i + 1]))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            if i >= n:
                raise ValueError("unterminated string")
            stack[-1].append("".join(buf))
            i += 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            stack[-1].append(text[i:j])
            i = j
    if len(stack) != 1:
        raise ValueError("unbalanced '(': document ended early")
    roots = [x for x in stack[0] if isinstance(x, list)]
    if not roots:
        raise ValueError("no S-expression found")
    return roots[0]


def name(node: object) -> str | None:
    return node[0] if isinstance(node, list) and node and isinstance(node[0], str) else None


def children(node: Node, tag: str) -> Iterator[Node]:
    """Yield direct child nodes named ``tag``."""
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == tag:
            yield c


def child(node: Node, tag: str) -> Node | None:
    return next(children(node, tag), None)


def value(node: Node, tag: str, default: str | None = None) -> str | None:
    """Return the first atom of child ``tag``: ``(tag value)`` -> ``value``."""
    c = child(node, tag)
    if c is None or len(c) < 2 or isinstance(c[1], list):
        return default
    return c[1]


def flag(node: Node, tag: str) -> bool:
    """True for ``(tag)``, ``(tag yes)``, ``(tag <anything but no>)`` or a bare ``tag`` atom."""
    for c in node[1:]:
        if c == tag:
            return True
        if isinstance(c, list) and c and c[0] == tag:
            return len(c) == 1 or c[1] not in ("no", "false")
    return False


def at(node: Node) -> tuple[float, float, float]:
    """Return ``(x, y, angle)`` from the node's ``(at x y [angle])`` child."""
    c = child(node, "at")
    if c is None:
        return 0.0, 0.0, 0.0
    vals = [float(v) for v in c[1:4] if isinstance(v, str)]
    vals += [0.0] * (3 - len(vals))
    return vals[0], vals[1], vals[2]


def properties(node: Node) -> dict[str, str]:
    return {p[1]: p[2] for p in children(node, "property") if len(p) >= 3 and isinstance(p[2], str)}
