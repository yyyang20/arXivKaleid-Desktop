# 项目结构

本文档维护 arXivKaleid Desktop 整体目录与核心模块职责；包内文件和 runtime 数据细节见 [Desktop 结构](desktop/DESKTOP_STRUCTURE.md)。核心筛选规则见 [当前业务规范](PROJECT_SPEC.md)，开发与发布流程见 [运行与发布手册](OPERATIONS.md)，文档分工见 [文档索引](README.md)。

## 根目录与核心模块

| 路径 | 职责 |
|---|---|
| `AGENTS.md` | 项目级长期协作、安全、按需阅读和必要授权规则 |
| `README.md` | 项目概览、源码启动、下载和文档入口 |
| `EULA.txt`、`PRIVACY.md`、`SECURITY.md` | 仓库用户可见的应用许可、隐私与安全说明；与 `docs/public_release/` 维护源保持字节一致 |
| `THIRD_PARTY_NOTICES.txt` | 仓库用户可见的第三方声明；源文本位于 `packaging/windows/` |
| `config.json` | Desktop 配置身份、模型、Prompt、筛选策略、Token、费用和资源哈希；不包含 API Key |
| `requirements.txt` | 固定 pypdf 核心运行依赖 |
| `requirements-desktop.txt` | 引入核心运行依赖并固定 PySide6 |
| `requirements-build.txt` | 固定 PyInstaller 及仅构建阶段依赖 |
| `desktop/` | GUI、候选快照、DPAPI、配置、诊断、进度、一次性分析和 Markdown 日报；详见 [Desktop 结构](desktop/DESKTOP_STRUCTURE.md) |
| `main.py` | arXiv URL、curl 传输、Atom 解析、排序合并、Desktop 主库 schema 与筛选保存核心函数；无 CLI 启动入口 |
| `arxiv_transport_evidence.py` | curl write-out、timing 和响应头白名单纯解析边界 |
| `deepseek_client.py` | Chat Completions 与 Responses 单次请求、命名函数结果传输、usage 和安全错误分类 |
| `content_labels.py` | 四类内容标签和现有标签解析辅助函数 |
| `pdf_processing.py` | 精确版本 PDF 下载、校验、哈希、文本提取和进度回调 |
| `round2_fulltext_state.py` | 全文库、物理页数门控、逐页全文、Token 预检和进度回调 |
| `build_round2_inputs.py` | 当前工作库 Round 1 结果、全文资格与只读 SQLite 输入辅助函数 |
| `run_round2.py` | 当前协议 Round 2 输入、命名结果工具契约、显式 Key 注入、缓存和事务保存 |
| `model_usage.py` | 模型 attempt、成功调用模型、Token、价格快照、费用和阶段耗时 |
| `rebuild_daily_report.py` | 当前 run 的 Round 1 论文和全文门控事实读取辅助函数 |
| `generate_round2_report.py` | 当前 Round 2 结果验证、页数与推荐区块渲染辅助函数 |
| `packaging/windows/` | one-folder spec、冻结入口、portable 自检和 vendor/许可证元数据 |
| `scripts/` | 本地 Windows portable 构建、许可证收集和零模型验证入口 |

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
   └─ DESKTOP_STRUCTURE.md
```

`docs/` 保存当前有效说明和简短变更记录，不保存旧规范副本。历史内容通过 Git 查询。

`docs/public_release/` 保存 portable 根目录四份应用文档的维护源和发布检查清单，只保存文本，不保存 ZIP 或运行数据。`RELEASE_CHECKLIST.md` 不进入 ZIP。第三方声明以 `packaging/windows/THIRD_PARTY_NOTICES.txt` 为唯一源文本。根 README 与打包 README 分别面向源码仓库读者和 portable 用户。

## 配置和提示词

| 路径 | 职责 |
|---|---|
| `desktop/config.py` | `desktop_config_v1` 只读契约、预算边界和资源文本哈希校验 |
| `prompts/relevance_round1_v20.txt` | 自包含 Round 1 摘要筛选契约 |
| `prompts/relevance_round2_v15.txt` | 自包含 Round 2 全文筛选契约 |

提示词按版本新增，不覆盖旧文件。当前运行和发行资源只包含 `config.json` 与上述两份 Prompt；不依赖独立自动化策略或 Research Profile 文件。`profile_v2` 保留为请求、SQLite 和缓存兼容标识。

## 测试

`tests/` 使用标准库 `unittest`：

| 测试 | 当前覆盖 |
|---|---|
| `test_desktop_config.py` | 当前配置、预算、模型协议、Prompt 路径及哈希，拒绝旧 Online 配置字段 |
| `test_desktop_decoupling.py` | alpha.4 合成场景行为基线、主库 schema v1、缓存、预算、导入边界和隔离源码运行 |
| `test_desktop_pipeline.py` | 假 Atom、日期边界、计数、排序、冻结快照、进度及安全传输诊断 |
| `test_desktop_analysis.py` | mock HTTP 两轮集成、门控、预算、单次 attempt、工作数据隔离和日报事实 |
| `test_desktop_secrets.py` | 密文落盘、错误、路径和 Windows DPAPI 假值往返 |
| `test_desktop_app.py` | offscreen GUI、告知拒绝、线程、凭据交互和日报展示失败 |
| `test_desktop_diagnostics.py` | JSONL、身份关联、作用域、线程安全、内存降级和隐私 canary |
| `test_desktop_portable.py` | source/frozen 路径、链接、可写性、bundled curl 和工作库边界 |
| `test_desktop_build.py` | checksum、x64、资源 allowlist、固定依赖、打包文档复制及发行数据排除 |
| `test_desktop_governance.py` | 文档入口、安全与授权、阅读和更新路由、身份表、链接、发行资料副本及发布检查要求；内存反例验证 |

治理测试只读检查维护文件，不启动应用或读取运行数据；构建测试验证打包复制和发行扫描，两者职责不同。发布清单的静态检查不能代替实际成品或 GitHub 发布核验。新增功能必须新增或更新对应测试，文档不以固定测试数量描述当前状态。

## 运行数据与忽略内容

源码运行数据位于 `.desktop-runtime/`，portable 对应 EXE 同级 `runtime/`；详细目录见 [Desktop 当前运行数据](desktop/DESKTOP_STRUCTURE.md#当前运行数据)。

- 工作库、锁、PDF、DPAPI 密文、请求缓存和会话日志的具体路径及职责见 [Desktop 当前运行数据](desktop/DESKTOP_STRUCTURE.md#当前运行数据)；SQLite 数据边界见 [当前业务规范](PROJECT_SPEC.md#sqlite-工作数据)。
- 候选快照只在内存中冻结；日报字符串传给 GUI，不写根 `reports/`，不形成跨运行历史数据库。
- `build/`、`dist/`、`release/`、`.desktop-build/`、`.codex-validation/`、`.codex-audit-*/`、Python 缓存及运行数据被 Git 忽略。
- `.gitignore` 仍保护旧 `config/local_secret.json`、`data/` 数据库、PDF、`reports/` 和日志等路径；这些忽略规则不表示旧运行入口仍然存在。

SQLite、PDF、报告、日志、缓存、审计目录和未跟踪文件不得因普通代码或文档任务被删除。下载或生成临时审计内容只能使用项目内明确的忽略目录。路径配置使用相对路径，代码写入前验证解析后的目标仍在规定根目录内。
