# kicad-mcp

An open source [Model Context Protocol](https://modelcontextprotocol.io) server that lets an LLM
(Claude Desktop, Claude Code, or any MCP client) read, check and work with your
[KiCad](https://www.kicad.org) 9 / 10 projects.

Ask things like:

- "Open `~/projects/amp/amp.kicad_pro` and give me an overview."
- "Is U1's VCC pin connected to C3?"
- "What's on the `/SDA` net, and does it have a pull-up?"
- "Run ERC and explain the errors."
- "Which parts are missing a datasheet or footprint?"

## Roadmap

| Step | What | Status |
| --- | --- | --- |
| 1 | Read and check a design: components, nets, pins, ERC, DRC, board info | **done (v0.1)** |
| 2 | Parts pipeline: search Mouser / DigiKey, download datasheets, symbols and footprints into a project library, purchase-ready BOM | planned |
| 3 | Datasheet assistant (answers with page references, pin-table checks) and schematic editing (place and wire parts) | planned |
| 4 | SPICE: export netlist, run ngspice, generate behavioral models from datasheets, KiCad plugin button | planned |

## Install

Requires Python 3.10+ and KiCad 9 or 10 (KiCad provides `kicad-cli`, used for ERC, DRC and exact netlists).

```bash
git clone https://github.com/rinku404/kicad-mcp
cd kicad-mcp
uv venv && uv pip install -e .      # or: python -m venv .venv && .venv/bin/pip install -e .
```

`kicad-cli` is found automatically on `PATH` or in KiCad's default install folder
(`C:\Program Files\KiCad\10.0\bin`, `/Applications/KiCad/KiCad.app/Contents/MacOS`, `/usr/bin`).
Otherwise set `KICAD_CLI` to its full path.

### Claude Desktop

Add to `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "kicad": {
      "command": "/full/path/to/kicad-mcp/.venv/bin/kicad-mcp"
    }
  }
}
```

On Windows the command is `C:\\path\\to\\kicad-mcp\\.venv\\Scripts\\kicad-mcp.exe`.

### Claude Code

```bash
claude mcp add kicad -- /full/path/to/kicad-mcp/.venv/bin/kicad-mcp
```

## Tools

| Tool | What it does |
| --- | --- |
| `open_project` | Open a `.kicad_pro` / `.kicad_sch` / `.kicad_pcb` / folder and get an overview |
| `project_summary` | Sheets, part and net counts, parts missing footprints or datasheets, board stats |
| `list_components` | Components with value, footprint, datasheet and custom fields; optional text filter |
| `get_component` | One part's fields, every pin with its net and what it connects to, PCB placement |
| `list_nets` / `get_net` | Nets with their pins, pin names and electrical types |
| `check_connection` | Are two pins (e.g. `U1.VCC`, `C3.1`) on the same net? |
| `run_erc` | KiCad electrical rules check |
| `run_drc` | KiCad design rules check, including schematic/board parity |
| `list_footprints` | Footprints with side, position, rotation and pad nets |
| `kicad_status` | Is `kicad-cli` available, and which version |

Files are re-read when they change, so save in KiCad and ask again.

## How it works

The server parses KiCad's S-expression files directly, so it works without KiCad running.
When `kicad-cli` is available, nets come from KiCad's own netlist export, which is authoritative.
Without it, a built-in connectivity solver is used; it matches KiCad 10's netlist exactly on
KiCad's flat and hierarchical demo projects, but does not yet understand buses.

## Development

```bash
uv pip install -e ".[dev]"
pytest
```

Tests that run ERC and DRC are skipped when `kicad-cli` is not installed.

## License

MIT
