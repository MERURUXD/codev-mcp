# Bounded Session Reuse in the Comparison Workflow

[中文](performance-workflow.md)

Single-lens evaluation and two-lens comparison use one session per analysis by default. After explicitly passing `--max-analyses-per-session 2..8`, each session of the same lens runs at most that many consecutive analyses; preflight still has its own session, and sessions are never shared across lenses. First-order, spot, numeric MTF, WAV and the five native plot types on single-zoom lenses can all reuse sessions, and full specification evaluation is supported too; multi-zoom reuse is refused. All COM calls still run serially on the single thread of their worker process.

```powershell
python -m codev_mcp.compare --lens D:\lenses\design.len --spec docs/design/design-spec-dbgauss.json --max-analyses-per-session 8
```

## State and failure handling

Each analysis checks the public lens state, task source and settings before and after. WAV keeps the full snapshot check; native plots run the `GRA T` wrap-up on success, failure or cancellation. When a single computation fails or is truncated, the current reuse group is closed, and only after cleanup is confirmed does a new session open from the input snapshot to continue with the remaining items; the overall result is still a failure. An invalid session, state drift, a timeout, a protocol fault or unconfirmed cleanup stops the run.

This option is a bounded orchestration option, not a fixed safe call threshold of CODE V. Whether it saves time depends on the lens, the analysis kinds, sampling and the license environment.

## Viewing timings

In each comparison package's `manifest.json`, `performance.sessions` records client initialization, CODE V start-up, lens open, release, the actual number of COM calls and cleanup confirmation; analysis entries record computation, export and read-back times.

These are wall-clock times, not pure CODE V CPU time. Computation time includes MCP submission and polling, and export time includes local outputs and image checks. CODE V start-up time is included in the lens open time, so the two must not be added. In reuse mode, start-up and release count only for the session they belong to and should not be added again for every analysis. The simulated backend provides no CODE V start-up time or COM call count.
