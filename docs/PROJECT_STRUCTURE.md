# 项目结构

本文档维护 arXivKaleid Desktop 整体目录与核心模块职责；包内文件和 runtime 数据细节见 [Desktop 结构](desktop/DESKTOP_STRUCTURE.md)。核心筛选规则见 [当前业务规范](PROJECT_SPEC.md)，开发与发布流程见 [运行与发布手册](OPERATIONS.md)，文档分工见 [文档索引](README.md)。

## 根目录与核心模块

| 路径 | 职责 |
|---|---|
| `AGENTS.md` | 项目级长期协作、安全、按需阅读和必要授权规则 |
| `README.md` | 项目概览、源码启动、下载和文档入口 |
| `LICENSE` | 标准 GPLv3 全文唯一维护源；自有应用采用 GPL-3.0-only，构建时直接复制到 portable 根目录 |
| `EULA.txt`、`PRIVACY.md`、`SECURITY.md` | 仓库用户可见的应用许可、隐私与安全说明；与 `docs/public_release/` 维护源保持字节一致 |
| `THIRD_PARTY_NOTICES.txt` | 仓库用户可见的第三方声明；源文本位于 `packaging/windows/` |
| `config.json` | Desktop 配置身份、模型、Prompt、筛选策略、Token、费用和资源哈希；不包含 API Key |
| `requirements.txt` | 固定 pypdf 核心运行依赖 |
| `requirements-desktop.txt` | 核心 PDF、PySide6、基础 Fluent 与 Windows 必要依赖固定版本，不使用 full |
| `requirements-build.txt` | 固定 PyInstaller 及仅构建阶段依赖 |
| `assets/` | A0 应用图标 SVG 母版、Windows 多尺寸 ICO 与使用说明线框书本 SVG |
| `desktop/` | GUI、候选快照、DPAPI、配置、诊断、进度、一次性分析和 Markdown 日报；详见 [Desktop 结构](desktop/DESKTOP_STRUCTURE.md) |
| `main.py` | arXiv URL、curl 传输、Atom 解析、排序合并、Desktop 主库 schema 与筛选保存核心函数；无 CLI 启动入口 |
| `arxiv_transport_evidence.py` | curl write-out、timing 和响应头白名单纯解析边界 |
| `deepseek_client.py` | Chat Completions 与 Responses 单次请求、命名函数结果传输、usage 和安全错误分类 |
| `content_labels.py` | 历史领域标签辅助函数；当前引擎与冻结程序不依赖 |
| `pdf_processing.py` | 精确版本 PDF 下载、校验、哈希、文本提取和进度回调 |
| `round2_fulltext_state.py` | 全文库、物理页数门控、逐页全文、Token 预检和进度回调 |
| `build_round2_inputs.py` | 当前工作库 Round 1 结果、全文资格与只读 SQLite 输入辅助函数 |
| `run_round2.py` | 当前协议 Round 2 输入、命名结果工具契约、显式 Key 注入、缓存和事务保存 |
| `model_usage.py` | 模型 attempt、成功调用模型、Token、价格快照、费用和阶段耗时 |
| `rebuild_daily_report.py` | 当前 run 的 Round 1 论文和全文门控事实读取辅助函数 |
| `generate_round2_report.py` | 当前 Round 2 结果验证、页数与推荐区块渲染辅助函数 |
| `packaging/windows/` | one-folder spec、冻结入口、portable 自检和 vendor/许可证元数据 |
| `scripts/` | 本地 Windows portable 构建、许可证收集、零模型验证及隔离离线 GUI 视觉 QA 入口 |
| `scripts/local_artifacts.py` | 项目内生命周期、进程锁、容量和留存门槛、准确处置计划；不自动淘汰历史材料 |
| `scripts/run_local_checks.py` | 独立 TEMP/TMP 与测试根目录的离线 unittest 薄入口 |
| `scripts/verify_public_release.py` | 匿名只读核验发行资产、冻结源码与历史基线；不执行远端写入 |

根目录保留核心函数模块供 Desktop 调用；应用启动入口为 `python -B -m desktop.app`。当前仓库没有 Online 自动化、云端状态、恢复、错误样本或 Issue 日报发布模块，也没有 GitHub workflow。

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
   ├─ DESKTOP_STRUCTURE.md
   ├─ USER_GUIDE.md
   └─ PROMPT_SPLIT.md
