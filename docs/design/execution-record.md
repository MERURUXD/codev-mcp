# 执行记录格式（schema 1）

[English](execution-record.en.md)

每个写镜头的工作流在自己的运行目录留下 `execution-record.json`，作为之后生成设计记录（walkthrough）的唯一数据来源。生成器只渲染记录中的数值，不重算、不估算；缺失项写明缺失。

| `action` | 写入者 | 运行目录 | 说明 |
| --- | --- | --- | --- |
| `scale` | `codev_mcp.scale` | 缩放输出包 | 类型化缩放事务的等价命令、EFL 核对、求解联动 |
| `glass_candidates` | `codev_mcp.glass_near` | 查询运行目录 | 只列候选，不写镜头；失败时不写记录 |
| `edit` | `codev_mcp.edit` | 编辑输出包 | 类型化 `update_lens` 事务（例如换玻璃）的等价命令与一阶参数前后值 |
| `aut` | `codev_mcp.aut prepare` | AUT 根目录 | 各阶段实际命令块、约束结果、候选镜头；`status` 取候选状态（`complete`／`failed`） |
| `aut_accept` | `codev_mcp.aut accept` | 同一 AUT 根目录（追加） | 接受修订号与发布镜头哈希 |
| `export_seq` | `codev_mcp.export_seq` | 导出运行目录 | 只发送 `RES`／`WRL` |
| `evaluate` | `codev_mcp.compare` | 评价输出包 | 规格判定、图表清单；原生绘图命令取自运行警告 |

`codev_mcp.walkthrough` 只读取上述目录并生成中文设计记录，不写执行记录。

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

- `native_commands` 是服务实际发送、或与之逐字等价的 CODE V 命令，按执行顺序排列；`command_note` 说明前置条件（例如先 `RES` 哪个文件）和不由这些命令决定的项（例如求解重新推导的值）。
- `source` 为 `simulated` 的记录不能作为光学结论，生成器必须明显标注。
- 失败的步骤同样写入（`glass_near` 除外，查询失败只报错不留记录），`status=failed` 并带 `error`，不给出输出文件。
- 输入与输出都带 SHA-256，便于把多次运行串成一条链（上一步的输出哈希等于下一步的输入哈希）。

新增步骤类型沿用同一结构；新增字段只增不改。
