# 项目文档索引

本目录只保存 arXivKaleid 当前有效的项目说明。历史实现、旧运行流水和已失效规范由 Git、PR 和 Issue 保存，不在现行文档中重复维护。

## 权威职责与不一致处理

1. 根目录 `AGENTS.md` 定义不可由普通任务授权突破的安全、授权、任务执行和完成边界；用户当次明确指令只能在该边界内确定任务目标和范围。
2. [当前业务规范](PROJECT_SPEC.md) 定义核心筛选的预期业务行为；[运行与发布手册](OPERATIONS.md) 定义开发、验证和公开发布流程；[项目结构](PROJECT_STRUCTURE.md) 定义整体目录与核心模块职责。
3. [Desktop 文档入口](desktop/README.md) 路由到 [Desktop 规范](desktop/DESKTOP_SPEC.md)、[Desktop 运行手册](desktop/DESKTOP_OPERATIONS.md) 和 [Desktop 结构](desktop/DESKTOP_STRUCTURE.md)：分别维护 GUI、凭据与候选快照行为，源码启动、依赖、本机构建与 portable 验证，以及包内文件与 runtime 数据职责。核心筛选规则以 `PROJECT_SPEC.md` 为唯一来源；两层文档通过链接引用对方负责的细节，不建立相互竞争的规则。
4. 当前代码、配置、提示词、数据库约束和测试记录实际实现。它们用于核验现行规范是否已经落实；与对应规范不一致时应报告并修正不一致，不得静默让实现偏差覆盖规范，也不得脱离实现事实声称功能已经完成。
5. 根目录 [README](../README.md) 只提供用户可见概览和稳定入口。[功能变更记录](CHANGELOG.md) 只记录已完成变化的历史，不定义或覆盖现行规则。

GitHub Run、PR、Issue、artifact 和仓库变量都属于实时外部状态。执行相关任务时必须重新查询，不得从任何 Markdown 文档推断当前值。

