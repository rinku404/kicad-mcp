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
- "Find an LM358 in SOIC-8 on Mouser and assign it to U1, with the datasheet."
- "Export a BOM for 10 boards with current prices."

## Roadmap

| Step | What | Status |
| --- | --- | --- |
| 1 | Read and check a design: components, nets, pins, ERC, DRC, board info | **done (v0.1)** |
| 2 | Parts pipeline: search Mouser / DigiKey, download datasheets, symbols and footprints into a project library, purchase-ready BOM | **done (v0.2)** |
| 3 | Datasheet assistant (search and read datasheets with page numbers) and schematic editing (place and wire parts) | **done (v0.3)** |
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

### Antigravity CLI (Google)

```bash
agy mcp add kicad /full/path/to/kicad-mcp/.venv/bin/kicad-mcp
```

To stop it asking before every call, add `"mcp(kicad/*)"` to `permissions.allow` in
`~/.gemini/antigravity-cli/settings.json`.

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

Parts, libraries and BOM:

| Tool | What it does |
| --- | --- |
| `distributor_status` | Which distributor APIs are configured, and where KiCad's libraries are |
| `search_parts` | Keyword search on Mouser and DigiKey with price breaks, stock and links |
| `get_part` | Full details and parameters for one part number |
| `assign_part` | Write MPN, manufacturer, Mouser/DigiKey part numbers and links, and the datasheet into a symbol; save the PDF in `datasheets/` |
| `set_fields` | Set any fields on a symbol (value, footprint, MPN, ...) |
| `download_datasheet` | Save a datasheet PDF into the project |
| `find_library_symbols` / `find_library_footprints` | Search KiCad's stock libraries |
| `add_library_symbol` | Copy a stock symbol into the project library, renamed and with fields set |
| `create_symbol` | Build a symbol from a pin table (for example, read from a datasheet) |
| `import_part_zip` | Import a KiCad-format ZIP from Mouser/SamacSys, SnapEDA or Ultra Librarian |
| `export_bom` | Purchase-ready CSV in `bom/`, grouped, multiplied by board count, optionally priced |

New symbols, footprints and 3D models go into a library inside the project (`libs/<project>.kicad_sym`,
`libs/<project>.pretty`, `libs/3dmodels`), registered in the project's library tables.
Tools that change the schematic write the file directly: if it is open in KiCad, use
File → Revert afterwards so KiCad does not overwrite the change when you save.

### Distributor API keys (optional)

Search, pricing and `assign_part` need at least one of these (both are free):

- **Mouser**: request a Search API key at <https://www.mouser.com/api-hub/>.
- **DigiKey**: create an app with the *Product Information v4* API at <https://developer.digikey.com/>
  to get a client ID and secret. Optional: `DIGIKEY_LOCALE_SITE` (e.g. `BR`), `DIGIKEY_LOCALE_CURRENCY` (e.g. `BRL`).

Put them in a settings file, `%APPDATA%\kicad-mcp\config.json` on Windows or
`~/.config/kicad-mcp/config.json` elsewhere:

```json
{
  "MOUSER_API_KEY": "...",
  "DIGIKEY_CLIENT_ID": "...",
  "DIGIKEY_CLIENT_SECRET": "..."
}
```

Environment variables with the same names also work and take priority.
Everything else (KiCad library search, symbol creation, ZIP import, BOM without prices) works without keys.

Datasheets and schematic drawing:

| Tool | What it does |
| --- | --- |
| `datasheet_contents` | A part's datasheet sections with page numbers (downloads the PDF into `datasheets/` the first time) |
| `search_datasheet` | Pages where the datasheet talks about something, with the matching lines and printed page numbers |
| `read_datasheet` | Text of chosen pages; `layout` keeps table columns aligned for pin and electrical tables |
| `place_part` | Put a library symbol on the schematic in a free spot (or near another part), with value, footprint and fields |
| `connect_pins` | Connect pins to a net with short wires and net labels, or power symbols for GND, +3V3, +5V and similar |
| `mark_no_connect` | Flag pins that are meant to stay unconnected |

`connect_pins` never draws long wires across the sheet, so it cannot short something by
accident, and every edit is checked against the netlist and undone if any other net would change.
Parts and labels land on KiCad's grid; tidy the layout in KiCad as you like.

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

Tests that run ERC and DRC are skipped when `kicad-cli` is not installed. `tests/data/kicad_share`
holds a few symbols copied from KiCad's stock libraries (CC-BY-SA 4.0 with the KiCad libraries'
exception) so placement tests run without KiCad.

## License

MIT
