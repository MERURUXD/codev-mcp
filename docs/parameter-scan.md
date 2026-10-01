# 固定网格参数扫描

`python -m codev_mcp.scan` 使用公共 MCP 工具扫描单变焦镜头的一个现有表面半径或厚度。每个样本在独立服务会话中打开同一基线快照的独立副本，事务编辑后回读，再运行当前焦面的一阶参数分析。它不执行 CODE V AUT，不挑选或发布最优镜头，也不修改输入文件。

```powershell
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
.\.venv\Scripts\python.exe -m codev_mcp.scan --lens D:\lenses\singlet.len --surface 1 --parameter radius --values 60,61
```

`--backend` 默认为 `com`；`simulated` 仅验证编排，结果无光学意义。`--timeout` 默认 300 秒；`--output-dir` 默认 `.codev-run/scans`。`--values` 必须为 2～16 个互不重复的有限数值；一个扫描只改一个表面的一个参数，不支持多变焦、无穷基线参数、物面或像面。拒绝的求解／拾取参数由现有 `update_lens` 事务保护处理，单个失败样本会独立标记，后续样本仍从原始基线开始。

每次运行新建目录，`manifest.json` 记录输入哈希、网格、每个样本状态、回读值、任务 ID 和镜头版本；`metrics.csv` 和 `scan.md` 列出请求／回读参数、EFL、F/#、像距和总长。每个样本保留请求、事务结果、分析快照、镜头读回、MCP 协议日志和清理记录。任一失败、清理无法确认，或输入及快照哈希变化、无法核对，都令本次运行失败并返回退出码 1；成功返回 0。指标沿用一阶分析自带的单位与精度说明，详细值和原始输出在各样本的 `snapshot.json` 中。

状态核对只覆盖公共 `LensData` 暴露的光学字段。修改后的求解联动由原有事务回读与检查点机制核对；分析前后要求公共光学状态及镜头版本不变。扫描表不按指标排序，也不推断像质优劣。

扫描分别保留主错误、清理错误与输入核对错误；中断或清理无法确认时停止后续样本，普通样本计算失败在清理确认后仍可继续。`manifest.json` 的 `interrupted`、`cleanup_unconfirmed` 和 `error_sources` 记录对应原因。

使用 `--config` 启用固定焦面像质扫描：可选一阶、逐视场点列、数值 MTF、WAV；输出条件绑定的指标表、曲线与逐项评价。不指定配置时默认只做一阶分析。真实 CODE V 只接受可确认没有求解／拾取关系的固定焦面镜头；扫描空气间隔时目标面后必须为空气且下一面不是像面。显式选择的样本另存并在独立会话重开复算，通过后才标记为可交付。
