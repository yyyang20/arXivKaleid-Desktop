# 项目结构

本文档说明 arXivKaleid 当前文件分布和职责。业务规则见 [当前业务规范](PROJECT_SPEC.md)，运行流程见 [运行与发布手册](OPERATIONS.md)。

## 根目录

| 路径 | 职责 |
|---|---|
| `.vscode/settings.json` | 可移植的 VS Code Conda 环境管理与包管理偏好；不包含解释器路径或环境名，不创建、绑定或修改具体 Conda 环境 |
| `AGENTS.md` | 项目级长期协作、安全、按需阅读和必要授权规则 |
| `README.md` | 简洁项目概览和文档入口 |
| `config.json` | 分类、模型、提示词、策略、Token、费用和项目相对路径配置；不得包含 API key |
| `requirements.txt` | GitHub runner 所需的最小固定运行依赖 |
| `requirements-desktop.txt` | Windows Desktop 专用固定 PySide6 依赖 |
| `requirements-build.txt` | Windows portable 专用固定 PyInstaller 构建依赖 |
| `packaging/windows/` | Desktop one-folder spec、冻结入口、诊断与 vendor/许可证元数据 |
| `scripts/` | 本地 Windows portable 构建、许可证收集和零模型验证；职责见 Desktop 结构 |
| `desktop/` | submittedDate 候选、DPAPI、两层诊断、真实运行进度、当前快照两轮分析与 GUI Markdown 日报薄层；详见 [Desktop 结构](desktop/DESKTOP_STRUCTURE.md) |
| `main.py` | 本地元数据、Round 1、SQLite 和 PDF 阶段的核心实现及历史兼容入口 |
| `run_daily_pipeline.py` | 本地一键编排入口，不复制业务逻辑 |
| `automation_daily.py` | 正式候选扫描、G2至G7状态链、schema迁移、发布审计、Round 2空内容恢复谱系、失败传输证据迁移和错误样本账本 |
| `arxiv_transport_evidence.py` | 正式 metadata 与独立 transport probe 共用的 curl write-out、timing 和响应头白名单纯解析边界 |
| `arxiv_metadata_probe.py` | 手动 Ubuntu 固定分页对照，复用正式 URL 与 metadata 传输，只输出安全证据，不进入正式状态或模型流程 |
| `arxiv_transport_probe.py` | 第三阶段本地/Ubuntu 传输环境 2×2 对照；独立采集 DNS、目标 IP、TLS、timing 和白名单响应头，不调用正式传输函数 |
| `deepseek_responses_probe.py` | DeepSeek Responses一次性最小兼容性探针；仅保存脱敏诊断，不进入正式日报链 |
| `controlled_replay.py` | 按准确历史来源执行四种受控重做模式，并为完整两轮模式校验 G6 来源及相邻 G7 错误样本合并 |
| `controlled_replay_publish.py` | 零模型审计并创建或更新 Top 0 至 5 的完整两轮受控重做日报 |

## 文档

```text
docs/
├─ README.md
├─ PROJECT_SPEC.md
├─ OPERATIONS.md
├─ PROJECT_STRUCTURE.md
├─ CHANGELOG.md
├─ public_release/
│  ├─ README.md
│  ├─ EULA.txt
│  ├─ PRIVACY.md
│  ├─ SECURITY.md
│  └─ RELEASE_CHECKLIST.md
└─ desktop/
   ├─ README.md
   ├─ DESKTOP_SPEC.md
   ├─ DESKTOP_OPERATIONS.md
   └─ DESKTOP_STRUCTURE.md
```

`docs/` 只包含当前有效说明和简短功能变更记录，不保存旧规范副本。历史内容通过 Git 查询。

