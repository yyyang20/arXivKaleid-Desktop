# 运行与发布手册

本文档维护 Desktop 当前有效的开发、验证和公开发布流程。核心筛选规则见 [当前业务规范](PROJECT_SPEC.md)，源码启动、依赖、本机构建和 portable 验证细节见 [Desktop 运行手册](desktop/DESKTOP_OPERATIONS.md)。文档分工与更新路由以 [文档索引](README.md) 为准。

## 实时核验原则

涉及 Git 或 GitHub 操作时，只读核验与本次操作相关的本地 HEAD、分支、工作树、远程提交、PR、Release、标签和资产。GitHub 状态必须重新查询，不能从文档推断；不得读取 Secret 值。身份、哈希、版本或文件集合不一致时停止。

## Desktop 候选传输与安全诊断

候选抓取由 `desktop/pipeline.py` 调用 `main.fetch_arxiv_metadata()`；查询、分页、超时、间隔与快照失效规则见 [Desktop 规范](desktop/DESKTOP_SPEC.md#候选抓取)。本节维护同次请求的安全诊断边界。

抓取入口将同次请求的结构化证据交给 Desktop 诊断记录器：可解析的非 200 响应记录安全正文特征，200 与非 200 都可记录 transport 证据。它们进入源码 `.desktop-runtime/logs/` 或 portable `runtime/logs/` 下的会话 JSONL，不新增网络请求，不保存响应原文。

### 非 200 响应证据

| 字段 | 含义 |
|---|---|
| `http_status`、`attempt`、`max_attempts` | HTTP 状态、本次尝试序号和最大尝试次数 |
| `body_bytes` | 已接收正文的总字节数，不含 curl 状态标记 |
| `sample_bytes`、`sample_truncated` | 前 8 KiB 样本的实际长度、正文是否超出该范围 |
| `sample_sha256` | 原始样本字节的 SHA-256，不是完整正文哈希 |
| `markers` | 样本中的固定特征标签，无匹配时为空数组 |

特征按字节进行大小写不敏感匹配：`<html` → `html`、`<?xml` → `xml`、`not acceptable` → `not_acceptable`、`access denied` → `access_denied`、`request blocked` → `request_blocked`、`rate exceeded` → `rate_exceeded`、`captcha` → `captcha`。这些仅表示样本中出现对应字节，不能单独证明响应格式、防护拦截或故障根因；样本范围之外的内容不参与分析。

诊断只使用已接收的字节，不限制网络下载大小，不补打正文摘录。连接失败、超时或状态解析失败不生成上述 HTTP 响应证据，沿用现有稳定错误与安全 details。

### 同次请求 transport 证据

| 字段 | 含义 |
|---|---|
| `http_status`、`attempt`、`max_attempts` | 该次 HTTP 响应的状态与尝试序号 |
| `write_out_capture` | 固定 curl write-out 是否通过封闭解析 |
| `remote_ip`、`remote_ip_version`、`remote_port` | 该次 curl 实际连接目标 |
| `http_version`、`ssl_verify_result`、`num_redirects` | HTTP 版本、TLS 校验结果与重定向数 |
| `timings_seconds` | DNS、连接、TLS、首字节和总耗时 |
| `response_header_capture` | 响应头临时捕获状态 |
| `response_headers` | 通过白名单、长度和 ASCII 检查的最终 HTTP 响应头 |
| `response_headers_rejected` | 值不合规的白名单头名，不包含原值 |

响应头白名单为 `date`、`server`、`via`、`age`、`content-type`、`content-length`、`retry-after`、`x-cache`、`x-cache-hits`、`x-served-by`、`x-timer`、`cf-ray`。每项最多保留一个 128 字节的可打印 ASCII 值；Cookie、未知头、控制字符和超限值不得进入日志。

临时响应头文件只存在于当前请求的 runtime 缓存目录，处理后清理；路径不输出，不进入 SQLite 或发行包。不启用 verbose，不记录 stderr、原始响应头、正文原文、Cookie、Secret、代理值或临时路径，不执行额外 DNS、HTTP 或网络探针。

取证准备、解析、日志或清理异常只能导致证据缺失，不能改变原请求返回值或错误码。诊断日志写入失败时降级到当前会话内存，不改变业务成败。日志格式、隐私边界和关联身份见 [两层诊断](desktop/DESKTOP_SPEC.md#两层诊断与失败范围)。

## 本地验证

使用当前已安装环境，不安装或升级依赖。先按 [Desktop 离线验证](desktop/DESKTOP_OPERATIONS.md#离线验证) 将当前进程的 TEMP/TMP 限定在项目内被忽略的目录，再运行：

```powershell
python -B -m unittest discover -s tests -v
git diff --check
git status --short
```

重要代码或文档变更还应检查：

- 所有 Git 跟踪 Python 文件可通过 AST 解析。
- 文档链接、文件引用、当前版本与配置身份一致。
- 配置中的 Prompt 文本 SHA-256 与对应资源一致。
- 测试、报告、数据库、日志、PDF 和 Secret 没有进入 Git。

当前仓库没有 GitHub workflow。`tests/test_desktop_governance.py` 使用 JSON 与 AST 只读核对文档、安全与授权契约、阅读和更新路由、当前身份、内部链接、发行资料副本及发布检查要求；不启动应用、不读取用户运行数据、不调用网络或模型。配置、解耦和构建测试继续验证各自契约。治理测试包含在完整发现与 `test_desktop*.py` 发现中，也可按 Desktop 运行手册单独执行。

记录测试跳过的原因和未覆盖范围；GUI、Windows DPAPI 及发行验收按根 `AGENTS.md` 执行。普通文档与治理测试修改不要求重新构建 portable，实际发行仍须完成冻结成品验证。

## Desktop 公开发布

当前 Desktop 仓库保存应用源码、测试、构建配置和项目文档；Windows portable ZIP 与 `.sha256` 通过本仓库 GitHub Releases 发布，不进入 Git 历史。

portable 根目录的 README、EULA、隐私与安全说明由 `docs/public_release/` 维护；该目录只保存文本。`RELEASE_CHECKLIST.md` 留在开发文档中，不进入 portable ZIP。第三方声明以 `packaging/windows/THIRD_PARTY_NOTICES.txt` 为唯一源文本。

根 README 提供项目概览、源码启动和下载入口；打包 README 面向 portable 用户，分别维护。根 EULA、隐私、安全说明和第三方声明是对应维护源的同步副本；源与副本更新遵循 [文档路由](README.md#文档更新规则)，治理测试核对字节一致性。

本地构建只生成项目内被忽略的 `dist/`、`release/` 和 `.desktop-build/` 产物。`dist/` 保存未压缩发行目录，`release/` 保存当前构建或发行候选的 ZIP 和 `.sha256`。正式历史版本由 GitHub Releases 保存。构建器不自动创建仓库、标签或 Release，也不上传文件。每次公开发布必须基于与最新 `origin/main` 一致的干净提交重建 ZIP，按 [公开发布检查清单](public_release/RELEASE_CHECKLIST.md) 核对文件集、构建身份、用户数据排除、portable 验证和 SHA-256。

构建和发行扫描拒绝 `runtime/`、`logs/`、JSONL、SQLite、PDF、Secret 与本机路径；运行日志不得写入 `_internal/` 或用户目录。

每个新的 Desktop Release 正文必须包含 `## 本次更新`，用面向用户的 1～5 条简短要点说明该版本更新了什么。首个公开版本概括首次提供的主要功能；后续版本只概括相对于上一公开版本的主要新增、修改或修复，依据冻结构建提交、版本间 Git 差异和 `docs/CHANGELOG.md`，不得凭印象编写。平台、下载、SmartScreen、DeepSeek 数据发送与费用、SHA-256 等通用说明必须保留，但不能代替版本更新说明。发布 Draft 前检查该章节存在且非空；发布后重新读取公开 Release，核对正文、Tag、assets、digest 及其他必要说明。

Git commit/push、创建标签或 Release、上传成品都是受控操作，必须在当次获得授权。既有 immutable Release 的标签、ZIP 和校验文件不得修改，修正成品时发布新版本。Release 正文在发布前完成；只有用户明确授权的文案修正任务才可单独编辑已发布正文，并在操作前后对比 Tag 目标及全部 assets 的名称、字节数与 digest，确认发行身份未变。

构建和成品发布基于干净冻结提交；公开 Release 及未登录核验成功后，仍在同一发布任务中按 [文档路由](README.md#文档更新规则) 更新 `docs/CHANGELOG.md`。发布后文档收口不改变冻结成品或公开 Release，不拆成新的独立任务。尚未取得 Git commit/push 授权时，先完成本地文档修改、必要测试和 `git diff --check`，报告待提交状态，不得省略文档或宣称整个发布任务已经完成。

## 功能开发与验证

1. 从最新 `origin/main` 创建 `codex/` 前缀分支。
2. 完成最小范围实现、测试和文档同步。
3. 执行完整本地离线验证和 `git diff --check`。
4. 用户授权 GitHub 写操作后，推送冻结提交并创建 Draft PR。
5. 审查最终差异和测试结果，再将 PR 标记 ready 并按项目约定合并。
6. 合并后核验 `main`、工作树和相关实时 GitHub 状态；付费验证必须另行明确授权。

## 付费与失败规则

- 未获得用户当次明确授权时，只允许零模型、零费用检查。
- 授权必须明确具体入口、次数和费用上限；每个逻辑模型调用最多一个 HTTP attempt，失败后不自动重试或重跑。
- 费用预检使用配置中的峰值快照；实际费用按每次调用前的北京时间价格档位记录。单批费用上限由 Desktop 配置固定。
- 单篇身份、标签、理由、PDF、全文或 Token 问题按当前规范逐篇排除；系统身份、JSON 结构、费用、数据库或资源完整性错误整批停止。
- 失败时保留安全结构诊断、HTTP 状态、`finish_reason`、usage 和费用；不保存或打印模型原始响应、响应 ID、完整 prompt、全文或 Secret。

## 项目内产物

本地数据库、PDF、报告、日志、缓存和审计目录均属于运行产物，不得提交。用户电脑上的所有主动下载和临时审计内容只能写入项目内被忽略的明确目录；不得删除既有忽略目录或未跟踪内容。
