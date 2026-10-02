# 多方案比较

[English](scheme-comparison.en.md)

独立 CLI `python -m codev_mcp.schemes`：从**同一基线镜头**出发批量运行多个设计方案（换玻璃、改渐晕或视场集合、换约束集），每个方案是“类型化修改 → 分阶段 AUT → 候选”，最后汇总并列出哪些量可以直接比较、哪些不能。它只推荐，**不接受任何候选**；采用某个候选仍要另外运行 `python -m codev_mcp.aut accept`。不新增 MCP 工具，也不发送新的 CODE V 命令：修改由 [`codev_mcp.edit`](field-set-and-seq-import.md)（一次 `update_lens` 事务）完成，优化由 [`codev_mcp.aut`](controlled-aut.md)（含视场爬升）完成，规格判定可选地由 [`codev_mcp.compare`](comparison-workflow.md) 完成。

```powershell
.\.venv\Scripts\python.exe -m codev_mcp.schemes --lens base.len --schemes schemes.json --aut-spec aut.json --output-dir <工作目录>\new-run [--design-spec spec.json]
.\.venv\Scripts\python.exe -m codev_mcp.schemes --lens base.len --schemes schemes.json --aut-spec aut.json --design-spec spec.json --max-analyses-per-session 8 --jobs 2 --output-dir <工作目录>\new-fast-run
.\.venv\Scripts\python.exe -m codev_mcp.schemes --lens base.len --schemes schemes.json --aut-spec aut.json --screen-cycles 2 --finalists 1 --output-dir <工作目录>\new-screened-run
```

## 方案文件（schema 1）

```json
{"schema_version": 1, "kind": "scheme_set", "name": "...",
 "schemes": [
   {"name": "schott", "reason": "基线"},
   {"name": "cdgm", "edits": [{"target": "surface", "surface": 4, "parameter": "glass", "value": "HF1_CDGM"}]},
   {"name": "vignetted", "field_set": {"fields": [{"y_angle": 0}, {"y_angle": 10, "vuy": 0.3}]}},
   {"name": "loose", "aut_spec": "aut-loose.json"}
 ]}
```

- 1～8 个方案，名称 1～24 个字母、数字、点、下划线或减号，不能重复。每个方案至多一种修改：`edits`（1～100 条公共 `ParameterEdit`）或 `field_set`（公共视场集合替换）；都没有就直接优化基线的副本。
- `aut_spec`（相对方案文件的路径）覆盖默认的 `--aut-spec`，用来比较不同的约束集；规格可含 `field_ramp`。所有规格在运行前校验，缺规格、路径不存在、输出目录已存在或含非 ASCII 字符（CODE V 10.2 要求）都在运行任何 CODE V 之前拒绝。
- 方案默认按文件顺序串行运行（一次一个 CODE V 会话）。`--jobs N`（1～8，即一个方案集最多的方案数）让 N 个方案同时运行：每个方案在自己的工作进程和 CODE V 会话中做与串行完全相同的事，汇总里的顺序仍是方案文件的顺序（见下面的“并行运行”）。单个方案失败（修改被拒、AUT 未完成、超时）只记录该方案并继续其余方案；若某方案留下无法确认的 CODE V 进程，则不再启动新方案、已在运行的自然结束、其余方案标为“未运行”；`Ctrl+C` 保留已完成的部分。

## 两阶段比较（可选）

同时指定 `--screen-cycles N --finalists K`，先对全部方案粗筛，再对入围方案完整优化。不指定时仍对所有方案完整运行。

- `N` 为 1～500 的整数：展开 `field_ramp` 后，每个阶段的 `MXC` 取原值与 N 的较小值，`MNC` 随之限制；变量、约束、误差函数的其余设置、类型化修改和视场爬升顺序保留。粗筛仍执行 AUT 的回读、候选保存重开和 WAV 诊断，不做 `--design-spec` 评价。
- `K` 为 1～8 的整数，表示**每个误差函数可比组**最多入围 K 个，不是跨组的总名额。只在粗筛完成、清理无疑、变量界满足且最终 `ERR. F.` 有限的方案中，按组内 `ERR. F.` 从小到大选择；相同值按方案文件顺序。少量循环后的具体约束未满足可能在后续优化中改善，因此不直接据此淘汰。设置不同的组分别保留候选，不比较跨组的误差函数。
- 完整阶段从原始基线重新执行该方案的类型化修改和原始 AUT 规格；每次独立会话。最终规格评价和推荐只使用完整阶段的结果。粗筛排序可能与完整优化排序不同，减少完整运行次数不保证缩短总时间。
- 粗筛清理存疑、中断或基线哈希变化时，不进入完整阶段；没有有效入围方案时不推荐。`--jobs` 对两阶段分别生效，粗筛全部结束后才启动完整阶段。

两阶段目录为 `screen/<方案>/`、`full/<方案>/`；并行请求分别放在各阶段的 `_workers/`。`summary.json.screening` 保留粗筛指标、排序依据、可比组与入围名单；汇总表并列粗筛与完整结果，未入围方案记为 `screened_out`，其粗筛候选不作为最终候选或推荐。基线已变化或无法核对时，不提供接受建议。

