# 设计记录：演示

> 由 `codev_mcp.walkthrough` 根据 5 个运行目录的执行记录生成（2026-09-28T00:00:00+00:00）。数值均取自运行记录，保留单位、精度说明与来源；未记录的项写为“缺失”，不重算、不估算。设计思路等叙述请在各节之后补充，引用的数值应出自这些记录。

## 1. 设计目标与指标

规格：dbgauss-demonstration（单位 mm）；**阈值为演示值，不是设计要求**

- 孔径：EPD 50（来源：hand-authored demonstration condition）
- 视场：0°（权重 1）、10°（权重 1）、14°（权重 1）（来源：hand-authored demonstration condition）
- 波长：656.3 nm、587.6 nm、486.1 nm；参考 2（来源：hand-authored demonstration condition）
- 玻璃目录：SCHOTT、OHARA（来源：demonstration value）

| 编号 | 指标 | 条件 | 下限 | 上限 | 必需 | 来源 |
| --- | --- | --- | ---: | ---: | --- | --- |
| efl | effective_focal_length（mm） | — | 99.5 | 100.5 | 是 | demonstration value |
| f-number | f_number（ratio） | — | 1.95 | 2.05 | 是 | demonstration value |
| oal | overall_length（mm） | — | — | 80 | 是 | demonstration value |
| bfl | back_focal_length（mm） | — | 60 | — | 是 | demonstration value |
| glass-center-min | center_thickness_min（mm） | — | 2 | — | 是 | demonstration value |
| glass-center-max | center_thickness_max（mm） | — | — | 15 | 是 | demonstration value |
| glass-edge-min | edge_thickness_min（mm） | — | 1 | — | 是 | demonstration value |
| air-center-min | air_center_thickness_min（mm） | — | 0.1 | — | 是 | demonstration value |
| air-edge-min | air_edge_thickness_min（mm） | — | 0.5 | — | 是 | demonstration value |
| distortion | distortion_max_abs（percent） | — | — | 1 | 是 | demonstration value |
| mtf-axis-10 | mtf（ratio） | field 1；direction tangential；frequency 10 | 0.25 | — | 是 | demonstration value |
| spot-axis-rms | spot_rms_radius（mm） | field 1 | — | 0.05 | 是 | demonstration value |
| mtf-edge-50 | mtf（ratio） | field 3；direction sagittal；frequency 50 | 待补 | 待补 | 是 | 待补: shows how an unknown threshold keeps the overall verdict unknown |

- 判据预设：marechal，非必需
- 判据预设：airy_spot（rms），非必需

## 2. 素材与准备

- 起点镜头：`D:/lenses/start.len`，SHA-256 `aaaaaaaaaaaa`

| 步骤 | 产物 | 路径 | SHA-256 |
| ---: | --- | --- | --- |
| 1 | 起点缩放 | `D:/lenses/scaled.len` | `bbbbbbbbbbbb` |
| 3 | 类型化修改（换玻璃等） | `D:/设计/换玻璃.len` | `dddddddddddd` |

运行目录（均保留完整清单、原始输出和日志）：

- 步骤 1：`<ROOT>/scale`
- 步骤 2：`<ROOT>/glass`
- 步骤 3：`<ROOT>/edit`
- 步骤 4：`<ROOT>/aut`
- 步骤 5：`<ROOT>/evaluate`

## 3. 操作步骤

### 3.1 起点缩放（`codev_mcp.scale`，成功）

- 目标 EFL 50，系数 0.5；缩放后 EFL 50.0000000001，相对偏差 2e-12
- 求解联动：S11 PIM 像距 63.1 → 31.6

| 一阶参数 | 之前 | 之后 |
| --- | ---: | ---: |
| effective_focal_length | 100 | 50 |

可在 CODE V 命令行复现的命令（one update_lens transaction）：

```
RES D:/lenses/start.len
RDY S1 28.7
EPD 25
SAV D:/lenses/scaled.len
```

### 3.2 相近玻璃候选（`codev_mcp.glass_near`，成功）

- 参考：REF_SCHOTT（nd 1.6，νd 38）；目录：CDGM

| 目录 | 玻璃 | 代码 | nd | νd（计算） | Δnd | Δνd | 距离 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| CDGM | CAND | 603380 | 1.60342 | 38 | -0.002 | 0.1 | 0.2 |

距离定义：sqrt((dnd/0.01)^2 + (dvd/1)^2)

可在 CODE V 命令行复现的命令：

```
GLD;GLI CDGM
```

### 3.3 类型化修改（换玻璃等）（`codev_mcp.edit`，成功）

- 修改：swap；理由：nearest
- 事务说明：surface 11 thickness re-derived

