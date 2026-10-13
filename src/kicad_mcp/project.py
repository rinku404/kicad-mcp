"""A KiCad project on disk: its schematic, board and the connectivity between them."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import kicad_cli
from .pcb import Board, load_board
from .schematic import Net, Schematic, load_schematic


class ProjectError(ValueError):
    pass


def resolve_paths(path: str | Path) -> tuple[Path, Path | None, Path | None]:
    """Return ``(project_dir, root_schematic, board)`` for a .kicad_pro, .kicad_sch, .kicad_pcb or folder."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ProjectError(f"path does not exist: {p}")
    if p.is_dir():
        pros = sorted(p.glob("*.kicad_pro"))
        if len(pros) > 1:
            names = ", ".join(x.name for x in pros)
            raise ProjectError(f"{p} holds several projects ({names}); pass the .kicad_pro file instead")
        if pros:
            p = pros[0]
        else:
            schs = sorted(p.glob("*.kicad_sch"))
            if not schs:
                raise ProjectError(f"no KiCad project or schematic found in {p}")
            p = schs[0]
    stem, folder = p.stem, p.parent
    if p.suffix not in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
        raise ProjectError(f"not a KiCad file: {p.name}")
    sch = folder / f"{stem}.kicad_sch"
    pcb = folder / f"{stem}.kicad_pcb"
    if p.suffix == ".kicad_sch":
        sch = p
    return folder, (sch if sch.exists() else None), (pcb if pcb.exists() else None)


@dataclass
class _Stamp:
    files: dict[Path, float]

    @classmethod
    def of(cls, folder: Path) -> _Stamp:
        files = {}
        for pattern in ("*.kicad_sch", "*.kicad_pcb", "**/*.kicad_sch"):
            for f in folder.glob(pattern):
                files[f] = f.stat().st_mtime
        return cls(files)


class Project:
    """Loads lazily and reloads whenever a file on disk changes."""

    def __init__(self, path: str | Path) -> None:
        self.folder, self.schematic_path, self.board_path = resolve_paths(path)
        self.name = (self.schematic_path or self.board_path or self.folder).stem
        self._stamp: _Stamp | None = None
        self._sch: Schematic | None = None
        self._board: Board | None = None
        self.netlist_source = "none"

    def _refresh(self) -> None:
        stamp = _Stamp.of(self.folder)
        if self._stamp is not None and stamp.files == self._stamp.files:
            return
        self._stamp = stamp
        self._sch = load_schematic(self.schematic_path) if self.schematic_path else None
        self._board = load_board(self.board_path) if self.board_path else None
        if self._sch is not None:
            self.netlist_source = "built-in parser"
            if kicad_cli.find_kicad_cli():
                try:
                    self._apply_kicad_netlist(kicad_cli.export_netlist(self.schematic_path))
                    self.netlist_source = "kicad-cli"
                except (kicad_cli.KiCadCliError, OSError) as e:
                    self._sch.warnings.append(f"kicad-cli netlist export failed, using built-in parser: {e}")

    def _apply_kicad_netlist(self, nets: dict[str, list[dict]]) -> None:
        sch = self._sch
        assert sch is not None
        for comp in sch.components.values():
            for pin in comp.pins.values():
                pin.net = None
        sch.nets = {}
        for name, nodes in nets.items():
            sch.nets[name] = Net(name=name, nodes=[(n["ref"], n["pin"]) for n in nodes])
            for n in nodes:
                comp = sch.components.get(n["ref"])
                if comp and n["pin"] in comp.pins:
                    comp.pins[n["pin"]].net = name

    @property
    def schematic(self) -> Schematic:
        self._refresh()
        if self._sch is None:
            raise ProjectError("this project has no schematic")
        return self._sch

    @property
    def board(self) -> Board:
        self._refresh()
        if self._board is None:
            raise ProjectError("this project has no board (.kicad_pcb)")
        return self._board

    def has_board(self) -> bool:
        return self.board_path is not None

    def summary(self) -> dict:
        self._refresh()
        d: dict = {
            "name": self.name,
            "folder": str(self.folder),
            "schematic": self.schematic_path.name if self.schematic_path else None,
            "board": self.board_path.name if self.board_path else None,
            "netlist_source": self.netlist_source,
        }
        if self._sch:
            parts = [c for c in self._sch.components.values()]
            d.update(
                title=self._sch.title,
                sheets=[s["path"] for s in self._sch.sheets],
                components=len(parts),
                nets=len(self._sch.nets),
                parts_missing_footprint=sorted(c.ref for c in parts if not c.footprint and c.on_board),
                parts_missing_datasheet=sorted(
                    c.ref for c in parts if c.in_bom and (not c.datasheet or c.datasheet == "~")
                ),
            )
            if self._sch.warnings:
                d["warnings"] = self._sch.warnings
        if self._board:
            d["board_summary"] = self._board.summary()
        return d
