"""Thin wrapper around KiCad's ``kicad-cli`` command line tool."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from . import sexpr


class KiCadCliError(RuntimeError):
    pass


def _candidates() -> list[str]:
    system = platform.system()
    if system == "Windows":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")]
        return [
            str(Path(r) / "KiCad" / v / "bin" / "kicad-cli.exe")
            for r in roots
            if r
            for v in ("10.0", "9.0", "8.0")
        ]
    if system == "Darwin":
        return ["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"]
    return ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"]


def find_kicad_cli() -> str | None:
    """Locate kicad-cli: ``$KICAD_CLI``, then ``PATH``, then the default install folders."""
    env = os.environ.get("KICAD_CLI")
    if env and Path(env).exists():
        return env
    found = shutil.which("kicad-cli")
    if found:
        return found
    return next((c for c in _candidates() if Path(c).exists()), None)


def _run(args: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    exe = find_kicad_cli()
    if exe is None:
        raise KiCadCliError(
            "kicad-cli was not found. Install KiCad 9 or 10, or set the KICAD_CLI environment "
            "variable to the full path of kicad-cli."
        )
    proc = subprocess.run(
        [exe, *args], check=False, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    return proc


def version() -> str | None:
    try:
        proc = _run(["version"], timeout=30)
    except (KiCadCliError, OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None


def _json_report(args: list[str], src: Path) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        proc = _run([*args, "--format", "json", "--severity-all", "-o", str(out), str(src)], cwd=src.parent)
        if not out.exists():
            raise KiCadCliError(f"kicad-cli failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return json.loads(out.read_text(encoding="utf-8"))


def _violation(v: dict, with_pos: bool) -> dict:
    items = []
    for it in v.get("items", []):
        d = {"description": it.get("description", "")}
        if with_pos and "pos" in it:
            d["x_mm"], d["y_mm"] = it["pos"].get("x"), it["pos"].get("y")
        items.append(d)
    return {
        "severity": v.get("severity"),
        "type": v.get("type"),
        "description": v.get("description"),
        "items": items,
    }


def _summary(violations: list[dict]) -> dict:
    return {
        "errors": sum(1 for v in violations if v["severity"] == "error"),
        "warnings": sum(1 for v in violations if v["severity"] == "warning"),
        "by_type": dict(Counter(v["type"] for v in violations).most_common()),
    }


def run_erc(schematic: Path) -> dict:
    """Run the electrical rules check and return the violations, grouped per sheet."""
    data = _json_report(["sch", "erc"], schematic)
    violations = []
    for sheet in data.get("sheets", []):
        for v in sheet.get("violations", []):
            # ERC item positions in the JSON report are not in mm, so they are dropped
            d = _violation(v, with_pos=False)
            d["sheet"] = sheet.get("path", "/")
            violations.append(d)
    return {"kicad_version": data.get("kicad_version"), "summary": _summary(violations), "violations": violations}


def run_drc(board: Path, schematic_parity: bool = True) -> dict:
    """Run the design rules check on a board."""
    args = ["pcb", "drc"]
    if schematic_parity:
        args.append("--schematic-parity")
    data = _json_report(args, board)
    violations = [_violation(v, True) for v in data.get("violations", [])]
    unconnected = [_violation(v, True) for v in data.get("unconnected_items", [])]
    parity = [_violation(v, False) for v in data.get("schematic_parity", [])]
    return {
        "kicad_version": data.get("kicad_version"),
        "summary": {
            **_summary(violations),
            "unconnected_items": len(unconnected),
            "schematic_parity_issues": len(parity),
        },
        "violations": violations,
        "unconnected_items": unconnected,
        "schematic_parity": parity,
    }


def export_netlist(schematic: Path) -> dict[str, list[dict]]:
    """Export the netlist with KiCad itself. Returns ``{net name: [node, ...]}``."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "netlist.net"
        proc = _run(["sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out), str(schematic)], cwd=schematic.parent)
        if not out.exists():
            raise KiCadCliError(f"netlist export failed: {proc.stderr.strip() or proc.stdout.strip()}")
        doc = sexpr.parse(out.read_text(encoding="utf-8"))
    nets: dict[str, list[dict]] = {}
    for n in sexpr.children(sexpr.child(doc, "nets") or ["nets"], "net"):
        nets[sexpr.value(n, "name", "") or ""] = [
            {
                "ref": sexpr.value(node, "ref", ""),
                "pin": sexpr.value(node, "pin", ""),
                "pin_name": sexpr.value(node, "pinfunction", "") or "",
                "pin_type": sexpr.value(node, "pintype", "") or "",
            }
            for node in sexpr.children(n, "node")
        ]
    return nets