## 并行运行（`--jobs`）

`python -m codev_mcp.schemes ... --jobs 3` 同时运行至多 3 个方案（上限 8，默认 1）。

- 每个方案交给独立的工作进程（`python -m codev_mcp.schemes _scheme <请求文件>`），进程再去调用 `codev_mcp.edit` 与 `codev_mcp.aut`，与串行时完全一样，所以每个方案的隔离、清理和中断行为不变。请求、结果与错误输出放在输出目录的 `_workers/`。
- 各个会话的启动由机器级互斥量排队（一次一个），归属记录因此只含各自的进程；`cvcomsvr.exe` 是所有同时运行的会话共享的，只要还有别的会话在用就不会被结束。
- 一个工作进程没有留下结果就退出，按“清理存疑”处理并停止新方案；`Ctrl+C` 后等待工作进程自己清理（180 秒），仍未结束的被强制结束并标为存疑。串行运行中被 `Ctrl+C` 打断的方案记为 `interrupted`，其后的方案不再运行。
- 清理是否存疑由 `cleanup_confirmed`、`cleanup_remaining` 和各诊断／评价会话的结构化状态判断。最终归属检查确认无残留时，诊断超时只使该方案失败，后续方案可继续；错误消息中的 “cleanup” 字样不决定调度。不同会话的未确认状态仍分别保留。
- 并行会话会增加内存和许可证使用量，按机器资源选择 `--jobs`。替换 `edit_runner`／`aut_runner`／`evaluator`（测试用）只在 `--jobs 1` 下可用。

## 输出

`--max-analyses-per-session 1..8` 只控制 `--design-spec` 的候选评价，默认 1。串行方案和 `--jobs` 工作进程均传递此参数。每个方案内部仍串行，WAV／原生图沿用同一状态和释放核对；评价清理未确认时停止新方案。结果清单与执行记录保留参数，见[性能流程](performance-workflow.md)。

输出目录里有 `summary.md`（中文汇总表）、`summary.json`、`execution-record.json`，以及每个方案的子目录：`edits.json`、`start.len`、`edit/`（修改的运行包）、`aut/`（AUT 运行目录，含 `result.json`、候选与执行记录）、可选的 `evaluation/`。基线镜头的哈希前后必须一致，否则整次运行判为失败。

汇总表列出：状态、阶段数、`ERR. F.` 初始→最终、WAV 加权 RMS 与 Strehl（起始→候选）、约束和变量界是否满足、规格判定（仅给 `--design-spec` 时，对每个候选做完整评价）、候选的一阶参数（括号内是该方案修改后、优化前的起点）。所有数值取自各方案的 AUT 结果与评价包，只有接受光瞳比和不按光线数加权的综合 RMS 由打印的逐视场值算出。

## 可比与不可比

- **一阶参数与规格判定**：对同一规格，可直接并列。
- **误差函数（`ERR. F.`）与 WAV 加权 RMS／Strehl**：只在“设置相同”的方案之间可比——视场角、视场权重、渐晕因子、波长与波长权重、参考波长、孔径相同（误差函数另要求 `DEL`、`WTA` 相同）。渐晕不同时误差函数不可直接比较：CODE V 按渐晕因子缩放优化光线网格，光瞳采样已经不同（WAV 不缩放网格，渐晕因子只经默认孔径影响哪些光线通过）。不满足时汇总把方案分组并给出说明，只有组内可比。
- **WAV 的接受光瞳**：WAV 网格固定，光阑面和默认孔径挡掉一部分光线，被挡的光线不是失败、也不进 RMS，综合 RMS 又按各视场光线数加权。汇总因此另列“WAV 接受光瞳比”（各视场光线数除以视场 1 的光线数）、CODE V 的综合 RMS 与按视场权重直接平均的综合 RMS；任一视场的接受光瞳比与同组第一个方案相差超过 3 个百分点，就把方案分到不同的光瞳组，说明里写明光线数少的方案 RMS 偏小、只有组内直接可比。推荐仍按原规则选出，但被选方案的光瞳与其他可比方案不同时，推荐下方附“注意”。

## 推荐

在误差函数可比的**最大一组**里，只考虑 AUT 完成、具体约束全部满足、变量界满足、规格判定不是“未通过”的方案，按 WAV 加权 RMS 由小到大取第一名（缺失时按 `ERR. F.`）。没有符合条件的方案就不推荐，并写明原因；最大的组不唯一（例如两个方案渐晕不同、各成一组）时也不推荐，因为按名字挑一组没有依据。推荐附带接受命令，但不执行；不在最大组内的方案会被列为“不参与比较”，而不是悄悄比较。

## 使用限制

只按 WAV 加权 RMS 推荐一个方案，不做多目标权衡；方案之间不共享 AUT 状态。评价耗时随采样与镜头变化。推荐不等于候选已经被接受或设计已经满足全部制造要求。
