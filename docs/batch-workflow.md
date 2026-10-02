# 批量镜头报告

[English](batch-workflow.en.md)

`python -m codev_mcp.batch` 通过公共 MCP 工具串行分析。第一份 `--lens` 是参考镜头，其余镜头逐份与它比较；每对镜头使用独立的、不可覆盖的对比包，输入按绝对路径生成稳定标识。

```powershell
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
python -m codev_mcp.batch --lens D:\lenses\base.len --lens D:\lenses\candidate-a.len --lens D:\lenses\candidate-b.len --analyses first_order,spot_diagram,mtf --fields 1 --mtf-frequencies 0,20,40 --spot-grid 5
```

`--backend` 默认 `com`；`--output-dir` 默认 `.codev-run/batches`；`--timeout`、`--allow-field-weight-difference` 及分析配置选项与[双镜头流程](comparison-workflow.md)相同。模拟后端只按文件名选内置样本，不读取输入 `.len` 光学数据。

每次运行生成独立目录。`batch.json` 保存输入哈希、每对状态、明细包路径、同请求且同实际设置的数值差异；`batch.md` 展示这些差异并链接到各对的 `comparison.md`。指标记录 `unit`；MTF 还分别记录 `frequency` 与 `frequency_unit=cycles/mm`。长度和点列半径沿用结果的镜头单位，WAV RMS 用 `waves`，F 数、Strehl 与 MTF 调制度用 `1`（表格显示“无量纲”）。第一份参考镜头在每对中独立重算。

两镜头不兼容或某项失败时，该对标记失败且不进入数值汇总，后续镜头继续运行；任一对失败或输入变化使批次退出码为 1。最终输入复核遇到文件缺失或不可读时，清单记录 `unchanged=false` 与错误，仍生成失败报告。用户中断时停止后续镜头，保存 `status=interrupted`，命令行退出码为 130；当前对的会话清理状态以其独立对比包为准。无跨不同设置的排名或总评分。

多变焦数值分析可用 `--zoom-positions 1,2` 显式选择位置，限定一阶、点列和数值 MTF。WAV 与 CODE V 原生图仍只支持单变焦镜头。
