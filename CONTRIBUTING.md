# 开发与测试

先读 [README](README.md)、[能力边界](docs/capabilities.md)，检查 `git status --short`，保留已有修改。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
git diff --check
```

自动化测试使用模拟后端、假 COM、项目自建夹具和临时目录，不需要 CODE V 或许可证。

COM 对象只由独立工作进程持有，同一个会话串行调用；stdout 只承载协议。公共契约修改同步检查模型、后端、工作进程、服务与测试。保留输入校验、工作副本、回读、检查点、回滚与会话失效守卫。先跑受影响用例，涉及协议／生命周期时跑全套。

提交应聚焦单一问题，说明变更、验证及未验证范围。不要提交镜头、运行产物、本机配置、商业软件或厂商文档／样本。测试数据应为自建或明确标注的合成数据。保持 unittest、类型注解和现有依赖，不做无关格式化。
