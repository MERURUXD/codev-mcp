# 受控 AUT 候选与显式接受

入口为独立命令行 `python -m codev_mcp.aut`。它在新的运行目录内建立自己的已提交镜头检查点，不接管已打开的 MCP 镜头或用户 CODE V GUI。`prepare` 只生成候选；查看 `result.json` 后，由调用方另外执行 `accept` 才能把该候选发布为该运行目录的 revision 1。没有自动接受选项。

请求使用类型化的 **AUT 规格**：多个按顺序执行的阶段，每个阶段有自己的变量、白名单约束、通用厚度约束、误差函数参数和类型化权重修改。单变量参数也可用，内部转换为一阶段规格。

## 使用

```powershell
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
.\.venv\Scripts\python.exe -m codev_mcp.aut prepare --lens <工作目录>\start.len --output-dir <工作目录>\new-aut-run --spec docs\design\aut-spec-dbgauss-stages.json
.\.venv\Scripts\python.exe -m codev_mcp.aut accept <工作目录>\new-aut-run\result.json
```

单变量用法（1～8 循环、5～300 秒）不变：`--surface 1 --parameter radius --lower 25 --upper 80 --target 0 --cycles 1 --wall-seconds 90`，与 `--spec` 互斥。示例规格：[`aut-spec-dbgauss.json`](design/aut-spec-dbgauss.json)（一阶段 12 个变量，EFL／OAL／IMD 与 MNT／MNE）、[`aut-spec-dbgauss-stages.json`](design/aut-spec-dbgauss-stages.json)（单色 → 加谱线 → 抛光）。数值均为演示值。

## 规格（schema 1）

```json
{"schema_version": 1, "kind": "aut_spec", "name": "...", "wall_seconds": 600,
 "stages": [{"name": "mono",
   "lens_changes": [{"target": "wavelength", "wavelength": 1, "parameter": "weight", "value": 0}],
   "variables": [{"surface": 1, "parameter": "radius"},
                 {"surface": 2, "parameter": "thickness", "lower": 0.1, "upper": 5}],
   "constraints": [{"operand": "EFL", "relation": "=", "value": 100, "tolerance": 0.001},
                   {"operand": "DIY", "field": 3, "relation": "<", "value": 0.005}],
   "general_constraints": {"MNT": 2, "MNE": 1},
   "error_function": {"MXC": 30, "MNC": 1, "TAR": 0, "IMP": 0.05},
   "set_vignetting": false}]}
```

- `wall_seconds`：整次候选生成的父进程墙钟上限，5～1800，默认 600。每个阶段 `TIM` 取其分钟数作辅助。
- `variables`：现有普通面的 `radius` 或 `thickness`，可选上下界（`RDY/THI Sn > a < b`）。起始值可以在界上或界外，CODE V 可把变量推入界内，阶段记录的 `start_bounds` 逐项注明“在界上／界外”。起始为平面（无限半径）的面可以作为 `radius` 变量；AUT 变化的是曲率，平面起点不允许 `lower`／`upper`（曲率过零时半径符号翻转，半径界没有确定含义）。变量记录带 `before_infinite`／`after_infinite`，此时 `before`／`after` 为 `null`，回读比对允许半径变量的无穷标志变化。求解或拾取控制的参数、无穷厚度、物面／像面不能作为变量；像面离焦（`THI SI`）不开放。
- `constraints`（白名单）：`EFL`、`EFY`、`OAL`（可选 `surfaces: [i, j]`，默认 S1..I-1）、`IMD`（含离焦的像距；规格“后截距”映射到这里）、`DIY`（`field`，视场高度的**分数**）、`CT`／`ET`（`surface`）。`relation` 为 `=`、`<`、`>`；同一量的 `>` 与 `<` 合成一条双边命令。`tolerance` 可选，缺省为目标的 1e-6 相对（至少 1e-6 绝对）。
- `general_constraints`：`MXT`、`MNT`、`MNE`、`MNA`、`MAE`，只设定；CODE V 不打印其数值。
- `error_function`：`MXC`（1～500，必填）、`MNC`、`TAR`、`IMP`、`DEL`、`WTA`；误差函数固定为 `ERR CDV` 默认横向像差。
- `lens_changes`：阶段开始前的类型化视场数据修改与波长权重，作为镜头变化计入候选差异：视场 `weight`（`WTF Fn`，≥0）、`y_angle`／`x_angle`（`YAN`／`XAN Fn`，±89°）、渐晕因子 `vux/vlx/vuy/vly`（-0.99～0.99），波长 `weight`（`WTW Wn`，0～1000 整数）。
  - **视场权重示例**：在阶段内临时提高某视场权重、后面的阶段恢复——第一阶段 `{"target":"field","field":3,"parameter":"weight","value":2}`，最后一阶段再写 `"value":1`（见 [`aut-spec-dbgauss-ramp.json`](design/aut-spec-dbgauss-ramp.json)）。
  - **渐晕不是 AUT 变量**：要逐步改渐晕，使用 `lens_changes` 的 `vuy` 等类型化修改，或阶段后显式 `set_vignetting`。`FAP` 会重写渐晕并允许光瞳尺度变化，本服务未开放。
