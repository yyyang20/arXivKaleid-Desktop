# Desktop 文档入口

当前版本为 `0.1.0-alpha.4`，提供最新候选抓取、真实运行进度、两轮分析、GUI Markdown 日报和两层错误诊断。GUI 面向用户显示稳定代码、影响与建议；本地脱敏 JSONL 面向开发排查。支持源码运行和 Windows 10/11 x64 portable ZIP。

每次启动后首次开始分析前必须显示模型数据发送、费用、DPAPI 凭据与本地运行数据告知；用户拒绝时不消费快照、不调用模型。

| 文档 | 职责 |
|---|---|
| [Desktop 规范](DESKTOP_SPEC.md) | GUI、DPAPI、候选快照、一次性分析与日报行为 |
| [Desktop 运行手册](DESKTOP_OPERATIONS.md) | 依赖、启动、运行目录与离线验证 |
| [Desktop 结构](DESKTOP_STRUCTURE.md) | 当前文件及运行数据职责 |

根 [项目文档索引](../README.md) 和 [AGENTS.md](../../AGENTS.md) 继续约束本入口。核心筛选规则唯一来源为 [PROJECT_SPEC.md](../PROJECT_SPEC.md)；本目录说明 GUI、凭据、候选快照和运行行为，不复制两轮筛选、PDF 或标签规范。

变更统一记录在 [CHANGELOG.md](../CHANGELOG.md)，不另建 Desktop 变更记录。
