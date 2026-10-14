"""Optional settings file, so API keys live in one place instead of in every MCP client's config.

Location: ``%APPDATA%\\kicad-mcp\\config.json`` on Windows, ``~/.config/kicad-mcp/config.json``
elsewhere (or the path in ``KICAD_MCP_CONFIG``). It is a flat JSON object of the same names as
the environment variables, e.g. ``{"MOUSER_API_KEY": "...", "DIGIKEY_CLIENT_ID": "..."}``.
Real environment variables win over the file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def config_path() -> Path:
    if os.environ.get("KICAD_MCP_CONFIG"):
        return Path(os.environ["KICAD_MCP_CONFIG"])
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "kicad-mcp" / "config.json"


def get(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    try:
        value = json.loads(config_path().read_text(encoding="utf-8")).get(name)
    except (OSError, ValueError, AttributeError):
        return None
    return str(value) if value else None