- `set_vignetting`：阶段 AUT 之后执行 `SET VIG`。镜头没有显式通光孔径时拒绝：CODE V 会假定光阑孔径并替换设计渐晕。

### 视场爬升 `field_ramp`

大视场镜头不宜一步到位：从较小的视场开始，逐步增大视场角，每步都从上一步的候选继续。规格里的 `field_ramp` 在读入时展开成普通阶段：

```json
{"field_ramp": {"name": "grow",
  "steps": [{"fields": [{"field": 2, "y_angle": 6}, {"field": 3, "y_angle": 9, "weight": 2}]},
            {"fields": [{"field": 2, "y_angle": 10}, {"field": 3, "y_angle": 14, "weight": 1}]}],
  "stage": {"variables": [...], "constraints": [...], "error_function": {"MXC": 15}}}}
```

- 每一步生成阶段 `<name>-<k>`：先发送该步的视场修改（`y_angle`、`x_angle`、`weight`、渐晕因子，最多 12 步、每步 10 个视场，同一步不能重复视场），再是模板 `stage` 自己的 `lens_changes`，然后按普通阶段执行。可与 `stages`（先于爬升的阶段，例如调焦）并用，`field_ramp.then` 是爬升之后运行的普通阶段（例如变量更多的精修）；阶段总数上限 20。
- 每步是一个阶段：留有自己的命令块、`ERR. F.` 变化、约束判定、候选文件 `candidate-stage-<n>.len` 与记录里的 `ramp_step`；某一步失败则停止，前面步骤的候选和 `last_good_stage` 保留。
- **中间步骤只作为运行内的链式候选，不发布**：整次运行的最后一步候选仍然只能由 `accept` 显式接受。
- 结果里 `spec` 是展开后的普通规格，`spec_input` 保留原始（含 `field_ramp`）规格；规格文件哈希对应原始字节。示例：[`aut-spec-dbgauss-ramp.json`](design/aut-spec-dbgauss-ramp.json)、[`aut-spec-wideang-ramp.json`](design/aut-spec-wideang-ramp.json)。起始的小视场镜头可用 `python -m codev_mcp.edit` 的 `field_set` 文件从样例生成（见[视场集合](field-set-and-seq-import.md)）。

规格原始字节的 SHA-256 写入结果。没有自由命令、宏、`IN` 或用户字符串进入 CODE V。

## 执行过程

基线、候选、两次 WAV 诊断、接受时两次重开验证各在可丢弃子进程中串行调用 COM；基线／诊断／验证各有 120 秒外部截止，候选受 `wall_seconds` 截止。

候选子进程从 revision 0 副本启动，逐阶段：

