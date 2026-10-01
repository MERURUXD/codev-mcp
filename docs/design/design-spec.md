# 设计规格格式（schema 1）

设计规格是用户提供的 JSON 文件，描述一次设计要满足的系统定义、评价配置、判据预设与逐项要求。它驱动[评价出图包](../comparison-workflow.md#设计规格评价包)的分析选择和[规格判定](#规格判定)。代码中没有课程或项目默认值：阈值、玻璃目录和频率都来自规格文件，内置的只有注明出处、需显式选用的判据预设。

示例：[`design-spec-dbgauss.json`](design-spec-dbgauss.json)，针对模拟后端的三视场测试模型（保留旧示例文件名）。**示例中的系统定义为手写演示条件，所有阈值都是演示值，不是设计要求。** 实际任务的阈值由用户填写；不知道的项写成待补，不要猜测。

## 顶层字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema_version` | 是 | 固定为 `1` |
| `kind` | 是 | 固定为 `"design_spec"` |
| `name` | 是 | 非空名称 |
| `description` | 否 | 说明文字 |
| `demonstration` | 否 | `true` 表示阈值是演示值；判定结果会原样携带这个标记 |
| `units` | 是 | `mm`、`cm` 或 `inch`；必须与被评价镜头一致，长度要求的单位也必须等于它 |
| `system` | 是 | 系统定义，见下节；可为空对象 |
| `evaluation` | 是 | 评价配置：`analyses`、`mtf_frequencies`、`spot_grid` |
| `criteria` | 是 | 显式选用的判据预设列表，可为空 |
| `requirements` | 是 | 逐项要求列表，可为空 |

未知字段一律拒绝，避免拼写错误被静默忽略。任何一项都可以带 `source` 文字（任务原文、页码、“待补”等），判定结果原样保留。

## 系统定义 `system`

四项均可省略或为 `null`（不检查）。写成 `{"pending": true, "source": "待补"}` 时判定为 unknown；待补项不能同时给出数值。

| 项 | 数值字段 | 判定方式 |
| --- | --- | --- |
| `aperture` | `kind`（`epd`/`fno`/`na`/`nao`）、`value` | 只在镜头孔径类型相同时比较定义值；类型不同返回 unknown，应改用 `f_number` 等要求给出容差 |
| `fields` | `values`: `[{x_angle?, y_angle, weight?}]`，角度单位度 | 数量、角度（容差 1e-6 度）与给出的权重逐项一致 |
| `wavelengths` | `values`: `[{nm, weight?}]`、`reference`（1 起的序号或 `null`） | 数量、波长（容差 1e-6 nm）、权重与参考波长一致 |
| `glass_catalogs` | `allowed`: 目录名列表，例如 `["SCHOTT"]` | 镜头玻璃名按 `名称_目录` 的末段判断；目录不在列表中为 fail，名称没有目录后缀为 unknown |

系统定义不一致说明被评价的不是规格描述的系统，这些条目都按必需项计入总体判定。

## 评价配置 `evaluation`

- `analyses`：`first_order`、`spot_diagram`、`mtf`、`wavefront`、`native_plot` 的非空子集。规格评价总是使用镜头全部视场与波长，并限单变焦。
- `mtf_frequencies`：升序、唯一、非负的 cycles/mm 频率，最多 101 个，用于结构化衍射 MTF。原生 MTF 图仍固定为 `MFR 100; IFR 10`。
- `spot_grid`：点列绘图网格 2～101。

要求引用的分析必须在 `analyses` 中：MTF 要求需要 `mtf` 且频率在列表内；点列、WAV 要求需要对应分析；畸变要求需要 `native_plot`（从 `field_aberration` 原生图的 FIE 文本读取）；厚度要求只读镜头数据，不需要分析。

## 判据预设 `criteria`

预设只在列出时生效，展开为每个镜头视场一条判定：

| 预设 | 选项 | 内容与出处 |
| --- | --- | --- |
| `marechal` | `required` | Maréchal 判据：WAV RMS 波前差 ≤ 0.07 waves，且 CODE V Strehl ≥ 0.8。λ/14 ≈ 0.0714 λ 对应 Strehl ≈ 0.8（Born & Wolf《Principles of Optics》9.3 节），预设采用常用的略严的 0.07 |
| `airy_spot` | `statistic`（`rms` 或 `geometric_max`）、`required` | 点列**直径**（2 × 原生 SPO 半径）≤ 艾里斑直径 2.44 λ F/#（Born & Wolf 8.5.2）。λ 为镜头参考波长，F/# 为 CODE V 一阶 F/#；艾里斑是服务计算值。仅支持无限远物距 |

## 逐项要求 `requirements`

每项要求必须写全以下九个键：

```json
{"id": "efl", "metric": "effective_focal_length", "unit": "mm",
 "field": null, "direction": null, "frequency": null,
 "minimum": 99.5, "maximum": 100.5, "required": true,
 "source": "任务原文第 2 页"}
```

规格中另外允许 `source`、`note` 与 `pending`。`pending: true` 的项两个界限都必须为 `null`，判定为 unknown（“待补”）。ID 必须唯一，`system.` 与 `preset.` 前缀保留给系统条件与预设。

| metric | 单位 | 条件 | 数值来源 |
| --- | --- | --- | --- |
| `effective_focal_length`、`overall_length`、`back_focal_length` | 规格长度单位 | 无 | 一阶分析；`OAL` 是第 1 面到最后一个镜面、**不含像距**；后截距只取列表打印的 BFL |
| `f_number` | `ratio` | 无 | 一阶分析 |
| `mtf` | `ratio` | `field`、`direction`（`tangential`/`sagittal`）、`frequency` | 结构化衍射 MTF |
| `spot_rms_radius`、`spot_max_radius` | 长度单位 | `field` | 原生 SPO 统计，半径 |
| `wavefront_rms_waves`、`wavefront_strehl` | `waves`、`ratio` | `field` | 当前焦面 WAV |
| `distortion_max_abs` | `percent` | 无 | FIE 参考波长畸变表 11 个采样点中的最大绝对值 |
| `center_thickness_min`、`center_thickness_max` | 长度单位 | 无 | 玻璃中心厚度（镜头回读） |
| `edge_thickness_min` | 长度单位 | 无 | 玻璃边缘厚度，服务计算 |
| `air_center_thickness_min`、`air_edge_thickness_min` | 长度单位 | 无 | 空气间隔中心／边缘厚度，边缘为服务计算 |

## 规格判定

每项结果为 `pass`、`fail` 或 `unknown`，并带数值、单位、精度说明、`value_source`（`codev` 或 `service_calculated`）和 `threshold_source`（`spec`、`preset:marechal` 或服务计算的艾里斑）。总体判定：任一必需项 fail 为 fail；存在必需项且全部 pass 才为 pass；其余为 unknown。缺数据、模拟来源、待补阈值都不算通过。

- **打印精度**：后截距、点列、WAV 与畸变取自打印文本，按保留小数位给保守的半末位区间；阈值落在区间内为 unknown。点列按打印的直径推算区间。
- **机器精度**：一阶的 `EFL`、F/#、`OAL` 和厚度经 `EvaluateExpression` 读取，约 16 位有效数字，CODE V 内部运算还带入 1e-15 量级的舍入（厚度被优化推到下界 0.1 时读回 0.09999999999999896）。判定时阈值按 1e-14 × max(|值|, 1)（镜头单位）放宽，只差在这一舍入内的按“满足”判为 pass，并在原因里注明；结果里的 `tolerance` 给出用的宽度，真正越界仍是 fail。打印量（后截距、点列、WAV、畸变）不加这一宽度，仍只用打印精度区间。
- **畸变**：解析 `FIE;LSA;GO` 的默认旋转对称表（参考波长、相对视场 0.0～1.0 共 11 点、角度为度）。只接受 Y 向角视场；FIE 满视场角与镜头最大视场不符、波长与参考波长不符、表格缺失或格式不同（例如色差分表、全视场显示）都返回 unknown。CODE V 以理想（傍轴）像高为基准计算畸变，只在采样点上取最大值。
- **厚度**：玻璃段从每个后接玻璃的面到下一面（胶合双片为两段）；空气段在相邻“接触玻璃”的面之间，跳过两侧都是空气的虚设面（例如单独的光阑面）。不含物距和像距。边缘高度取两端面 `GetMaxAperture` 有效最大半孔径中的较大者，按球面矢高计算；这与 CODE V AUT 中 MNE/MAE 以光线定义的边缘（第一面最高光线投影到光轴的长度）不同，数值可能不一致。边缘厚度只在原生 `LIS` 面段确认没有特殊面数据时计算；含反射面、多变焦、缺半孔径，或边缘高度超过某一面球面半径（实际零件需倒边／平台）时为 unknown，原因中写明面号和数值。某个元件的边缘厚度无法计算时，不能用其余元件的最小值代替。
- **结果字段**：厚度与畸变项在 `details` 中列出逐段或逐点数据，便于找出起约束作用的元件。

## 输入校验

规格读取在启动 CODE V 前完成；镜头单位与规格不符、要求引用的视场不存在或镜头为多变焦时，在预检后、分析前拒绝。
