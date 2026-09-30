# 项目文档索引

本目录只保存 arXivKaleid 当前有效的项目说明。历史实现、旧运行流水和已失效规范由 Git、PR 和 Issue 保存，不在现行文档中重复维护。

## 权威职责与不一致处理

1. 根目录 `AGENTS.md` 定义不可由普通任务授权突破的安全、授权、任务执行和完成边界；用户当次明确指令只能在该边界内确定任务目标和范围。
2. [当前业务规范](PROJECT_SPEC.md) 定义预期业务行为；[运行与发布手册](OPERATIONS.md) 定义运行、测试、恢复和发布流程；[项目结构](PROJECT_STRUCTURE.md) 定义文件与模块职责。
3. [Desktop 文档入口](desktop/README.md) 及其下级文档只定义 Desktop 特有行为、运行和结构，共享业务规则仍以 `PROJECT_SPEC.md` 为唯一来源。
4. 当前代码、配置、提示词、数据库约束和测试记录实际实现。它们用于核验现行规范是否已经落实；与对应规范不一致时应报告并修正不一致，不得静默让实现偏差覆盖规范，也不得脱离实现事实声称功能已经完成。
5. 根目录 [README](../README.md) 只提供用户可见概览和稳定入口。[功能变更记录](CHANGELOG.md) 只记录已完成变化的历史，不定义或覆盖现行规则。

GitHub Run、PR、Issue、artifact 和仓库变量都属于实时外部状态。执行相关任务时必须重新查询，不得从任何 Markdown 文档推断当前值。

Windows portable 的公开仓库文档、用户协议、隐私和安全说明集中在 [公开发布资料](public_release/README.md)；发布前按 [公开发布检查清单](public_release/RELEASE_CHECKLIST.md) 核对。

## 新任务阅读顺序

每个新的独立任务开始前必须依次阅读：

1. 完整阅读 `AGENTS.md` 和本文档；根目录 `README.md` 仅在需要了解项目总体定位或稳定入口信息时读取。
2. 根据当前任务读取相关文档：业务规则对应 `PROJECT_SPEC.md`，运行方式、测试流程或运维流程对应 `OPERATIONS.md`，文件与模块职责对应 `PROJECT_STRUCTURE.md`。
3. 需要历史背景或近期变化时，读取 `CHANGELOG.md` 中与任务相关的最新条目。
4. 任务跨越多个领域或无法确定影响范围时，完整阅读所有受影响领域对应的核心文档。
5. 阅读与当前任务直接相关的配置、提示词、源码、测试和 workflow；发现新的影响范围时动态扩展阅读。

不需要从旧对话复制长篇交接。只有未完成且仓库无法体现的临时状态，才补充简短说明。

## 文档更新规则

功能完成后的统一同步义务由根目录 `AGENTS.md` 规定。本表不建立第二套完成规则，只负责把修改类型路由到需要检查并在受影响时更新的文档。

| 修改类型 | 文档路由 |
|---|---|
| 用户可见能力或核心说明 | 根目录 `README.md` |
| 候选、筛选、PDF、标签、日报、数据库或费用规则 | `PROJECT_SPEC.md` |
| workflow、发布、恢复、测试或上线流程 | `OPERATIONS.md` |
| 文件新增、删除、移动或职责改变 | `PROJECT_STRUCTURE.md` |
| Desktop GUI、凭据、候选入口、运行或结构 | 对应 `desktop/` 文档；共享规则变动才更新 `PROJECT_SPEC.md` |
| Desktop 版本 | 根目录 `README.md`、`desktop/README.md`、`desktop/DESKTOP_SPEC.md` 和含版本化运行示例的 `desktop/DESKTOP_OPERATIONS.md` |
| Windows portable 构建依赖或基线 | `desktop/DESKTOP_OPERATIONS.md`；分发组件或许可变化时同时检查 `public_release/` 和第三方声明 |
| 公开仓库身份或同步边界 | 根目录 `README.md`、`OPERATIONS.md`、`PROJECT_STRUCTURE.md` 和 `public_release/RELEASE_CHECKLIST.md` |
| 实际 GitHub Release 发布 | `CHANGELOG.md`；首次发布或稳定下载入口变化时同时更新根目录 `README.md` |
| Windows portable 公开仓库说明、EULA、隐私或安全政策 | `public_release/` 及相关 Desktop 运行文档 |
| 提示词、模板、策略或 schema 身份变化 | 规范、配置、兼容校验和测试 |

按文档职责检查所有受影响的相关文档，但只修改真正受影响的文件。禁止为了制造“已同步”的痕迹而加入重复或无意义内容。

## 内容边界

- 现行文档不保存具体历史 Run、PR、Issue、artifact、测试数量或仓库开关值。
- 现行规范不描述已经废弃的行为，也不保留未来设想和未实现功能清单。
- 旧文件可通过 Git 历史恢复；不要在 `docs/` 中建立第二套历史规范。
- 当前版本身份必须与代码和配置一致，并由治理测试自动核对。