可在 CODE V 命令行复现的命令：

```
RES D:/lenses/scaled.len
GLA S4 CAND_CDGM
SAV D:/设计/换玻璃.len
```

### 3.4 受控 AUT 候选（`codev_mcp.aut prepare`，失败）

- 失败原因：ValueError: stage 2 (poly) failed
- 目标达成（末阶段 ERR. F. ≤ TAR）：缺失；全部约束满足：缺失；变量界：缺失

#### 阶段 1：mono（成功）

- 结束原因：Maximum cycle limit reached；ERR. F. 10 → 5（第 3 循环）
- 权重修改：波长 1 → 0

| 变量 | 之前 | 之后 | 下限 | 上限 | 界内 |
| --- | ---: | ---: | ---: | ---: | --- |
| S1 radius | 28.7 | 28.1 | — | — | 是 |

| 约束 | 关系 | 目标 | 达成值（打印） | 差值 | 判定 |
| --- | --- | ---: | ---: | ---: | --- |
| EFL | = | 50 | 4.99990E+01 | -1.000E-04 | 未满足 |

- 通用约束 MNE（服务复核 edge_thickness_min）：0.4 mm，未通过
- CODE V 冻结厚度警告：Mn ET S10
- 求解联动：S11 thickness（PIM） 31.6 → 30.9

本阶段实际发送的命令（`out <文件>` 只重定向输出，复现时可省略）：

```
frz s0..i
ccy s1 0
go
```

#### 阶段 2：poly（失败）

- 失败原因：ValueError: no completion line

本阶段实际发送的命令（`out <文件>` 只重定向输出，复现时可省略）：

```
frz s0..i
```

### 3.5 评价出图与规格判定（`codev_mcp.compare`，成功）

- 规格判定总体：lens 未知
- 报告：`<ROOT>/evaluate/report.md`

可在 CODE V 命令行复现的命令（native plots）：

```
spo;go
```

## 4. 出图清单

| 镜头 | 分析 | 来源 | 图像 | 对应数据 |
| --- | --- | --- | --- | --- |
| lens | native-spot | CODE V 原生 | `<ROOT>/evaluate/lens/native-spot/image.png` | `lens/native-spot/native.PLT` |

原生图由 CODE V 绘制，没有对应的结构化数值；服务重绘图由同目录快照中的数值生成。图像保留在运行目录，不复制、不改名。

## 5. 实测数据

### 一阶参数

| 量 | lens |
| --- | ---: |
| effective_focal_length | 50 |
| f_number | 2 |
| back_focal_length | 缺失 |
| overall_length | 37.5 |
| image_distance | 缺失 |

精度：string items

### 规格判定

总体：lens 未知

| 条目 | 必需 | lens 值 | lens 判定 |
| --- | --- | ---: | --- |
| efl | 是 | 50 mm | 通过 |
| mtf-edge-50 | 是 | 缺失 | 未知（threshold pending (待补)） |

### 各阶段误差函数与约束达成（见 3 节对应步骤）

| 阶段 | 结束原因 | 初始 ERR. F. | 最终 ERR. F. | 约束全部满足 |
| --- | --- | ---: | ---: | --- |
| mono | Maximum cycle limit reached | 10 | 5 | 否 |
| poly | 缺失 | 缺失 | 缺失 | 缺失 |

## 6. 注意事项

- CODE V `OAL` 从第 1 面量到最后一个镜面，不含像距；后截距取列表打印的 BFL，AUT 中的 `IMD` 约束则是含离焦的像距。
- 点列、MTF 与 WAV 都在镜头当前保存的像面上计算，服务不自动改焦；像距由 `PIM` 求解时即近轴像面，不是最佳焦面，Maréchal 等衍射判据应结合这一点阅读。
- 部分路径含非 ASCII 字符：各 CLI 先把输入复制到服务自有 ASCII 目录再交给 CODE V，输出由 Python 写回原路径。
- 步骤 3 更换的玻璃：S4 → CAND_CDGM；换玻璃后需重新优化，结果见后续 AUT 步骤。
- 步骤 4（受控 AUT 候选）状态为 失败：ValueError: stage 2 (poly) failed。
- 步骤 4 的 AUT 阶段 mono 约束未全部满足：EFL = 50（候选仍可接受，但不代表达标）。
- 步骤 4 的 AUT 阶段 mono 有 CODE V 冻结厚度警告：Mn ET S10。
- 步骤 5 的输入 `D:/lenses/other.len` 不是起点或前序步骤的输出（哈希不连续），请核对来源。
- 规格中有待补阈值，对应条目判定为未知，总体不会判为通过。
- 所用规格标记为演示：阈值是演示值，不是设计要求。
