# Desktop 协作规则

先遵循根目录 [AGENTS.md](../AGENTS.md) 和 [文档索引](../docs/README.md)，再阅读 [Desktop 文档入口](../docs/desktop/README.md)。

- 当前阶段包含 PySide6 GUI、submittedDate 候选抓取、DPAPI 凭据保存、两轮分析和 Markdown 日报渲染。
- 复用根目录候选、筛选、PDF、全文、usage 和报告函数；`main.py` 提供核心函数，不是应用启动入口。
- 抓取和分析在 QThread 中运行；控件仅由 GUI 线程更新；新抓取立即废弃旧快照并清除日报。
- 源码模式的运行数据只能位于项目内被忽略的 `.desktop-runtime/`；portable 模式只能位于 EXE 同级 `runtime/`。只读资源与运行数据分离，不依赖 cwd，不回退到用户目录；禁止明文凭据落盘或输出。
- 测试使用假凭据、假网络和隔离目录，不接触用户实际 Secret；无 PySide6 时仅跳过 GUI 测试，并按根规则报告未覆盖范围，不能以跳过代替 GUI 验收。
- 分析只消费已冻结快照一次，不重抓候选、不查历史完成状态；成功或失败都不能重试同一快照。
- GUI Key 仅在内存中注入两轮客户端，不读取传统 Secret 文件；不执行付费 self check，每轮最多一次 HTTP attempt，预算预检累计本批费用并受 ¥3.00 限制。
- 仅重置当前运行根 `work/` 两个已知工作 SQLite 及其 sidecar；持有工作目录文件锁期间操作，保留 Secret、缓存和 PDF。日报只在 GUI 中显示，不写正式 reports 目录。
- Windows x64 portable 使用固定 PyInstaller one-folder 构建，curl 只允许已校验的 bundled 版本；发行资源按 allowlist 收集，禁止带入用户运行数据。构建、网络诊断与模型调用授权分别处理。
- Desktop 文档说明 GUI 入口行为，核心筛选规则由 `docs/PROJECT_SPEC.md` 定义；统一更新根 `docs/CHANGELOG.md`。
