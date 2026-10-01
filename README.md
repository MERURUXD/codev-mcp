# CODE V MCP

[![tests](https://github.com/MERURUXD/codev-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/MERURUXD/codev-mcp/actions/workflows/test.yml)
[English](README.en.md) · Windows · CODE V 10.2 · Python 3.10+ · 实验性 0.1.0

把本机的 CODE V 10.2 接入 MCP 客户端（如 Claude Desktop、Claude Code）。客户端可以用自然语言让它打开镜头、读取和修改参数、运行一阶／点列／MTF／波前分析、导出 CODE V 原生图，并把结果另存。服务通过 Windows COM 驱动一个自己启动的无界面 CODE V 会话，只在本机经 stdio 通信。

除 MCP 工具外，仓库还提供一组独立命令行工具，用于镜头评价与对比、参数扫描、整体缩放、换玻璃、受控 AUT 优化候选、多方案比较和报告素材整理。

> [!NOTE]
> 真实后端需要已安装、已注册 COM 并有可用许可证的 CODE V 10.2。CODE V 是 Synopsys 的商业软件，不随本项目提供；本项目与 Synopsys 无关联。没有 CODE V 时可以用模拟后端体验协议和流程，模拟结果一律标记为 `simulated`，不能作为光学结论。

## 能做什么

### MCP 工具

| 工具 | 作用 |
| --- | --- |
| `get_status` | 查看后端、CODE V 版本、会话与镜头状态、当前任务 |
| `open_lens` | 打开 `.len` 的工作副本，返回完整镜头数据 |
| `create_lens` | 在空会话中按类型化规格新建单变焦球面镜头，可加近轴像面求解（`PIM`） |
| `get_lens` | 读取表面、半径、厚度、玻璃、孔径、视场（含渐晕因子）、波长、变焦位置 |
| `update_lens` | 批量修改半径／厚度／玻璃、视场、波长、系统孔径值等；整批回读核对，失败整批回滚 |
| `edit_lens_structure` | 对本服务新建的简单球面镜头插入／删除表面、设置光阑 |
| `run_analysis` | 一阶参数、点列图、衍射 MTF、波前 RMS／Strehl，或导出 CODE V 原生绘图（PNG） |
| `get_analysis` | 查询任务状态，取回数值结果与图像 |
| `cancel_analysis` | 取消点列图或原生绘图任务，并区分“已请求”与“已确认停止” |
| `save_lens_as` | 另存为新文件，不覆盖源文件或已有文件 |
| `close_session` | 释放本服务创建的 CODE V 会话 |

每项的参数边界、数值精度与口径见[支持能力与限制](docs/capabilities.md)。

### 独立命令行工具

均以 `python -m codev_mcp.<名称>` 运行，复用上面的工具或服务自己的会话，不提供任意命令入口。

| 名称 | 用途 | 说明 |
| --- | --- | --- |
| `compare`、`batch` | 单镜头按设计规格评价；初始／最终镜头对比；批量逐对报告 | [对比与评价](docs/comparison-workflow.md)、[批量](docs/batch-workflow.md)、[设计规格](docs/design/design-spec.md) |
| `scan` | 对一个表面的半径或厚度做固定网格扫描 | [参数扫描](docs/parameter-scan.md) |
| `scale` | 按目标焦距或系数整体缩放镜头 | [能力清单](docs/capabilities.md#起点缩放) |
| `edit`、`fieldset` | 按文件做类型化编辑（如换玻璃）、生成视场集合 | [视场集合与导入](docs/field-set-and-seq-import.md) |
| `glass`、`glass_near` | 查询本机玻璃目录、列出相近玻璃 | [玻璃目录](docs/glass-catalog.md) |
| `aut` | 按类型化规格生成分阶段 AUT 优化候选，人工确认后再显式接受 | [受控 AUT](docs/controlled-aut.md) |
| `schemes` | 从同一基线运行多个设计方案并汇总比较，只推荐不接受 | [多方案比较](docs/scheme-comparison.md) |
| `export_seq`、`seq_import` | 导出可读 `.seq`；按白名单导入 `.seq`（不执行序列） | [视场集合与导入](docs/field-set-and-seq-import.md) |
| `deliver`、`walkthrough` | 整理报告用的图、数据与宏；从执行记录生成设计记录 | [交付整理](docs/deliverable.md)、[执行记录](docs/design/execution-record.md) |

命令行工具的引擎工作目录由安装位置决定，请按下面的步骤在仓库根目录做可编辑安装后使用。

## 怎么保证不弄坏镜头

```text
MCP 客户端 ──stdio──> codev-mcp 服务 ──JSON 行──> 工作进程 ──COM──> 无界面 CODE V 会话
```

- **工作副本**：打开镜头时复制一份再操作，原文件不动；另存拒绝覆盖源文件和已存在的文件。
- **事务式修改**：每批修改前建恢复点，修改后逐项回读；任何一项不符（包括 CODE V 静默忽略修改）整批回滚。
- **检查点**：每次成功修改都保存、重新载入并比对后才报告成功；引擎中途崩溃时自动重建会话并从最近的检查点恢复。
- **没有任意命令**：发给 CODE V 的命令都由服务按类型化参数构造，路径、玻璃名和数值先经过校验。
- **只管自己的进程**：只清理确认由本服务启动的 CODE V 进程，不接管你打开的 CODE V 窗口。
- **结果可追溯**：每个结果带来源（`codev` 或 `simulated`）、单位、分析设置和原生输出；失败返回结构化错误类别。

## 快速开始

### 1. 安装

需要 Windows 和 Python 3.10 及以上（推荐 64 位；64 位 Python 可以驱动 32 位的 CODE V COM 服务）。克隆仓库后在根目录执行 PowerShell：

```powershell
git clone https://github.com/MERURUXD/codev-mcp.git
cd codev-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

### 2. 用模拟后端试运行

不需要 CODE V。下面的脚本经真实 MCP 协议新建一个模拟单透镜、做一阶分析并另存，结果写到 `.codev-run/demo/result.json`：

```powershell
.\.venv\Scripts\python.exe examples/simulated_quickstart.py --output .codev-run/demo
```

输入与输出说明见[模拟入门](examples/README.md)。

### 3. 接入 MCP 客户端

在客户端的 MCP 配置里加入下面这段，把路径换成你的仓库位置（JSON 里反斜杠要写两次）。没有 CODE V 时把 `com` 改成 `simulated`：

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

重启客户端后先调用 `get_status`，确认后端为 `com` 且能读到 CODE V 版本。工作目录存放会话、检查点和结果图片，必须可写，建议使用较短的纯英文路径。其他选项（超时、解释器、环境变量）和故障排查见[安装说明](docs/install.md)。

### 4. 开始使用

可以直接对客户端这样说：

- “打开 `D:\lenses\design.len`，列出每个面的半径、厚度和玻璃。”
- “把第 3 面的厚度改成 5 mm，再给出 EFL 和后焦距。”
- “画第 2 视场的点列图，并告诉我 RMS 半径。”
- “计算 10、30、50 lp/mm 的 MTF。”
- “导出 CODE V 的光路图（layout）。”
- “另存为 `D:\lenses\design-v2.len`。”

用设计规格评价一个镜头（命令行）：

```powershell
.\.venv\Scripts\python.exe -m codev_mcp.compare --lens D:\lenses\design.len --spec docs\design\design-spec-dbgauss.json
```

示例规格里的阈值只是演示值，实际使用时按自己的指标填写，格式见[设计规格](docs/design/design-spec.md)。

## 不支持的内容

- 任意 CODE V 命令或宏；
- 在 MCP 工具里自动优化、做公差或灵敏度分析（AUT 只通过 `aut` 命令行生成候选，并且需要显式接受）；
- 对任意已有镜头增删表面、改面型或特殊面型（结构编辑只开放给本服务新建的简单球面镜头）；
- 远程访问，或控制用户已经打开的 CODE V 窗口；
- 无焦系统 MTF、几何 MTF；多变焦镜头的原生绘图与波前分析。

完整清单与注意事项（长会话、同步／异步分析、精度、孔径编辑边界等）见[支持能力与限制](docs/capabilities.md)。

## 文档

| 主题 | 文档 |
| --- | --- |
| 安装、客户端配置与故障排查 | [docs/install.md](docs/install.md) |
| 工具能力、数值口径与限制 | [docs/capabilities.md](docs/capabilities.md) |
| 镜头评价与对比、批量报告 | [comparison-workflow](docs/comparison-workflow.md)、[batch-workflow](docs/batch-workflow.md)、[performance-workflow](docs/performance-workflow.md) |
| 设计规格与执行记录格式 | [design-spec](docs/design/design-spec.md)、[execution-record](docs/design/execution-record.md) |
| 受控 AUT 与多方案比较 | [controlled-aut](docs/controlled-aut.md)、[scheme-comparison](docs/scheme-comparison.md) |
| 参数扫描、视场集合、`.seq` 导入导出 | [parameter-scan](docs/parameter-scan.md)、[field-set-and-seq-import](docs/field-set-and-seq-import.md) |
| 玻璃目录、交付整理 | [glass-catalog](docs/glass-catalog.md)、[deliverable](docs/deliverable.md) |

## 开发

自动化测试使用模拟后端和假 COM 会话，不需要 CODE V 或许可证：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

提交约定与架构约束见 [CONTRIBUTING.md](CONTRIBUTING.md)。问题与建议请提交 [Issue](https://github.com/MERURUXD/codev-mcp/issues)。

## 许可

[MIT](LICENSE)
