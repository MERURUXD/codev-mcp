# 交付整理

[English](deliverable.en.md)

独立 CLI `python -m codev_mcp.deliver` 把已经跑完的评价包和 AUT 运行整理成报告素材，按报告里常用的目录和文件名排好，并生成资料索引。它只复制、核对哈希和排版：**不重算任何数值，不向 CODE V 发送命令，不写输入**，目标目录必须不存在（已存在即拒绝）。

```powershell
.\.venv\Scripts\python.exe -m codev_mcp.deliver --comparison <工作目录>\compare-bundle --aut <工作目录>\aut-run --seq <工作目录>\final.seq --output-dir <工作目录>\deliverables --title "双高斯物镜"
```

- `--comparison`：`codev_mcp.compare` 的运行包，初始／最终对比或单镜头评价均可（单镜头一律用 `final-` 前缀）。
- `--aut`：`codev_mcp.aut` 的运行目录，可重复（第二个起文件名带 `-2`、`-3`）。
- `--setup`：优化之前的类型化修改（`codev_mcp.edit`）、缩放（`codev_mcp.scale`）或 `.seq` 导入（`codev_mcp.seq_import`）的运行目录，可重复；取其执行记录里的等效命令写成 `macro/setup-*.seq`，放在优化宏之前使用（例如换玻璃的 `GLA` 命令）。
- `--seq`：一并放入的 `.seq`／`.len` 文件，可重复。
- 至少给出其中一项；可以任意组合。

## 目录布局

| 位置 | 内容 |
| --- | --- |
| `figures/init-*.png`、`figures/final-*.png` | 初始／最终成对的图：CODE V 原生绘图（layout、spot、mtf、ray_aberration、field_aberration，同名 `.PLT` 一并复制）与服务按数值重绘的点列图和 MTF；文件名沿用报告习惯（`init-`／`final-` + 分析名） |
| `lis/init-lis.txt`、`lis/final-lis.txt` | 各镜头的原生 `LIS` 文本（评价时 `get_lens` 读取的只读输出） |
| `wav/`、`first-order/`、`raw/` | CODE V 原生文本输出：WAV 原文、一阶参数、点列与 MTF 的原文 |
| `macro/setup-*.seq` | `--setup` 给出的修改／缩放／导入的等效命令（例如换玻璃的 `GLA` 行） |
| `macro/optimize-verbatim.seq` | 服务实际发送的优化命令块（含零循环核对、输出重定向、控制码恢复、中间保存，原样） |
| `macro/optimize-equivalent.seq` | **等效手写宏**：每个阶段只保留视场／权重修改、`FRZ` 与变量开放、误差函数设置、变量界、约束和一个 `GO`（阶段含 `SET VIG` 时保留）；文件头列出省略的内容 |
| `runs/` | 各阶段的 AUT 原始输出（`OUT` 重定向文件）和 `aut-summary.md`（阶段、结束原因、`ERR. F.` 初始 → 最终、约束与变量界判定） |
| `evaluation/` | 规格逐项判定 `evaluation.json`、对比报告、评价时使用的设计规格副本 |
| `lens/` | 通过 `--seq` 给出的文件 |
| `index.md`、`manifest.json` | 资料索引（每个文件的说明、大小、SHA-256 前 12 位）和机器可读清单（含来源路径、完整哈希、未能生成的项） |

## 精简宏与原样命令块

精简宏由**记录的命令块**按固定位置标记裁剪，不是重新生成：记录的每个阶段是“修改与变量开放 → 零循环核对（`aut; err cdv; mxc 0; vli y; go`）→ AUT 设置、变量界、约束 → `out <文件>` → `go` → `out t` → 控制码恢复 → `sav <文件>`”。标记找不到或顺序不符时，该阶段在精简宏里写成注释并在索引的“说明”中列出，原样命令块不受影响。

差别（也写在索引里）：精简宏省略零循环变量表核对、`tim` 与 `vli y`、输出重定向、控制码恢复和候选保存。因此手写宏运行后，变量的开放标记留在镜头里，不写候选文件，也不核对变量表；优化命令本身与服务发送的相同。

## 初始镜头无法完整追迹

大视场无渐晕的起点镜头可能有些分析根本得不到结果（例如网格光线全被光阑挡住）。这时初始图只包含实际生成的部分，每个缺失项都在索引的“未能生成的项”里给出运行记录中的原因，`manifest.json` 的 `unavailable` 同步列出，说明里注明有多少项缺失。评价包本身在单项失败时继续运行其余分析（见[对比流程](comparison-workflow.md)），所以这种情况下仍能整理出最终镜头的全套素材。

## 使用限制

只整理已有的运行包，不判断设计好坏；图像与 `.PLT` 不做内容校验（只核对复制后的哈希）；AUT 记录必须来自本服务的 `codev_mcp.aut`。
