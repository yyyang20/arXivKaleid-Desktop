# 运行与发布手册

本文档维护 Desktop 当前有效的开发、验证和公开发布流程。核心筛选规则见 [当前业务规范](PROJECT_SPEC.md)，源码启动、依赖、本机构建和 portable 验证细节见 [Desktop 运行手册](desktop/DESKTOP_OPERATIONS.md)。文档分工与更新路由以 [文档索引](README.md) 为准。

编写 PR、Issue、Release 说明、公开评论及项目维护说明时，遵循根目录 [公开维护文本语言](../AGENTS.md#公开维护文本语言)。

## 实时核验原则

涉及 Git 或 GitHub 操作时，只读核验与本次操作相关的本地 HEAD、分支、工作树、远程提交、PR、Release、标签和资产。GitHub 状态必须重新查询，不能从文档推断；不得读取 Secret 值。身份、哈希、版本或文件集合不一致时停止。

## Desktop 候选传输与安全诊断

候选抓取由 `desktop/pipeline.py`、`desktop/arxiv_versions.py` 复用 `main.fetch_arxiv_metadata()` 的单次传输，读取首次／最新提交日搜索及当前最新版 Atom，并补查本地历史关联；不请求 OAI。查询、分页、超时、间隔与快照失效规则见 [Desktop 规范](desktop/DESKTOP_SPEC.md#候选抓取)。本节维护同次请求的安全诊断边界。

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

诊断只使用已接收的字节，不限制网络下载大小，不补打正文摘录。连接失败和超时额外记录已收字节数、正文未完成标记、curl 退出码以及 `curl`／`python_subprocess` 超时来源；同次请求已有固定 write-out 和白名单响应头也可保留，缺失不补打网络请求。沿用现有稳定错误，不把部分正文或 HTTP 200 响应头记为抓取成功。

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
| `request_kind`、`request_index`、`candidate_date`、`resumption_token_present` | 请求类型、序号、北京时间候选日期及是否为 token 分页，不保存 token 或完整 URL |
| `timeout_seconds`、`process_timeout_seconds`、`timeout_source`、`body_complete` | 网络期限、外层进程期限、超时来源及不完整正文标记 |

响应头白名单为 `date`、`server`、`via`、`age`、`content-type`、`content-length`、`retry-after`、`x-cache`、`x-cache-hits`、`x-served-by`、`x-timer`、`cf-ray`。每项最多保留一个 128 字节的可打印 ASCII 值；传输解析器的单元素列表在日志中规范为字符串。Cookie、未知头、控制字符和超限值不得进入日志。分类分页完成也记录分类、页码与本页数量。

临时响应头文件只存在于当前请求的 runtime 缓存目录，处理后清理；路径不输出，不进入 SQLite 或发行包。不启用 verbose，不记录 stderr、原始响应头、正文原文、Cookie、Secret、代理值或临时路径，不执行额外 DNS、HTTP 或网络探针。

取证准备、解析、日志或清理异常只能导致证据缺失，不能改变原请求返回值或错误码。诊断日志写入失败时降级到当前会话内存，不改变业务成败。日志格式、隐私边界和关联身份见 [两层诊断](desktop/DESKTOP_SPEC.md#两层诊断与失败范围)。

## 本地验证

使用当前已安装环境，不安装或升级依赖。按 [Desktop 离线验证](desktop/DESKTOP_OPERATIONS.md#离线验证) 使用受管入口，它为子进程设置独立 TEMP/TMP 和测试根目录：

```powershell
python -X utf8 -B scripts/run_local_checks.py
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

本地技术候选仅用于人工验收，验证要求见 [Desktop 运行手册](desktop/DESKTOP_OPERATIONS.md#portable-零模型验证)。技术候选交付即停止，不进行远程写入；正式发行须另获授权并从合并后的干净 main 重建，不得直接上传技术候选。当前 alpha 12 已按本节流程完成公开发行，身份与验证记录见 [CHANGELOG](CHANGELOG.md)。

当前 Desktop 仓库保存应用源码、测试、构建配置和项目文档；Windows portable ZIP 与 `.sha256` 通过本仓库 GitHub Releases 发布，不进入 Git 历史。

portable 根目录的 README、EULA、隐私与安全说明由 `docs/public_release/` 维护；该目录只保存文本。`RELEASE_CHECKLIST.md` 留在开发文档中，不进入 portable ZIP。第三方声明以 `packaging/windows/THIRD_PARTY_NOTICES.txt` 为唯一源文本。

自有应用采用 GPL-3.0-only，根 LICENSE 的标准全文直接进入 portable。通过 GPLv3 第 6(d) 节提供免费对应源码：portable README 与 Release 正文链接同一版本 tag 的源码归档，包含自有源码、资源、测试、构建脚本和说明；该 tag 提交与 BUILD_INFO.json 一致。实际分发库的固定源码随 ZIP 提供，包括 QtBase、QtSvg、PySide/Shiboken、pypdf、Fluent、frameless、darkdetect 和 pywin32；第三方原许可、独立工具/解释器与系统库的边界见 THIRD_PARTY_NOTICES.txt。

根 README 提供项目概览、源码启动和下载入口；打包 README 面向 portable 用户，分别维护。根 EULA、隐私、安全说明和第三方声明是对应维护源的同步副本；源与副本更新遵循 [文档路由](README.md#文档更新规则)，治理测试核对字节一致性。

本地构建只生成项目内被忽略的 `dist/`、`release/` 和 `.desktop-build/` 产物。`dist/` 保存未压缩发行目录，`release/` 保存当前构建或发行候选的 ZIP 和 `.sha256`。正式历史版本由 GitHub Releases 保存。构建器不自动创建仓库、标签或 Release，也不上传文件。每次公开发布必须基于与最新 `origin/main` 一致的干净提交重建 ZIP，按 [公开发布检查清单](public_release/RELEASE_CHECKLIST.md) 核对文件集、构建身份、用户数据排除、portable 验证和 SHA-256。

### 本地历史 portable 收口

每次公开发布任务必须包含本步骤及对应删除授权，范围分别列明旧 ZIP、校验文件和旧版本 `dist/` 成品目录；仅授权清理 `release/` 不包含 `dist/`。当次已明确授权时不重复确认。构建、验证和失败阶段不得清理旧 portable，构建器保留其现有材料留存行为。

正式新版本 Release 成功发布，并完成新资产、tag、BUILD_INFO、公开源码及远端资产核验后，再从未登录视角下载待清理版本的正式 ZIP 和 `.sha256`，核对资产名称、字节数、GitHub digest、实际 SHA-256、校验文件内容及 tag/BUILD_INFO。全部相关历史版本资产完整且与发布前基线一致后，本地 `release/` 只保留当前最新正式版本的 ZIP 和 `.sha256`；旧正式版本由 GitHub Releases 作为历史存档。

删除前列出准确文件名，解析绝对路径并拒绝目录链接或越界，逐文件删除，不使用通配符或递归删除。遇到非 portable 文件、未知版本或本地与正式发行物不同的候选成品时，保留并停止清理；只有当次明确确认的技术构建例外才可纳入删除，且不得声称与远端字节一致。任一测试、构建、发行身份、远端核验或历史资产完整性异常均停止后续发布或删除。

旧版本 `dist/` 在上述新旧远端核验全部通过后才纳入本步骤，不由构建器或临时产物轮换自动删除。逐项列出 `dist/arXivKaleid-<version>-windows-x64/` 的准确目录，确认是早于当前正式版本的已发布成品、不再用于构建、验证或待审，并确认所属进程、线程及数据库连接退出。先检查完整路径和文件名，含 `runtime/`、Secret、数据库、PDF 或日志的目录保留，不读取这些用户数据；未知版本、技术候选、链接、越界或归属不明均保留并停止清理。上述技术构建例外不适用于 `dist/`。

对符合范围的旧目录，将目录名、BUILD_INFO 的版本与提交和对应历史 tag 核对，再将完整文件集和逐文件 SHA-256 与已核验的对应远端正式 ZIP 比对；遗漏文件、额外文件或目录、哈希及身份不符均停止。授权清单保存准确绝对路径、完整文件与目录清单、逐文件哈希、对应资产摘要和保留理由，执行前重新核验，任何变化都停止。只逐文件删除清单中的文件，再由深到浅移除已核验的空目录；禁止删除 `dist/` 父目录或使用通配符、`Remove-Item -Recurse`。删除失败立即停止，保存已删除与残留清单，不自动重试。

收口后 `dist/` 只保留当前最新正式版本及仍有明确必要的成品，逐项记录额外保留目录的用途和待完成事项，后续发布再次复核；不得为了数量目标删除用户数据或待审材料。本规则不删除或修改 GitHub 历史 Release、tag、源码或资产，也不清理 `.desktop-build/`、源码材料、审计材料、运行数据；`preserved/previous-*`、vendor、许可输入与来源归档均不在本步骤范围。清理后核对并记录 `release/` 和 `dist/` 最终文件及目录列表。治理测试核对本步骤、发布清单及根协作规则的一致性，不代替实际远端核验和删除。

构建和发行扫描拒绝 `runtime/`、`logs/`、JSONL、SQLite、PDF、Secret 与本机路径；运行日志不得写入 `_internal/` 或用户目录。

本地 portable 技术验证可以基于 `codex/` 开发分支的干净冻结提交，其基线来自已核验的 `origin/main`，不要求两个提交相等。`BUILD_INFO.json` 记录分支、基线、冻结提交和技术验证用途；正式公开 Release 仍须在合并后的 main／对应 release tag 重新构建及最终核验。本地提交、构建与外部发布的授权分别处理。

每个新的 Desktop Release 正文必须包含 `## 本次更新`，用面向用户的 1～5 条简短要点说明该版本更新了什么。首个公开版本概括首次提供的主要功能；后续版本只概括相对于上一公开版本的主要新增、修改或修复，依据冻结构建提交、版本间 Git 差异和 `docs/CHANGELOG.md`，不得凭印象编写。平台、下载、SmartScreen、DeepSeek 数据发送与费用、SHA-256 等通用说明必须保留，但不能代替版本更新说明。发布 Draft 前检查该章节存在且非空；发布后重新读取公开 Release，核对正文、Tag、assets、digest 及其他必要说明。

Git commit/push、创建标签或 Release、上传成品都是受控操作，必须在当次获得授权。既有 immutable Release 的标签、ZIP 和校验文件不得修改，修正成品时发布新版本。Release 正文在发布前完成；只有用户明确授权的文案修正任务才可单独编辑已发布正文，并在操作前后对比 Tag 目标及全部 assets 的名称、字节数与 digest，确认发行身份未变。

构建和成品发布基于干净冻结提交；公开 Release 及未登录核验成功后，仍在同一发布任务中按 [文档路由](README.md#文档更新规则) 更新 `docs/CHANGELOG.md`。发布后文档收口不改变冻结成品或公开 Release，不拆成新的独立任务。尚未取得 Git commit/push 授权时，先完成本地文档修改、必要测试和 `git diff --check`，报告待提交状态，不得省略文档或宣称整个发布任务已经完成。

发布前后对比既有历史 tag、Release 正文和所有资产的 id、名称、字节数与 digest，任何变化停止。源码归档须从未登录视角验证文件集及字节与冻结提交对应；新增发行记录通过文档 PR 收口后，分别记录最终 main 与发行 tag SHA，仅允许文档收口及其必要治理测试导致两者不同。

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
- 单篇身份、重复、PDF、全文或 Token 问题按当前规范逐篇排除；无效可选评价只丢弃字段。系统身份、JSON 结构、费用、数据库或资源完整性错误整批停止。
- 失败时保留安全结构诊断、HTTP 状态、`finish_reason`、usage 和费用；不保存或打印模型原始响应、响应 ID、完整 prompt、全文或 Secret。

## 项目内产物

本地数据库、PDF、报告、日志、缓存和审计目录均属于运行产物，不得提交。用户电脑上的所有主动下载和临时审计内容只能写入项目内被忽略的明确目录；未经授权不得删除既有忽略目录或未跟踪内容。当前任务自产、已登记的临时工作文件按下节收尾，不扩大到历史内容。

唯一适用于正式发布任务的历史 portable 删除例外见上文“本地历史 portable 收口”；仍须满足当次授权和全部核验条件，不扩展到其他产物。

## 临时产物生命周期

根 `AGENTS.md` 定义授权和安全边界，本节定义生命周期，Desktop 手册维护实际命令。`scripts/local_artifacts.py` 使用标准库和进程文件锁统一管理 build、portable、visual、tests、release-check；锁占用即拒绝冲突运行，不建立系统配额或后台任务。所有路径逐项拒绝越界、符号链接和 reparse point。

每次运行登记独立 ID、提交、类别、状态、受保护输入和准确工作路径，记录位于 `.desktop-build/managed-artifacts/<运行ID>/run.json`。工作文件位于该运行的 `work/`，测试工作文件位于 `.codex-validation/managed-artifacts/<运行ID>/`。`evidence/` 保存摘要、文件清单、SHA-256、安全诊断和必要截图；`preserved/previous-*` 保存被替换旧成品及移动前哈希。旧成品即使移入本次目录也不取得删除资格；被忽略不等于可以删除。

今后构建可为 `BUILD_INFO.json` 用途明确为 `local-portable-technical-validation`、有对应完整 ZIP 和校验文件的旧 `previous-dist` 登记 `verified_candidate_copy_v1`。仅移动目录不授予资格；新登记还保存旧身份、完整文件清单及归档 SHA-256。新包 ZIP 完整性、逐文件哈希校验和完整 portable 验证全部通过后，验证记录绑定构建运行 ID、冻结 commit、ZIP SHA-256 和文件清单摘要；同一 commit 的不同包不能互用验收。验证入口正常收尾时再次校验新包并自动清理符合全部条件的旧技术副本，保留验证证据。人工验收不作为副本清理条件，仅用于决定是否进入第二阶段发布流程；用户确认后使用 `close-review --run-id <portable-ID>` 记录发布人工验收结案。结案不是 Git 推送或公开发布，也不替代相应授权。

清理在同一进程文件锁内进行，后续受管入口在容量检查前复核自动验证已通过但尚未清理的副本。删除前再次核验准确目录、完整文件集、旧身份、ZIP 逐文件哈希与校验文件，无 runtime、数据库、PDF、凭据、额外文件、链接、有效使用引用和未退出程序；敏感文件仅核验存在性，不读取内容。有效引用或运行程序仍在使用时保留并记录原因。完整性异常、删除失败或中断留下 `cleanup_failed`／`rotating` 停止入口，不自动重试或扩大范围。删除前登记状态，删除后记录文件数、释放容量、归档哈希和自动验证来源；对应 ZIP、SHA-256、验证证据和处置记录继续保留。

此例外使用 `local_artifacts_v2` 的可选字段，不迁移 v1 或未标记的历史 v2 记录。正式发行目录、当前 `dist/`／`release/` 输出、其他 `previous-*` 和保护材料继续保留，不按年龄或目录名称推断资格；归档 ZIP 仍占用容量，**4 GiB** 和 **512 MiB** 门槛不变。不建立后台清理，也不为治理自动重跑构建、网络验证或模型调用。

自动收尾仅处理本次自产、已登记的可丢弃工作文件，包括 stage、构建缓存、重复程序副本和合成测试数据。正式 `dist/`、`release/` 输出、用户 `.desktop-runtime/` 或已有 `runtime/`、历史审计、vendor 归档、稳定许可输入和 alpha 9 来源 ZIP 均受保护，不参与淘汰。退役的历史包装脚本和旧许可副本不再作为构建输入；历史材料经依赖复核后，仍须当次明确授权才能精确清理，不按名称或年龄自动删除。本节允许当前工作文件收尾；上文“本地历史 portable 收口”的不得清理要求只约束该节的旧发行物和历史材料，不授予其他目录的删除权限。

成功时先保存摘要、清单、哈希和必要截图，失败时先保存安全诊断，再在所属进程、线程及数据库连接退出后释放可重建部分。只记录失败类型，不记录可能含私人内容的异常全文。证据收集或清理失败单独报告，保留残留并标记 `cleanup_failed`，不覆盖原始错误、不记作成功。人工验收截图使用 `awaiting_review` 状态，保留到明确结案。

新增受管工作区默认上限 **4 GiB**，其中证据上限 **512 MiB**。入口与阶段间检查容量和磁盘可用空间；保护材料单独登记，不纳入清理计划，受管目录中的保护材料仍计入工作区容量。历史目录不自动纳管。容量超限即停止后续生成，不能以腾空间为由删除保护输入。阶段检查不是写入中的硬配额，单阶段可能暂时超过门槛，下一阶段必须停止并收尾或保留诊断。

普通证据自动保留每类最近两次成功证据和最近一次普通失败诊断；第三次成功或普通失败记录不阻止下一次明确启动，也不要求人工清理。成功且无需人工验收默认普通；离线测试仅将返回码 1 且带完整 unittest 失败汇总的结果标为普通失败，构建仅将 PyInstaller 正常返回码 1 标为普通失败。标记绑定实际异常对象；启动失败、崩溃、超时、中断、许可、身份、资源、ZIP 完整性与未知异常默认未结案故障保护，其他入口的失败保持保守分类。

记录使用 `local_artifacts_v2`，保存证据分类、稳定保护原因和完成时间，兼容读取 v1。v1 仅正常成功、工作目录已收尾、身份与布局合法、证据清单和哈希全部匹配且无待审或清理错误时取得普通轮换资格；v1 失败和归属不明记录继续保护，不批量迁移历史目录。在进程锁内先保存当前证据、确认资源退出并收尾工作文件，再按完成时间（合格 v1 使用创建时间）跨提交、跨版本轮换；删除前重新核验准确路径、链接、完整清单和哈希，先登记 `rotating`。无保护内容的旧运行目录一并移除；含 `previous-*` 的材料及记录继续保留。

未结案故障和待人工验收材料阻止同类入口；唯一待审入口例外是已绑定具体构建并通过全部自动校验的 portable 验证，保留发布人工验收状态及受保护证据，但不阻止后续技术验证。其他类别仍受总容量和全局未收尾记录约束。当前记录在工作清理期间保持 `running`，轮换及容量核验期间保持 `finishing`，全部完成才记录最终状态。轮换失败或强制中断遗留 `running`、`finishing`、`rotating` 或 `cleanup_failed` 时，下一入口停止新增大体积产物并输出计划入口，不自动删除残留、不覆盖原始错误；须核验所属进程已退出、明确处置范围后人工收口。历史及受保护旧证据不会自动轮换，删除仍须对应授权。容量门槛不变，不为轮换删除保护材料。

`python -X utf8 -B scripts/local_artifacts.py plan` 默认为只读清理计划，列出准确路径、逐文件哈希、计划摘要和保护路径。获准删除对应运行后才执行 `apply --run-id <ID> --plan-sha256 <摘要>`；人工验收结案另加 `--review-complete`，中断处置另加 `--processes-stopped` 并实际核验 PID 已退出。文件变化、未知条目、链接、越界或进程状态不明均拒绝删除。计划和执行参数不构成删除授权；不能用于清理未登记历史目录。

绑定新构建的 portable 发布人工验收必须使用 `close-review`，不能用删除证据的 `apply --review-complete` 代替；失败、不完整或证据变化的验证不能结案。完整验证证据属于必要清理审计，无论是否人工结案，计划均继续保护它；旧未绑定记录仍使用原有当次授权处置入口。

实际构建仍要求干净冻结提交，生命周期工具不放宽身份检查。失败不得借治理自动重跑网络或付费业务。只读发布核验入口见公开发布检查清单，公开服务访问的授权与模型调用分别处理。
