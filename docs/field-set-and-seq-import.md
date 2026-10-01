# 视场集合、按像高归一化与 `.seq` 导入

本页说明 `update_lens` 的整体视场集合替换、`create_lens` 的 `PIM` 像面求解和 `.seq` 白名单导入。公共工具仍是十一个；所有输入经类型化校验，不执行任意命令、宏或 `IN`。

## 1. 替换视场集合：`update_lens` 的 `field_set`

```json
{"field_set": {"fields": [
  {"y_angle": 0}, {"y_angle": 5}, {"y_angle": 10}, {"y_angle": 14},
  {"y_angle": 18, "weight": 0.5, "vuy": 0.5}
]}}
```

- 一次请求要么带 `edits`，要么带 `field_set`，不能同批（模型校验直接拒绝）。`field_set` 单独成一次事务：恢复点 → 修改 → 回读 → 与修改前的整镜头快照逐项比对（视场之外任何东西移动即失败）→ 检查点，失败整体回滚并核对。
- 每个视场：`y_angle`（必填）、`x_angle`（默认 0），可选 `weight` 与渐晕因子 `vux/vlx/vuy/vly`（-0.99～0.99）。1～10 个视场，角度限 ±89°，权重 0～1e6。
- **省略的权重与渐晕因子沿用同编号视场的值，新增视场权重为 1、渐晕为 0；比原来短时丢弃多余的视场。** 这是 CODE V 的原生行为：`XAN`／`YAN` 的取值个数决定视场个数，权重和渐晕按视场序号保留，缩短后再加长不会“找回”旧值。注意“同编号”不是“同角度”：把 0／10／14 改成 0／5／10／14／18，第 2 个视场保留原来 10° 视场的渐晕因子；要改就显式给出。
- 服务发送的命令固定：`XAN` 与 `YAN`（取值个数 = 新视场数，设置个数与角度），然后只对与 CODE V 已保留的值不同的权重和因子发送逐视场命令（`WTF Fn`、`VUY Fn` 等，与单项编辑相同）。结果的 `warnings` 最后一条列出实际发送的命令。
- 范围：单变焦、角度定义的视场（`XAN/YAN`）；多变焦、按物高／像高定义的视场返回 `field_set.applied=false` 与原因，不发送命令。任意已打开的单变焦镜头都可用，不限于服务新建的镜头。像面求解（`PIM`）、孔径和其它数据不变；检查点格式仍为 6。
- 结果 `UpdateResult.field_set`：`applied`、`previous_fields`、读回的 `fields`、`rejected_reason`；拒绝时 `rolled_back=true`。
- 独立 CLI `python -m codev_mcp.edit` 的编辑文件可以改写成 `{"schema_version":1,"kind":"lens_edits","name":"...","field_set":{...}}`（与 `edits` 二选一），执行记录里给出等效命令。

## 2. 按像高归一化的视场角（不连接 CODE V）

任务写“归一化视场 0.5／0.7／0.85”时有两种读法：CODE V 的相对视场按**像高**（无畸变时正比于 tan 角），而线性取角度会差近半度（半视场 23.5° 时 0.5 处相差约 0.5°）。

```powershell
.\.venv\Scripts\python.exe -m codev_mcp.fieldset --max-angle 23.5 --relative 0 0.5 0.7 0.85 1 --output fields.json
```

- 默认 `--mode height`：角度 = atan(相对视场 × tan 最大角)；`--mode angle` 为线性。输出 JSON 给出口径说明、逐视场角度、线性读法的对照值、结果反算的相对像高，以及可直接作为 `update_lens` 请求的 `field_set`（`--output` 写入新文件，拒绝覆盖；`--weights` 可一并给权重）。
- 只做计算，不改变 CODE V 的视场类型，不新增 MCP 工具。

## 3. `create_lens` 的 `image_solve: "pim"`

新建镜头时给最后一个普通面的厚度加 CODE V 近轴像面求解（发送 `pim yes`）。请求里的厚度只是初值，求解后由 CODE V 推导；此后该厚度按“求解控制的参数”拒绝编辑，改半径等会让像距随焦面移动并在 `warnings` 中列出。快照按 `SOLVES` 与变量控制列核对，求解未生效则新建失败。带求解的镜头不属于“简单球面模型”，`edit_lens_structure` 拒绝（与既有求解镜头一致）。

## 4. `.seq` 导入：`python -m codev_mcp.seq_import`

```powershell
.\.venv\Scripts\python.exe -m codev_mcp.seq_import --seq lens.seq --output lens.len --reference-lens ref.len
.\.venv\Scripts\python.exe -m codev_mcp.seq_import --seq lens.seq --dry-run   # 只解析，列出将发送的命令
```

序列文件从不被执行。服务逐行按白名单解析成类型化请求，再由服务自己构造命令：`create_lens`（含 `PIM`）→ 必要时 `update_lens` 的 `field_set`（权重、渐晕）→ 必要时波长参考／权重编辑 → `save_lens_as` → 独立会话重开复核。

**接受的命令**：`RDM`、`LEN "版本串"`、`EPD/FNO/NA/NAO`、`DIM M|C|I`、`WL`（严格降序）、`REF`、`WTW`（非负整数）、`XAN/YAN/WTF/VUX/VLX/VUY/VLY`、`SO`（无穷物距）、`S 半径 厚度 [玻璃_目录]`（半径 0 为平面）、其后的 `STO`、`PIM`（只在最后一个普通面）、`SI 0 0`、`GO`；`!` 注释、空行、行尾 `&` 续行、同一行用 `;` 分隔的多条命令（引号内的 `;` 是文本）。

**列为“未导入”而不拒绝**：`TITLE`、`INI`、`UID`、`DOR`（CODE V 2025 的未知系统项）、`LEN` 的版本串、`DER`（AUT 留下的导数增量），以及变量标记 `CCY/THC/GLC`（AUT 变量只由类型化 AUT 规格开放）。清单写入 `manifest.json` 的 `not_imported`。

**整份拒绝并逐行列出**：其它一切命令（`IN`、`CIR` 等孔径、非球面、其它求解／拾取、变焦、`AUT`……）、有限物距、非零像面厚度或半径、玻璃缺目录后缀、视场个数不一致、缺 `STO`／`SI`／`SO` 等。任何拒绝都不写输出、不连接 CODE V。文件大小限 1 MB、5000 行，不接受 NUL。

**核对**：读回值与序列逐项比对（单位、孔径、波长、权重、参考波长、视场角／权重／渐晕、每个面的半径／厚度／玻璃、光阑、`PIM` 求解），`LIS` 的求解区与规格区再比对一次；`PIM` 厚度被 CODE V 重新求出且与序列起始值相差超过 1e-6 时给出警告（不是失败：CODE V 读这份序列同样会重解）。服务发送 12 位有效数字，序列是 16 位；读回偏差记录在 `max_relative_deviation`。`--reference-lens` 时导入结果与该 `.len` 逐项比对，有差异即失败。执行记录列出服务实际发送的等效命令，并注明序列文本本身从未发给 CODE V。

限制：不导入孔径、偏心、非球面、涂层、多变焦、变量标记；序列里的 `INI`／标题不带入新镜头。`.seq` 由 CODE V 的 `WRL` 导出时（`python -m codev_mcp.export_seq`），带遮拦或显式通光孔径的镜头（如 cooke1）会被拒绝。
