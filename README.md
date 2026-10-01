# CODE V MCP

[English introduction](README.en.md) · Windows / CODE V 10.2 · 实验性 0.1.0

通过 Windows COM 将 CODE V 10.2 接入本机 MCP 客户端，读取镜头、修改现有参数、运行基础光学分析，并导出 CODE V 原生绘图。

服务使用独立工作进程管理自己的 CODE V 会话，采用 stdio 通信。打开镜头时创建工作副本，批量修改经过回读核对与检查点保存；另存不会覆盖源文件或已有目标文件。

## 支持什么

- 读取和编辑镜头参数，运行一阶、点列、MTF、WAV 分析，导出原生绘图。
- 通过独立命令行完成评价、对比、缩放、玻璃查询、固定网格扫描和受控 AUT。
- 新建简单球面镜头，按类型化输入导入、导出与整理设计资料。

具体工具、参数边界以[支持能力与限制](docs/capabilities.md)为准。结构编辑仅开放给本服务新建的简单球面镜头；AUT 候选须另行显式接受。不支持任意命令或宏、任意现有镜头增删面、远程服务或控制用户已打开的 CODE V 窗口。

## 快速开始

真实后端需要 Windows、已安装且可使用许可证的 CODE V 10.2，以及注册的 `CodeV.Command.102`。Python 要求 3.10+，推荐 64 位解释器。模拟后端不需要 CODE V，其结果标记为 `simulated`，不用于光学结论。

下载或克隆仓库后，在仓库根目录执行 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m codev_mcp --backend simulated
```

最后一条命令启动 stdio 服务并等待 MCP 客户端输入；没有交互提示是正常现象。连接真实 CODE V 时改用 `--backend com`，客户端配置见[安装说明](docs/install.md)。

## 使用与开发

| 要做什么 | 从哪里开始 |
| --- | --- |
| 安装并连接 MCP 客户端 | [安装与故障排查](docs/install.md) |
| 确认工具支持范围、光学口径与使用限制 | [支持能力与限制](docs/capabilities.md) |
| 评价一个镜头或比较初始／最终镜头 | [镜头对比与评价包](docs/comparison-workflow.md) |
| 生成受控优化候选并显式接受 | [受控 AUT](docs/controlled-aut.md) |
| 查找其他 CLI、规格示例与模拟入门 | [文档索引](docs/README.md) |
| 修改代码、运行测试或维护文档 | [开发与验证](CONTRIBUTING.md) |



## 可复现示例

安装后执行 `python examples/simulated_quickstart.py --output .codev-run/demo`。它从空会话创建模拟球面镜头，经 MCP 获取一阶结果并另存，输出与限制见[模拟入门](examples/README.md)。

## 发布与许可

本项目采用 [MIT](LICENSE) 许可证，版权人 MERURUXD，2026。CODE V 是外部商业软件；本项目许可不覆盖 CODE V 软件、许可证、厂商手册与示例库，它们不随项目分发。用户镜头由用户自行提供。