GitHub 与项目维护文本的语言统一遵循根目录 [公开维护文本语言](../AGENTS.md#公开维护文本语言)，本索引及下级文档只引用该规则。

Windows portable 的打包 README、用户协议、隐私和安全说明集中在 [公开发布资料](public_release/README.md)；发布前按 [公开发布检查清单](public_release/RELEASE_CHECKLIST.md) 核对。

`docs/public_release/` 是 EULA、PRIVACY、SECURITY 的唯一维护源；修改后同步根目录 `EULA.txt`、`PRIVACY.md`、`SECURITY.md`，保持字节一致。第三方声明的唯一维护源是 `packaging/windows/THIRD_PARTY_NOTICES.txt`，根目录同名文件为同步副本。根仓库 README 与 portable README 面向不同读者，分别维护，不要求内容相同。

应用 GPL-3.0-only 许可证唯一维护源是根目录 `LICENSE`，构建时直接复制到 portable 根目录，不另建许可证副本。许可范围、无保证、对应源码和第三方覆盖说明分别由 README、EULA、第三方声明及发布检查清单维护。

## 新任务阅读顺序

每个新的独立任务开始前必须依次阅读：

1. 完整阅读 `AGENTS.md` 和本文档；根目录 `README.md` 仅在需要了解项目总体定位或稳定入口信息时读取。
2. 根据当前任务读取相关文档：核心业务规则对应 `PROJECT_SPEC.md`，开发、测试或发布流程对应 `OPERATIONS.md`，整体文件与模块职责对应 `PROJECT_STRUCTURE.md`。GUI、凭据或候选快照任务另读 `desktop/DESKTOP_SPEC.md`；源码启动、依赖、本机构建或 portable 验证另读 `desktop/DESKTOP_OPERATIONS.md`；包内文件或 runtime 职责任务另读 `desktop/DESKTOP_STRUCTURE.md`。涉及公开发行资料时读取对应维护源与发布检查清单。
3. 需要历史背景或近期变化时，读取 `CHANGELOG.md` 中与任务相关的最新条目。
4. 任务跨越多个领域或无法确定影响范围时，完整阅读所有受影响领域对应的核心文档。
5. 阅读与当前任务直接相关的配置、提示词、源码、测试和 workflow；发现新的影响范围时动态扩展阅读。

不需要从旧对话复制长篇交接。只有未完成且仓库无法体现的临时状态，才补充简短说明。

## 文档更新规则

功能完成后的统一同步义务由根目录 `AGENTS.md` 规定。本表不建立第二套完成规则，只负责把修改类型路由到需要检查并在受影响时更新的文档。

| 修改类型 | 文档路由 |
|---|---|
| Codex 协作、安全、授权或完成规则 | 根目录 `AGENTS.md`；局部规则受影响时检查 `desktop/AGENTS.md` |
| 文档职责、阅读路线或更新路由 | 本文档；下级文档入口受影响时检查 `desktop/README.md` |
| 用户可见能力或核心说明 | 根目录 `README.md` |
| 用户可见功能的操作入口与步骤、提示词、结果展示、资源限制、费用或数据处理 | `desktop/USER_GUIDE.md`；每个功能任务主动检查，受影响时更新；无影响时在完成报告说明，不强制改写 |
| 候选、筛选、PDF、标签、日报、数据库或费用规则 | `PROJECT_SPEC.md` |
| 开发、验证与公开发布流程 | `OPERATIONS.md`；公开发布检查对应 `public_release/RELEASE_CHECKLIST.md` |
| 文件新增、删除、移动或职责改变 | `PROJECT_STRUCTURE.md` |
| Desktop GUI、凭据或候选快照行为 | `desktop/DESKTOP_SPEC.md`；核心筛选规则变动才更新 `PROJECT_SPEC.md` |
| Desktop 源码启动、依赖、本机构建或 portable 验证 | `desktop/DESKTOP_OPERATIONS.md` |
| Desktop 包内文件或 runtime 数据职责 | `desktop/DESKTOP_STRUCTURE.md`；整体职责受影响时检查 `PROJECT_STRUCTURE.md` |
| Desktop 版本 | `PROJECT_SPEC.md` 当前身份表、根目录 `README.md`、`desktop/README.md`、`desktop/DESKTOP_SPEC.md` 和含版本化运行示例的 `desktop/DESKTOP_OPERATIONS.md` |
| Windows portable 构建依赖或基线 | `desktop/DESKTOP_OPERATIONS.md`；分发组件或许可变化时同时检查 `public_release/` 和第三方声明 |
| 仓库身份或发行资料职责 | 根目录 `README.md`、`OPERATIONS.md`、`PROJECT_STRUCTURE.md` 和 `public_release/RELEASE_CHECKLIST.md` |
| 实际 GitHub Release 发布 | `CHANGELOG.md`；首次发布或稳定下载入口变化时同时更新根目录 `README.md` |
| Windows portable 打包说明、EULA、隐私或安全政策 | `public_release/` 维护源；同步根目录 `EULA.txt`、`PRIVACY.md`、`SECURITY.md`，并检查相关 Desktop 运行文档；两份 README 分别维护 |
| 第三方声明 | `packaging/windows/THIRD_PARTY_NOTICES.txt` 维护源与根目录同名副本；分发组件改变时检查公开发布资料 |
| 应用许可证或对应源码分发 | 根 `LICENSE`、两份 README、EULA、第三方声明、构建/源码 manifest 与 `public_release/RELEASE_CHECKLIST.md`；检查运行和结构文档 |
| 提示词、模板、策略或 schema 身份变化 | 规范、配置、兼容校验、发行校验和测试 |

按文档职责检查所有受影响的相关文档，但只修改真正受影响的文件。禁止为了制造“已同步”的痕迹而加入重复或无意义内容。

[用户指南](desktop/USER_GUIDE.md) 是 GUI“使用说明”的唯一正文维护源，描述当前使用方法；README 提供入口，CHANGELOG 记录变化。指南遵循现行业务规范与 Desktop 规范，不建立竞争规则。更新义务由根 `AGENTS.md` 规定，治理测试检查该义务、路由、链接及打包一致性，不自动改写自然语言或新增后台任务。

预发布测试版的展示名称统一使用小写 `alpha`，例如 `alpha 5`、`alpha 6`；适用于根 README、docs（含 CHANGELOG）、公开发布资料维护源及其他现行说明文本。标准版本标识保持原样，例如 `0.1.0-alpha.6`、`v0.1.0-alpha.6`；不因展示文字统一修改 tag、文件名、URL、代码正式版本常量、Release asset 或 BUILD_INFO 身份。已冻结的 GitHub Release、历史 tag、资产及对应源码状态保持不变；main 上现行文档的修正及必要治理测试通过新的最小提交完成，不重建或替换已发布资产。

## 内容边界

- 现行文档不保存具体历史 Run、PR、Issue、artifact、测试数量或仓库开关值。
- 现行规范不描述已经废弃的行为，也不保留未来设想和未实现功能清单。
- 旧文件可通过 Git 历史恢复；不要在 `docs/` 中建立第二套历史规范。
- 当前版本身份必须与代码和配置一致，由 Desktop 治理测试核对；历史 CHANGELOG 保留当时身份，不作为当前值校验对象。
