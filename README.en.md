# CODE V MCP

Experimental **0.1.0** for Windows and CODE V 10.2. A local stdio MCP server using an owned COM worker to read lenses, make typed edits and run bounded optical analyses. CODE V is commercial software and requires a registered `CodeV.Command.102` and a working license.

The simulated backend needs no CODE V. Its results carry `source=simulated` and are not optical evidence. Start with the [Chinese README](README.md), [installation instructions](docs/install.md), [capabilities](docs/capabilities.md) and [reproducible simulated example](examples/README.md).

There are eleven public tools. No arbitrary commands/macros, remote service, GUI takeover, general optimisation or tolerance analysis. AUT uses a separate typed CLI: prepare a candidate, inspect it, then explicitly accept. Calls within a COM session are serial; comparison defaults to isolated sessions, with bounded reuse only for single-zoom lenses.

Project code is under the [MIT License](LICENSE). CODE V software, license, manuals and vendor sample library are excluded.
