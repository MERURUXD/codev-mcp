# 本机玻璃目录查询

`python -m codev_mcp.glass` 是独立的只读 CLI，不增加 MCP 工具。它通过版本化 `CodeV.Command.102` 在本机查询 CODE V 10.2 预装目录，查询过程使用服务自建的无界面会话，不读取或改写用户镜头。

```powershell
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
.\.venv\Scripts\python.exe -m codev_mcp.glass --name BK7 --catalog SCHOTT --wavelength-nm 550
.\.venv\Scripts\python.exe -m codev_mcp.glass --name BK7
```

默认从 32 位的 `CodeV.Command.102` 注册项定位安装目录。传入 `--install-root` 时，该路径必须与注册的 COM 安装目录一致；不一致时在启动会话前拒绝查询，防止把另一份 `glass.cat` 的哈希附在当前引擎结果上。目录文件只读取 SHA-256、大小、修改时间和格式标识，不解析、不复制、也不随仓库分发。每次查询的详细原生输出写入独立 `.codev-run/glass-query-*` 目录，JSON 返回该路径和哈希。

结果的 `status` 为 `found`、`not_found`、`ambiguous` 或 `catalog_unavailable`。未指定目录时逐一搜索 14 个手册列出的预装目录；同名玻璃只列候选，需再传 `--catalog` 明确选择。`not_found` 可列相似拼写，但绝不自动替换。不支持的目录名或本机缺少 `glass.cat` 返回 `catalog_unavailable`。任一目录的 `GLI` 页眉、玻璃行或结束标记无法核实，则整次查询失败，不能以其他目录的匹配报告 `found` 或 `not_found`。CLI 对 `found` 返回退出码 0，对这些未命中状态返回 2，对参数、COM 或无法核实的输出返回 1。

给出 `--wavelength-nm` 时，CLI 从 CODE V 的 `GLD;REL` 原生列表中按目录、名称、六位代码提取指定波长的折射率。`index.available=false` 表示本次原生输出未能核实该数值；不能当作材料物理上不存在。值仅有列表打印精度，目录自身的适用波段与精度说明以本机 `detail.txt` 为准，不据此作玻璃替代或设计可行性判断。查询范围是预装目录，不覆盖用户私有目录、熔次数据或厂商目录更新。



## 相近玻璃候选

`python -m codev_mcp.glass_near` 只读列出**调用方指定目录**中与参考玻璃 nd／νd 相近的玻璃，不预设目录、不替换镜头中的玻璃。

```powershell
.\.venv\Scripts\python.exe -m codev_mcp.glass_near --glass SK16_SCHOTT --catalog CDGM --catalog HOYA --limit 5
.\.venv\Scripts\python.exe -m codev_mcp.glass_near --nd 1.62041 --vd 60.3 --catalog CDGM
```

- 参考玻璃写成 `名称_目录`，从其目录的 `GLI` 行读取；或直接给 `--nd`、`--vd`。`--catalog` 可重复，必须是 CODE V 10.2 预装目录，没有默认值。
- nd 取 `GLI` 587.6 nm 列；νd = (nd − 1)／(n486.1 − n656.3)，由同一行的打印值计算，属于服务计算值。`GLI` 只打印五位小数，结果给出 νd 的最坏情况区间（例如 SK16 约 ±0.06），可与六位玻璃代码对照；计算值与厂商公布的 νd 可能在该区间内不同。
- 排序距离为 √((Δnd／0.01)² + (Δνd／1)²)，比例可用 `--nd-scale`、`--vd-scale` 改变。它只是本服务的排序约定，不代表光学等效；结果同时列出 Δnd 与 Δνd。
- 每个目录列表按列位置解析，页眉必须只含所请求目录、以 `Command End:` 结束，缺少 587.6／486.1／656.3 nm 数值的行不参与排序。原生列表保存在独立 `.codev-run/glass-near-*` 目录并记录哈希，目录文件哈希前后核对。
- 实际替换经 `update_lens`（或 `python -m codev_mcp.edit`）完成，再用受控 AUT 重新优化。