```

`docs/` 保存当前有效说明和简短变更记录，不保存旧规范副本。历史内容通过 Git 查询。

`docs/public_release/` 保存 portable 根目录四份应用文档的维护源和发布检查清单，只保存文本，不保存 ZIP 或运行数据。`RELEASE_CHECKLIST.md` 不进入 ZIP。根 LICENSE 直接打包，不在该目录重复维护。第三方声明以 `packaging/windows/THIRD_PARTY_NOTICES.txt` 为唯一源文本。根 README 与打包 README 分别面向源码仓库读者和 portable 用户。

## 配置和提示词

| 路径 | 职责 |
|---|---|
| `desktop/config.py` | `desktop_config_v5` 只读契约、预算边界和四份资源文本哈希校验 |
| `desktop/research_requirements.py` | 两轮独立保存、校验、默认读取及分析快照 |
| `desktop/prompt_page.py` | 完整研究提示词查看/编辑、保存、取消、恢复与未保存保护 |
| `desktop/user_guide.py` | 使用说明资源读取、冻结哈希、只读 Markdown 和内部导航入口 |
| `docs/desktop/USER_GUIDE.md` | GUI 使用说明唯一正文维护源，用户可见功能变化时按文档路由同步 |
| `prompts/relevance_round1_v23.txt` | Round 1 最小技术协议，无固定篇数限制 |
| `prompts/relevance_round2_v18.txt` | Round 2 最小技术协议，无固定篇数限制，以数组顺序表达排名 |
| `prompts/research_prompt_round1_v2.txt`、`prompts/research_prompt_round2_v2.txt` | 两轮可编辑的版本化完整默认研究提示词 |
| 其他历史 Prompt | 原样保留，不进入运行与打包 |

提示词按版本新增，不覆盖旧文件。研究资源包含 `config.json` 与上述四份 Prompt；发行还包含正式 ICO、使用说明 SVG 和只读用户指南，均绑定 BUILD_INFO 哈希。不依赖独立自动化策略或 Research Profile 文件。`profile_v2` 保留为请求、SQLite 和缓存兼容标识。

## 测试

`tests/` 使用标准库 `unittest`：

| 测试 | 当前覆盖 |
|---|---|
| `test_desktop_config.py` | 当前配置、预算、模型协议、Prompt 路径及哈希，拒绝旧 Online 配置字段 |
| `test_desktop_decoupling.py` | 合成场景技术门禁基线、主库 schema v1、缓存、预算、导入边界和隔离源码运行 |
| `test_desktop_pipeline.py` | 假 Atom、日期边界、计数、排序、冻结快照、进度及安全传输诊断 |
| `test_desktop_analysis.py` | mock HTTP 两轮集成、可选自由评价、门控、预算、单次 attempt、工作数据隔离和日报事实 |
| `test_research_prompt_protocol.py` | 最小身份协议、跨领域自由评价、第二轮交接独立性、Token 并列裁决、缓存及安全日报 |
| `test_desktop_secrets.py` | 密文落盘、错误、路径和 Windows DPAPI 假值往返 |
| `test_desktop_app.py` | offscreen GUI、告知拒绝、线程、凭据交互和日报展示失败 |
| `test_desktop_diagnostics.py` | JSONL、身份关联、作用域、线程安全、内存降级和隐私 canary |
| `test_desktop_portable.py` | source/frozen 路径、链接、可写性、bundled curl 和工作库边界 |
| `test_desktop_build.py` | checksum、x64、资源 allowlist、固定依赖、LICENSE/文档复制、源码版本/许可/归档校验及发行数据排除 |
| `test_portable_diagnostic.py` | 独立进程完整诊断路径、Windows 合成 DPAPI、后置失败与已用 runtime 拒绝 |
| `test_desktop_governance.py` | 文档入口、安全与授权、阅读和更新路由、身份表、链接、发行资料副本及发布检查要求；内存反例验证 |
| `test_desktop_artifacts.py` | 成功、失败、中断、超时、锁、容量、清理安全、许可 manifest 和 mock 发布核验 |

治理测试只读检查维护文件，不启动应用或读取运行数据；构建测试验证打包复制和发行扫描，两者职责不同。发布清单的静态检查不能代替实际成品或 GitHub 发布核验。新增功能必须新增或更新对应测试，文档不以固定测试数量描述当前状态。

## 运行数据与忽略内容

源码运行数据位于 `.desktop-runtime/`，portable 对应 EXE 同级 `runtime/`；详细目录见 [Desktop 当前运行数据](desktop/DESKTOP_STRUCTURE.md#当前运行数据)。

- 工作库、锁、PDF、DPAPI 密文、请求缓存和会话日志的具体路径及职责见 [Desktop 当前运行数据](desktop/DESKTOP_STRUCTURE.md#当前运行数据)；SQLite 数据边界见 [当前业务规范](PROJECT_SPEC.md#sqlite-工作数据)。
- 候选快照只在内存中冻结；成功日报的原始 Markdown 与计数进入独立 `history/history.sqlite`，不写根 `reports/`，不参与后续候选、筛选或工作库重置。
- `build/`、`dist/`、`release/`、`.desktop-build/`、`.codex-validation/`、`.codex-audit-*/`、Python 缓存及运行数据被 Git 忽略。
- `release/` 和 `dist/` 在构建和验证期间保留候选及旧成品；正式发布和新旧远端核验成功后，按分别列明的当次授权精确收口，`release/` 只保留最新正式 ZIP 与 `.sha256`，`dist/` 只保留当前正式版本及仍有明确必要的成品。旧 `dist/` 必须与对应远端正式 ZIP 逐文件一致且已无构建、验证或待审用途，含 `runtime/` 的目录继续保护。历史正式版本存于 GitHub Releases；授权、核验和精确清理边界见 [本地历史 portable 收口](OPERATIONS.md#本地历史-portable-收口)，不清理其他忽略材料。
- `.gitignore` 仍保护旧 `config/local_secret.json`、`data/` 数据库、PDF、`reports/` 和日志等路径；这些忽略规则不表示旧运行入口仍然存在。

SQLite、PDF、报告、日志、缓存、审计目录和未跟踪文件不得因普通代码或文档任务被删除。当前任务自产合成数据及可重建工作文件仅按 [临时产物生命周期](OPERATIONS.md#临时产物生命周期) 收尾，不涉及用户或历史数据。`.desktop-build/managed-artifacts/` 保存登记、证据和受保护 previous-*；测试 work 独立位于 `.codex-validation/managed-artifacts/`。`.desktop-build/inputs/conda-licenses/` 是稳定、被忽略的构建输入，不是临时清理目标；来源发行 ZIP 和 vendor 继续保留。退役的历史包装脚本及旧许可映射目录不是当前依赖，须经依赖复核及当次明确授权后才能清理。下载或生成临时审计内容只能使用项目内明确的忽略目录。路径配置使用相对路径，代码写入前验证解析后的目标仍在规定根目录内。
