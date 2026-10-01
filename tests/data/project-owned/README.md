# 项目自建测试数据

`lis.txt`、`fie.txt`、`spot.txt`、`wavefront.txt`：由 CODE V 10.2 SR1 生成的原生解析夹具；输入为本项目自建 BK7 单透镜，半径 ±45 mm、厚度 4 mm、EPD 8 mm、650/550/450 nm、0/3/6°、PIM。命令为受限 `create_lens`、SPO、`wav;nom yes;bes no;nrd 20;go`、FIE 原生绘图入口。没有输入厂商镜头。

`compound-lis.txt` 和三份 `.seq` 是手写合成语法夹具，保留多面、平面、求解、渐晕、元数据、白名单与整份拒绝路径；不代表真实光学性能。`walkthrough-snapshot.md` 是合成运行记录的渲染快照。