`docs/public_release/` 是公开下载仓库 [yyyang20/arXivKaleid-Desktop](https://github.com/yyyang20/arXivKaleid-Desktop) 与 portable 根目录中应用级文档的维护源，只保存文本，不保存 ZIP 或运行数据。第三方声明继续以 `packaging/windows/THIRD_PARTY_NOTICES.txt` 为唯一来源。

## 配置、兼容资源和提示词

| 路径 | 职责 |
|---|---|
| `config/automation_policy.json` | 正式自动化冻结身份、费用边界及受保护文件哈希 |
| `config/research_profile.json` | 旧 `profile_v2` 恢复与历史审计使用的只读兼容资源；新正常请求不发送其内容 |
| `config/local_secret.example.json` | 本地 Secret 文件格式示例，不含真实凭据 |
| `config/local_secret.json` | 用户本机 Secret；被 Git 忽略，禁止读取内容、输出或提交 |
| `profiles/research_profile.md` | 历史人工说明与审计使用的只读兼容资源；不进入新正常请求或 Desktop 发行包 |
| `prompts/relevance_round1_v20.txt` | 当前自包含 Round 1 摘要筛选契约 |
| `prompts/relevance_round2_v15.txt` | 当前自包含 Round 2 全文筛选契约 |

提示词必须按版本新增，不覆盖旧文件。旧 Prompt 与 `profile_v1/profile_v2` 资源保留用于精确历史恢复、来源定位和审计；新正常请求只使用两份现行自包含 Prompt，并仅回显 `profile_v2` 兼容身份。

## 筛选与报告源码

| 文件 | 职责 |
|---|---|
| `deepseek_client.py` | Chat Completions与Responses单次请求、思考模式、命名函数结果传输、usage和安全错误分类 |
| `content_labels.py` | 当前四类内容标签及历史标签只读兼容 |
| `selection_nature.py` | 仅支持历史入选性质和A–E层级回读 |
| `pdf_processing.py` | 精确版本PDF下载、校验、哈希和文本提取；可选进度回调默认关闭 |
| `round2_fulltext_state.py` | 物理页数门控、逐页全文和Token预检；可选进度回调默认关闭 |
| `run_round2.py` | Round 2预检、命名结果工具契约、调用、缓存和事务保存 |
| `model_usage.py` | 模型attempt、实际成功调用模型、Token、价格快照、费用和阶段耗时 |
| `daily_report_template.py` | 正式日报Markdown外壳、阶段计数和标签统计 |
| `rebuild_daily_report.py` | 从SQLite只读装载稳定数据并生成正式日报 |
| `daily_report_publish.py` | 分模板版本校验报告与数据库一致性 |
| `generate_round2_report.py` | 独立Round 2报告及历史兼容渲染 |

## 云端状态模块

| 文件 | 职责 |
|---|---|
| `cloud_metadata_state.py` | 历史metadata阶段状态 |
| `cloud_round1_preflight.py` | Round 1零模型预检 |
| `cloud_round1_state.py` | Round 1状态与artifact |
| `cloud_pdf_state.py` | 临时PDF、全文和generation 5状态 |
| `cloud_round2_state.py` | Round 2预检、输出传输身份、付费执行和generation 6状态 |
| `cloud_dry_run.py` | 零模型云端预检 |
| `cloud_blank_state.py` | 空白状态与恢复边界验证 |

这些模块继续支持当前自动化及受控诊断，不得通过文档重构改变其产物身份。

## 修复与兼容工具

| 文件 | 职责 |
|---|---|
| `published_report_repair.py` | 已发布报告的受限Markdown前向修复 |
| `published_timing_repair.py` | 已发布报告时间归属的受限前向修复 |
| `import_report_feedback.py` | 历史反馈日报只读兼容 |
| `build_round2_inputs.py` | 历史Round 2输入诊断工具 |
| `extract_pdf_sections.py` | PDF文本提取诊断入口 |
| `preview_arxiv_metadata.py` | arXiv元数据只读预览和传统日期回退诊断 |

## GitHub workflows

| 分组 | 入口 |
|---|---|
| 正式自动化 | `.github/workflows/automation-daily.yml` |
| arXiv 分页对照诊断 | `.github/workflows/arxiv-metadata-probe.yml`；独立手动、零模型、只读权限、不上传 artifact |
| arXiv 传输环境对照 | `.github/workflows/arxiv-transport-probe.yml`；固定正式 G1 URL 优先的两请求对照、独立手动、零模型、只读权限、不上传 artifact |
| Round 2异常恢复及零模型发布 | `.github/workflows/automation-round2-recovery.yml`、`.github/workflows/automation-round2-recovery-publish.yml` |
| 受控重做及发布 | `.github/workflows/controlled-replay.yml`、`.github/workflows/controlled-replay-publish.yml` |
| 错误样本 | `.github/workflows/screening-error-samples.yml` |
| 独立日报发布 | `.github/workflows/daily.yml` |
| 已发布报告修复 | `.github/workflows/published-report-repair.yml`、`.github/workflows/published-timing-repair.yml` |
| 零费用验证 | `.github/workflows/test.yml`、`.github/workflows/cloud-dry-run.yml` |
| Responses兼容性诊断 | `.github/workflows/deepseek-responses-probe.yml` |
| 分阶段诊断 | 其余 `cloud-*` 和元数据预览 workflow |

所有 workflow 使用最小权限和固定 Action SHA。默认手动入口必须保持零费用或显式确认，不能因文档调整扩大权限。

## 测试

`tests/` 使用标准库 `unittest`，主要覆盖：

- Desktop 候选快照、DPAPI 密文边界、mock HTTP 两轮分析及费用门控、日报事实和 PySide6 offscreen 状态测试。
- Desktop 稳定错误/outcome、PDF/全文实际影响范围、脱敏 JSONL、身份关联、内存降级和隐私 canary。
- arXiv解析、候选排序、跨分类去重、版本身份以及正式非 200 transport 取证的 fail-open 和泄漏边界。
- `test_arxiv_metadata_probe.py` 覆盖分页对照顺序、正式传输参数、重试与停止、诊断安全、冻结提交和 workflow 隔离。
- `test_arxiv_transport_probe.py` 覆盖正式 URL 优先的 2×2 顺序、独立 curl 采集、DNS/TLS/timing/响应头安全、冻结提交和 workflow 隔离。
- Round 1、PDF、全文、Round 2及逐篇/整批失败边界。
- 自动化schema、G2至G7、artifact、费用、时间、历史策略兼容和零模型发布恢复。
- 日报数据层、四种正常退化、展示模板和历史版本兼容。
- 错误样本幂等、冲突、分类一致性和零模型workflow。
- 受控重做、已发布报告修复及源账本不变。
- `test_project_governance.py` 对文档结构、链接、身份以及 `AGENTS.md` 的安全和授权契约进行保护。
- 公开发布文档测试核对固定文件集、纯文本边界、隐私告知要点、ZIP 排除和 Desktop Release 版本更新说明流程。

新增功能必须新增或更新对应测试，不以固定测试数量作为文档中的当前状态。

## 运行数据与忽略内容

以下属于项目内运行产物，不得提交：

```text
config/local_secret.json
.codex-validation/
.desktop-runtime/
.codex-audit-*/
.pytest_cache/
__pycache__/
*.pyc
data/*.sqlite
data/pdfs/
reports/
logs/*.log
cache/arxiv/last_request_time.txt
```

SQLite、PDF、报告、日志、缓存、审计目录和未跟踪文件不得因普通代码或文档任务被删除。需要下载或生成临时审计内容时，只能使用项目内明确的忽略目录。

## 数据职责

- 主数据库 `data/arxiv_kaleid.sqlite` 保存论文、运行、筛选、usage、费用、PDF状态、日报、错误样本和自动化账本。
- 短期 `data/round2_inputs.sqlite` 或artifact中的同名数据库保存实际页数和合格论文逐页全文。
- `reports/` 保存本地生成报告；GitHub Issue是正式云端日报展示渠道。
- generation artifact 保存经过manifest和哈希校验的云端滚动状态，不包含PDF或Secret。

所有项目路径配置应使用相对路径；代码写入前必须验证解析后的目标仍位于项目根目录。
