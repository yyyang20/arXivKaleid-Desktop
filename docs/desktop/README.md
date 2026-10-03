# Desktop 文档入口

当前版本为 `0.1.0-alpha.7.1`，修复 Round 1 非空摘要证据校验时缺少标准库导入导致的异常。使用基础版 PySide6-Fluent-Widgets，提供首页、历史占位和设置页及正式 A0 应用图标。首页保留候选抓取、两轮分析独立入口、可收缩的真实运行进度、Markdown 日报和安全诊断；Key 与 About 位于设置页，核心业务行为不变。最新公开成品为 [alpha 7 Windows x64 portable](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.7)，运行与验证方式见运行手册。

每次启动后首次开始分析前必须显示模型数据发送、费用、DPAPI 凭据与本地运行数据告知；用户拒绝时不消费快照、不调用模型。

| 文档 | 职责 |
|---|---|
| [Desktop 规范](DESKTOP_SPEC.md) | GUI、DPAPI、候选快照、一次性分析与日报行为 |
| [Desktop 运行手册](DESKTOP_OPERATIONS.md) | 源码启动、依赖、本机构建、portable 验证与运行排错 |
| [Desktop 结构](DESKTOP_STRUCTURE.md) | Desktop 包内文件及 runtime 数据职责 |

根 [项目文档索引](../README.md) 和 [AGENTS.md](../../AGENTS.md) 继续约束本入口。核心筛选规则唯一来源为 [PROJECT_SPEC.md](../PROJECT_SPEC.md)；本目录说明 GUI、凭据、候选快照和运行行为，不复制两轮筛选、PDF 或标签规范。

开发与公开发布流程见根 [运行手册](../OPERATIONS.md)，整体目录与核心模块职责见根 [项目结构](../PROJECT_STRUCTURE.md)；本目录链接引用这些规则，不另建完成或发布义务。

变更统一记录在 [CHANGELOG.md](../CHANGELOG.md)，不另建 Desktop 变更记录。