1. 发送类型化权重修改；`FRZ S0..I` 后逐个开放变量（`CCY`／`THC Sn 0`）。
2. 零循环 `AUT; ERR CDV; MXC 0; VLI Y; GO`：`VARIABLE LIST` 中出现的参数集合必须与请求完全一致（CODE V 自动生成的弯曲组合行可以接受）。
3. 发送误差函数参数、变量界、约束和通用约束；`OUT <服务文件>` 后异步 `GO`，完成后先取 `GetCommandOutput`，再 `OUT T`，读取重定向文件。`OUT T` 失败视为 AUT 未交回提示符：不再发送任何命令，只清理本会话记录的进程。
4. 解析文件：必须有 `Normal AUTO Completion` 行和逐循环 `ERR. F.`；具体约束从最后一个带约束表的循环读取 target／value／diff（6／6／4 位有效数字），逐条判定 `satisfied`／`violated`／`unknown`（打印精度跨越容差时）；记录活动的通用约束名称和 `Frozen Thickness Violations` 警告。
5. 只把 `FRZ` 与开放变量改变的数值控制码恢复为原值（例如 dbgauss 的 `THC S12 0`），`PIM` 等求解码不动。
6. 回读快照：只允许本阶段变量、求解控制参数（例如 `PIM` 像距）、本阶段权重修改（和 `SET VIG` 的渐晕因子）变化，其余任何变化使阶段失败。通用约束由服务按[规格判定](design/design-spec.md#规格判定)的中心／边缘厚度独立复核（覆盖所有元件，边缘定义与 CODE V 不同，已注明）。复核采用与具体约束相同的缺省容差（限值的 1e-6 相对，至少 1e-6 绝对），因为 CODE V 把厚度推到边界时回读值可能差 1e-15 量级；结果列出限值与容差。
7. 保存 `candidate-stage-<n>.len`，重新载入并核对快照，下一阶段从这个文件开始。

某一阶段失败时停止后续阶段，已保存的前序阶段候选和 `last_good_stage` 保留，结果为 `failed`，不能接受。全部阶段成功后执行前后一阶分析和独立 WAV 诊断。

## 结果与接受

`result.json`：`state`、每阶段的 `native_commands`（实际发送的命令块，供设计记录复现）、重定向输出文件与哈希、`completion`、首末 `ERR. F.`、变量前后对照与界内判定、`solve_coupled`（`PIM` 像距前后）、逐条约束、通用约束复核、权重修改、阶段候选；整体 `target_reached`（末阶段 `ERR. F.` ≤ `TAR`）、`all_constraints_satisfied`、`final_constraints_satisfied`、`explicit_bounds_satisfied`、`relations`（保留的求解／拾取及其控制的参数）、前后一阶与 WAV。**约束未满足时候选仍然保存并可接受，但结果明确标出，不代表达标。**

`accept` 再次核对源镜头哈希、候选哈希、原始 revision 和候选快照；允许的差异由规格重新推导（各阶段变量、求解控制参数、类型化权重修改），其余差异拒绝。之后复制为唯一的 revision 1、两次重开核对、持锁发布 `current.json`；结果增加 `accepted_path`。准备和接受都写入[执行记录](design/execution-record.md)（`aut`、`aut_accept` 步骤）。候选过期、修改、重复接受或发布前故障均拒绝；已经更新指针的提交不被后续诊断错误推翻。`audit.jsonl` 记录准备、失败与接受。源镜头不会被覆盖。

## 使用限制

- 非 `PIM` 求解与拾取按控制列放行并逐项列出，其与请求变量的关系必须能完整核对；不要把允许保留关系理解为支持任意求解结构。
- `SET VIG` 不保证保持原设计渐晕或光瞳；只有显式选用且满足孔径前提时才运行。候选的渐晕与光瞳变化需要单独检查。
- 多变焦、玻璃变量、非球面、像面离焦、`IMC`／`TT`／光线约束、用户自定义误差函数和 `WTC` 不支持。
- 候选超时或中断时弃用当前会话。准备、诊断和接受验证有各自的进程截止；不能把命令超时当作取消已被确认。
- 检查点格式为 6，包含原生 `CCY/THC/GLC` 控制码与视场渐晕因子；旧格式 1～5 不自动恢复。
