# CODE V MCP

[![tests](https://github.com/MERURUXD/codev-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/MERURUXD/codev-mcp/actions/workflows/test.yml)
[中文](README.md) · Windows · CODE V 10.2 · Python 3.10+ · experimental 0.1.0

A local MCP server that lets MCP clients (Claude Desktop, Claude Code and others) drive CODE V 10.2 on the same Windows machine: open lenses, read and edit parameters, run first-order, spot diagram, MTF and wavefront analyses, export CODE V's own plots and save the result. The server talks stdio only and drives a windowless CODE V session it starts itself over COM.

Separate command-line tools cover lens evaluation and comparison, parameter scans, scaling, glass swaps, controlled AUT optimization candidates, scheme comparison and report material.

> [!NOTE]
> The real backend needs CODE V 10.2 installed, its COM server registered (`CodeV.Command.102`) and a working license. CODE V is commercial software by Synopsys and is not included; this project is not affiliated with Synopsys. Without CODE V you can try the protocol with the simulated backend; its results are always marked `simulated` and carry no optical meaning.

## Features

**MCP tools (11):** `get_status`, `open_lens`, `create_lens`, `get_lens`, `update_lens`, `edit_lens_structure`, `run_analysis`, `get_analysis`, `cancel_analysis`, `save_lens_as`, `close_session`. Limits, precision and units for each are listed in [capabilities](docs/capabilities.en.md).

**Command-line tools** (`python -m codev_mcp.<name>`): `compare`, `batch`, `scan`, `scale`, `edit`, `fieldset`, `glass`, `glass_near`, `aut`, `schemes`, `export_seq`, `seq_import`, `deliver`, `walkthrough`. They reuse the public tools or the server's own sessions and never accept arbitrary commands. Use them from an editable install in the repository root, because their engine work directories follow the install location.

**Safety:** edits run on a working copy; each batch is read back and rolled back as a whole if anything differs; every successful edit is saved to a verified checkpoint used for automatic recovery after an engine crash; all CODE V commands are built from validated, typed parameters; only processes started by this server are cleaned up; `save_lens_as` never overwrites the source or an existing file.

**Not supported:** arbitrary commands or macros, optimization or tolerancing inside the MCP tools (AUT is a separate CLI that produces candidates you accept explicitly), adding or removing surfaces in arbitrary existing lenses, remote access, and controlling a CODE V window you opened yourself.

## Quick start

```powershell
git clone https://github.com/MERURUXD/codev-mcp.git
cd codev-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
# Simulated end-to-end demo over real MCP stdio; no CODE V needed
.\.venv\Scripts\python.exe examples/simulated_quickstart.py --output .codev-run/demo
```

MCP client configuration (replace the paths; use `simulated` instead of `com` without CODE V):

```json
{
  "mcpServers": {
    "codev": {
      "command": "D:\\path\\to\\codev-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "codev_mcp", "--backend", "com",
               "--working-directory", "D:\\path\\to\\codev-mcp\\.codev-run"]
    }
  }
}
```

After restarting the client, call `get_status` to confirm the backend and CODE V version. Further options and troubleshooting are in the [installation guide](docs/install.en.md), the other guides are listed in the [documentation index](docs/README.en.md), and the demo is described in [examples](examples/README.en.md).

## Development and license

Automated tests use the simulated backend and a fake COM session and need no CODE V license: `python -m unittest discover -s tests -t .` with `src` on `PYTHONPATH`. See [CONTRIBUTING.md](CONTRIBUTING.md).

Code is released under the [MIT License](LICENSE). CODE V software, licenses, vendor manuals and sample libraries are not covered and not distributed.
