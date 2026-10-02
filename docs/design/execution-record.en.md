# Execution Record Format (schema 1)

[中文](execution-record.md)

Each workflow that writes a lens leaves `execution-record.json` in its own run directory, as the only data source for the design record (walkthrough) generated later. The generator only renders numbers from the record and does not recompute or estimate; missing items are stated as missing.

| `action` | Writer | Run directory | Description |
| --- | --- | --- | --- |
| `scale` | `codev_mcp.scale` | Scaling output package | Equivalent commands of the typed scaling transaction, EFL check, solve coupling |
| `glass_candidates` | `codev_mcp.glass_near` | Query run directory | Lists candidates only and writes no lens; no record is written on failure |
| `edit` | `codev_mcp.edit` | Edit output package | Equivalent commands of the typed `update_lens` transaction (for example a glass swap) and first-order data before and after |
| `aut` | `codev_mcp.aut prepare` | AUT root directory | Actual command blocks of each stage, constraint results, candidate lens; `status` is the candidate state (`complete`/`failed`) |
| `aut_accept` | `codev_mcp.aut accept` | The same AUT root directory (appended) | Accepted revision number and published lens hash |
| `export_seq` | `codev_mcp.export_seq` | Export run directory | Sends only `RES`/`WRL` |
| `evaluate` | `codev_mcp.compare` | Evaluation output package | Specification judgement, figure list; native plot commands are taken from the run warnings |

`codev_mcp.walkthrough` only reads the directories above and generates a design record in Chinese; it writes no execution record.

```json
{
  "schema_version": 1,
  "kind": "execution_record",
  "steps": [
    {
      "action": "scale",
      "tool": "codev_mcp.scale",
      "status": "succeeded",
      "source": "codev",
      "recorded_at": "2026-09-28T12:59:09+00:00",
      "inputs": [{"role": "lens", "path": "...", "sha256": "..."}],
      "parameters": {"target_efl": 50.0, "factor": 0.4999975640553},
      "native_commands": ["RDY S1 28.7247427809", "THI S1 4.3733076187", "..."],
      "command_note": "Sent by update_lens as one typed transaction after RES of the input; ...",
      "results": {"efl_check": {}, "first_order": {}, "solve_coupled": [], "left_to_codev": []},
      "outputs": [{"path": "...", "sha256": "...", "bundle_copy": "scaled.len"}],
      "bundle": "...",
      "error": null
    }
  ]
}
```

- `native_commands` are the CODE V commands the service actually sent, or commands equivalent to them word for word, in execution order; `command_note` explains preconditions (for example which file to `RES` first) and items not determined by these commands (for example values re-derived by solves).
- Records with `source` set to `simulated` cannot serve as optical conclusions, and the generator must mark them clearly.
- Failed steps are written too (except `glass_near`, where a failed query only reports an error and leaves no record), with `status=failed` and `error`, and no output files.
- Inputs and outputs both carry SHA-256, so several runs can be chained (the output hash of one step equals the input hash of the next).

New step types follow the same structure; fields are only added, never changed.
