# Simulated Walkthrough from an Empty Directory

[中文](README.md)

Download or clone this repository and run in the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps .
.\.venv\Scripts\python.exe examples/simulated_quickstart.py --output .codev-run/demo
```

The input is a [self-built singlet specification](create-singlet.json): ±45 mm spherical surfaces, 4 mm BK7, EPD 8 mm, 650/550/450 nm, 0/3/6°, PIM.
Over the real stdio MCP protocol, the example lists the eleven tools, creates a simulated lens, runs a first-order analysis, saves it as a new file and closes. The output directory must not exist; results are written to `result.json` and `simulated.len`, the protocol log goes to `protocol/`, and internal run files to `work/`.

`simulated.len` is in a simulated format and cannot be opened as a real CODE V lens; simulated numbers prove nothing about real optical performance. Inputs and sources of the native parsing test data are in [fixture sources](../tests/data/project-owned/README.md) (Chinese).
For the real backend configuration see the [installation guide](../docs/install.en.md); lens paths are supplied by the user, and the service opens a working copy.
