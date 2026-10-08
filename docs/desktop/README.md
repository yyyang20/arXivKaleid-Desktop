# Desktop 文档入口

当前版本为 `0.1.0-alpha.11`，两轮编辑窗口管理完整研究 Prompt，研究判断及排序由用户定义，第二轮不继承第一轮评价或排名。导航与现有 Fluent、灰蓝卡片、正式图标、任务进度及安全边界保持。alpha 11 当前为本地技术候选；最新公开成品仍为 [alpha 10 Windows x64 portable](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.10)，运行与验证方式见运行手册。

每次启动后首次开始分析前必须显示模型数据发送、费用、DPAPI 凭据与本地运行数据告知；用户拒绝时不消费快照、不调用模型。

| 文档 | 职责 |
|---|---|
| [Desktop 规范](DESKTOP_SPEC.md) | GUI、DPAPI、候选快照、一次性分析与日报行为 |
| [Desktop 运行手册](DESKTOP_OPERATIONS.md) | 源码启动、依赖、本机构建、portable 验证与运行排错 |
| [Desktop 结构](DESKTOP_STRUCTURE.md) | Desktop 包内文件及 runtime 数据职责 |

根 [项目文档索引](../README.md) 和 [AGENTS.md](../../AGENTS.md) 继续约束本入口。核心筛选规则唯一来源为 [PROJECT_SPEC.md](../PROJECT_SPEC.md)；本目录说明 GUI、凭据、候选快照和运行行为，不复制两轮筛选、PDF 或标签规范。

开发与公开发布流程见根 [运行手册](../OPERATIONS.md)，整体目录与核心模块职责见根 [项目结构](../PROJECT_STRUCTURE.md)；本目录链接引用这些规则，不另建完成或发布义务。

变更统一记录在 [CHANGELOG.md](../CHANGELOG.md)，不另建 Desktop 变更记录。
