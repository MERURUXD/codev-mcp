# 从空目录开始的模拟流程

下载或克隆本仓库，在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps .
.\.venv\Scripts\python.exe examples/simulated_quickstart.py --output .codev-run/demo
```

输入是 [自建单透镜规格](create-singlet.json)：±45 mm 球面、4 mm BK7、EPD 8 mm、650/550/450 nm、0/3/6°、PIM。
示例经真实 stdio MCP 协议列出十一工具、创建模拟镜头、执行一阶分析、另存并关闭。输出目录必须不存在；结果写入 `result.json` 与 `simulated.len`，协议记录在 `protocol/`，内部运行文件在 `work/`。

`simulated.len` 是模拟格式，不能当作真实 CODE V 镜头打开；模拟数值不证明真实光学效果。原生解析测试数据的输入与来源见 [夹具来源](../tests/data/project-owned/README.md)。
真实后端配置见 [安装说明](../docs/install.md)，镜头路径由用户自行提供，服务会打开工作副本。
