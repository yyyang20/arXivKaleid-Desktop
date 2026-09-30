# 运行与发布手册

本文档只描述当前有效的运行、测试、发布和恢复流程。业务筛选规则见 [当前业务规范](PROJECT_SPEC.md)。

## 实时核验原则

开始任何云端任务前，必须实时只读确认：

- 本地 HEAD、`origin/main`、当前分支和工作树。
- 是否存在运行中或排队中的 GitHub Actions。
- 开放 PR、目标 Issue、来源 Run、Batch、generation 和 artifact 身份。
- 与任务相关的仓库变量；不得读取 Secret 值。

任何数量、哈希、版本、父代、日期或状态不一致都应停止。文档不保存这些实时值。

## 正式自动化

正式入口是 `.github/workflows/automation-daily.yml`，使用同一并发锁串行维护滚动账本。

| 入口 | 用途 | 时间归属 |
|---|---|---|
| `schedule` | 每日正式自动运行 | 启动前最近的北京时间 18:03 计划槽位 |
| `full` | 自动模式的应急手动触发 | 必填计划日期的北京时间 18:03 |
| `manual_backfill` | 指定日期手动回查 | 指定日期北京时间 23:59:59 |
| `publish_recovery` | 发布已审计的终态报告 | 原样继承 generation 6 的时间身份 |

`schedule` 与 `full` 都属于自动模式；GitHub 延迟不会改变报告归属日期。`manual_backfill` 不显示调度延迟。付费执行和手动回查分别受显式输入及仓库开关约束。

arXiv 元数据传输使用系统 `curl --http1.1`、1000 条分页、90 秒单次超时和 5 秒请求间隔；不新增 Python 第三方依赖。仅 HTTP 429、HTTP 5xx 或超时可在 60 秒退避后重试一次；HTTP 406、curl 不可用、其他连接错误和其他 HTTP 状态均不重试，并在读取 Secret 和调用模型前停止。这不是批次重跑，也不改变付费模型零重试规则。

### arXiv 非 200 响应安全诊断

正式自动化在元数据准备步骤的现有日志中输出 `WARNING ARXIV_METADATA_DIAGNOSTIC=`，后接单行 JSON；每次收到可解析的非 200 HTTP 响应时记录一次，随后继续原有重试或停止流程。该日志由共享 curl 元数据入口生成，本地调用沿用其既有 logger，不新增日志文件或 artifact。

| 字段 | 含义 |
|---|---|
| `http_status`、`attempt`、`max_attempts` | HTTP 状态、本次尝试序号和最大尝试次数 |
| `body_bytes` | 已接收正文的总字节数，不含 curl 状态标记 |
| `sample_bytes`、`sample_truncated` | 前 8 KiB 样本的实际长度、正文是否超出该范围 |
| `sample_sha256` | 原始样本字节的 SHA-256，用于比较响应样本，不是完整正文哈希 |
| `markers` | 样本中的固定特征标签，无匹配时为空数组 |

特征按字节进行大小写不敏感匹配：`<html` → `html`、`<?xml` → `xml`、`not acceptable` → `not_acceptable`、`access denied` → `access_denied`、`request blocked` → `request_blocked`、`rate exceeded` → `rate_exceeded`、`captcha` → `captcha`。这些仅表示样本中出现对应字节，不能单独证明响应格式、防护拦截或 406 根因；样本范围之外的内容不参与分析。

新增诊断仅输出固定字段、数字、布尔值、哈希及上述标签，不输出正文摘录、原始响应、stderr、Cookie、凭据、请求头、环境变量或新增 URL 信息。诊断消息最多 1000 ASCII 字节，连同现有 `WARNING` 前缀和换行不超过 1 KiB。诊断生成或日志写入异常会被忽略，不输出异常详情，也不改变原 HTTP 错误码及重试规则。

此正文诊断只截取已经由原传输流程接收的字节，不限制网络下载大小。200 响应仍原样返回，连接、超时或状态解析错误仍沿用原处理，不生成上述 HTTP 响应诊断。它不能补回历史运行中未保存的响应内容。

#### 同次请求 transport 取证

正式 metadata curl 对每个可解析的 HTTP 响应（包括 200 和非 200）另输出 `WARNING ARXIV_METADATA_TRANSPORT_DIAGNOSTIC=` 单行 JSON。第一阶段 `ARXIV_METADATA_DIAGNOSTIC` 仍只用于非 200，其字段、前缀和 1 KiB 边界保持不变；transport 诊断是独立、最多 4 KiB 的附加证据。

| 字段 | 含义 |
|---|---|
| `http_status`、`attempt`、`max_attempts` | 该次 HTTP 响应的状态与尝试序号 |
| `write_out_capture` | 固定 curl write-out 是否通过封闭解析 |
| `remote_ip`、`remote_ip_version`、`remote_port` | 该次 curl 实际连接目标 |
| `http_version`、`ssl_verify_result`、`num_redirects` | HTTP 版本、TLS 校验结果与重定向数 |
| `timings_seconds` | 该次请求的 DNS、连接、TLS、首字节和总耗时 |
| `response_header_capture` | 响应头临时捕获状态 |
| `response_headers` | 仅通过白名单、长度和 ASCII 检查的最终 HTTP 响应头 |
| `response_headers_rejected` | 值不合规的白名单头名，不包含原值 |

响应头白名单为 `date`、`server`、`via`、`age`、`content-type`、`content-length`、`retry-after`、`x-cache`、`x-cache-hits`、`x-served-by`、`x-timer`、`cf-ray`。正式路径每项最多保留一个 128 字节的可打印 ASCII 值；Cookie、未知头、控制字符和超限值不得进入日志。

响应头只在每次 attempt 的项目内缓存目录临时文件中存在，路径不输出；处理后先截断再删除，不进入 artifact 或 SQLite。不启用 verbose，不记录 stderr、原始响应头、正文原文、Cookie、Secret、代理值或临时路径。不执行额外 DNS、HTTP、curl 版本查询或其他网络探针。

取证准备、文件、解析、日志或清理异常全部 fail-open：只能导致证据缺失，不得改变原请求的返回值、错误码或重试决策。406 仍不重试，429、5xx 和 timeout 仍只使用原有唯一次有界重试。200 与非 200 使用相同 transport schema；`WARNING` 是稳定的取证日志前缀，不表示 200 业务失败。

### 云端 arXiv 分页对照 probe

独立入口 `.github/workflows/arxiv-metadata-probe.yml` 只接受手动 `workflow_dispatch`，调用 `arxiv_metadata_probe.py`。正式 workflow、配置、分页策略和 metadata preview 均不改变。模型调用和模型费用为 0；运行仍消耗 GitHub Actions 时间。

运行前须另行获得提交、推送、workflow 接入及单次触发授权，锁定包含两阶段诊断代码且已通过完整离线测试的冻结提交。在 Actions 选择该提交对应的 ref，勾选 `confirm_probe`，填写完整 40 位 `expected_sha`。程序核对实际 HEAD、`GITHUB_SHA`、干净检出及正式传输配置；只允许私有仓库的 GitHub-hosted Ubuntu、Run attempt 1。拒绝 Actions 的 Re-run，不自动追加实验。直接在本地运行此脚本会在请求和写入前拒绝执行。

对照固定为 **1000 → 100 → 100 → 1000**：同一 job、runner、Python 进程串行执行，复用正式 `build_updated_query_url()` 和 `fetch_arxiv_metadata()`，只改变 `max_results`。分类固定 `gr-qc`，`start=0`，排序为 `lastUpdatedDate DESC`；curl、HTTP/1.1、请求头、90 秒超时、5 秒间隔、一次 60 秒退避重试保持正式设置。四个逻辑样本共用独立请求时间缓存，最多八次 HTTP attempt，不翻页、不补样本。406 后继续预定对照；其他错误完成共享函数原有重试后仍失败，或 200 不是有效 Atom feed，立即停止，剩余样本为 `not_executed`。

权限仅 `contents: read`、`actions: read`；标准 Actions token 仅供只读查询任务状态，不引用模型 Secret。使用独立并发组，不占用正式锁。运行前和每个样本前检查正式 workflow 是否排队、等待或运行中；状态未知或活跃则停止，不取消任务、不修改开关。此检查是瞬时快照，不能消除检查之后正式任务恰好启动的竞态，运行时应避开正式调度时段。

不进入 G1 prepare、模型、PDF、SQLite、artifact、滚动账本或 Issue 流程。仅在 runner 临时目录创建本次 Run 独占的请求缓存，并写安全 Job Summary；不保存响应正文、不上传 artifact。检出中的正式运行状态必须不存在，结束后再次核验状态和工作树。

Actions 日志中的 `PROBE_SAMPLE_BEGIN` 对应样本序号和分页值，`ARXIV_METADATA_DIAGNOSTIC` 原样复用第一阶段的安全字段。`PROBE_SAMPLE` 与最终 `PROBE_RESULT`/Job Summary 记录 UTC、耗时、每次 attempt 的状态、重试及诊断缺失标记；另记录提交 SHA、runner 镜像、Python/curl 版本。只接收预定义字段、固定标签和安全错误码，未知日志、正文、stderr、异常原文均不输出。200 仅在内存验证 Atom 并统计 entry 数，不输出论文内容。完整记录四个样本时退出成功（包括四次 406），提前停止则退出失败并标记“实验不完整”；平台取消或超时可能只有已输出的样本日志，不能当作完整实验。

| 结果 | 解释与下一步 |
|---|---|
| 两次 1000 为 406，两次 100 为有效 200，且无重试 | 支持分页值或对应查询缓存与故障相关；另行设计更细对照，不直接改正式分页 |
| 两种分页均为 406 | 缩小分页未解决本次拒绝；比较安全标签与样本哈希 |
| 四次均为有效 200，且无重试 | 本次未复现，不能否定历史失败或宣称修复 |
| 同一分页结果不同，或发生重试、429、5xx、超时 | 时间、服务或网络波动干扰；结果不足以归因，保留证据，不自动重跑 |

诊断缺失必须保留为缺失，不用正文补打；哈希和标签不能单独确认拦截原因。同一 runner 仍不能冻结服务端缓存、负载和网络路由，此实验只提供相关性证据。

### arXiv 传输环境对照 probe

第三阶段独立入口 `.github/workflows/arxiv-transport-probe.yml` 只接受手动 `workflow_dispatch`，调用 `arxiv_transport_probe.py`。它不复用或修改 `main.fetch_arxiv_metadata()`，不改变正式 workflow、curl 参数、候选策略、分页、重试或状态；模型调用和模型费用为 0，实际运行仍消耗 GitHub Actions 时间。

初始对照在本地 Windows 与 GitHub-hosted `ubuntu-24.04` 上运行同一冻结代码和同一 `submitted_date`，每个环境固定串行两次且不重试：第一条必须是正式 G1 的 `gr-qc / lastUpdatedDate DESC / start=0 / max_results=1000` 完整 URL，作为首要判别证据；第二条是 `gr-qc / submittedDate / start=0 / max_results=1000`，只用于 Desktop 查询形状交叉对照。两条都使用正式 HTTP/1.1、User-Agent、Accept、90 秒超时和至少 5 秒间隔；诊断专用 curl 参数只负责把正文、响应头和固定 write-out 字段送入临时文件或内存，不改变请求头与 URL。

本地运行必须从干净冻结提交显式设置 `ARXIV_TRANSPORT_CONFIRM=true` 和 `ARXIV_TRANSPORT_SUBMITTED_DATE=YYYY-MM-DD`，临时文件只进入项目内被忽略的 `.codex-validation/` 并在进程结束时删除。云端运行还须另行获得提交、推送、workflow 接入和单次触发授权；选择相同冻结 ref，勾选 `confirm_probe`，填写完整 `expected_sha` 及与本地相同的 `submitted_date`。云端只允许私有仓库、GitHub-hosted Linux、Run attempt 1，拒绝 Actions Re-run 和脏检出。

probe 独立采集并封闭输出以下证据：Python、OS、runner image、curl/libcurl/TLS backend 与安全 feature；代理变量是否存在但不输出值；默认 curl 配置只检查是否存在而不读取内容；系统解析得到的 A/AAAA；curl 实际目标 IP、IP 族、端口、HTTP 版本、TLS 校验、协商出的 TLS/ALPN、重定向数和 DNS/连接/TLS/首字节/总耗时；以及 `date`、`server`、`via`、`age`、`content-type`、`content-length`、`retry-after`、`x-cache`、`x-cache-hits`、`x-served-by`、`x-timer`、`cf-ray` 响应头白名单。还只按布尔值确认实际发出的 Host、User-Agent 和 Accept 是否与预期一致。

正文只记录总字节数和完整 SHA-256；HTTP 200 在内存验证 Atom 并统计 entry，不输出论文内容。禁止输出原始 verbose/trace、stderr、Cookie、未知响应头、代理值、curl 配置内容、环境变量值、凭据或任意异常原文。字段不符合类型、长度、字符和范围约束时只记固定缺失或拒绝状态。初始 workflow 不执行 UA/Accept 改写、强制 IPv4/IPv6、备用 HTTP 客户端或外部出口 IP 查询；只有 2×2 结果不足以判断时，才另行设计并授权单变量追加对照。

权限仅 `contents: read`、`actions: read`；标准 Actions token 只用于查询正式 workflow 活跃状态。使用独立并发组，在首次请求前、每条样本前和结束后检查正式任务；状态未知或活跃立即停止。运行不读取 Secret，不进入 G1 prepare、模型、PDF、SQLite、artifact、滚动账本或 Issue，不上传 artifact，仅使用 runner 临时目录并写安全 Job Summary。完整两条样本才算实验完成；HTTP 406 仍是完整观测，平台取消、连接失败或中途安全停止均不得当作完整实验。

### arXiv API 系统级限流

`export.arxiv.org` 可能在客户端遵守请求间隔时仍返回 HTTP 429。arXiv API 团队说明，响应正文为 `Rate exceeded` 的 429 可能来自服务端容量和全部用户流量形成的系统级限流，不等同于单个客户端请求过量；状态页显示 `export.arxiv.org` 在线也只表示服务整体可用，不能证明每次 API 请求都会成功。核验时应同时参考 [arXiv API 使用条款](https://info.arxiv.org/help/api/tou.html)、[arXiv 系统状态](https://status.arxiv.org/)、[服务器容量说明](https://groups.google.com/a/arxiv.org/g/api/c/pNB3lnxf4mQ)和[系统级限流说明](https://groups.google.com/a/arxiv.org/g/api/c/ycq8giRdZsQ)。

出现持续 429 时，先核对请求仍为单连接、间隔不少于 3 秒、URL 和分页参数未发生计划外变化，并确认失败发生在读取 Secret 和模型调用之前。满足这些条件时，将其作为外部元数据服务故障处理：保留现有一次有界重试，不增加并发或频繁人工重跑，不轮换机器、代理或 IP 规避限制，也不把普通网页可访问视为 API 已恢复。

用户明确选择“优先恢复正常服务”时，可以保持定时入口启用，让每日计划任务按现有失败关闭边界进行一次正式尝试，并在等待期间关闭手动回查与恢复开关。失败的元数据尝试不产生模型费用、不发布日报，也不推进滚动账本。首次恢复成功的自动任务按其当天计划槽位生成日报，并继续处理滚动队列；它不会重建故障期间每个日期各自的正式 `full` 日报。缺失日期以后只能按明确授权执行 `manual_backfill` 人工回查，必须保留手动回查身份，不得表述为原日期的正式自动日报。

如果目标是保留故障期间每一天各自的正式自动模式日报，则不得采用上述服务优先策略；应关闭定时入口，待 API 恢复后依照连续日期规则逐日执行 `full`，全部成功后再恢复定时。

正式 generation 含义：

| generation | 状态 |
|---:|---|
| 2 | 候选与元数据固定 |
| 3 | Round 1 预检和费用预留通过 |
| 4 | Round 1 与调用审计完成 |
| 5 | PDF、全文和页数门控完成 |
| 6 | Round 2 与日报就绪，或形成安全失败终态 |
| 7 | Issue 已发布并写入最终滚动账本 |

## 受控重做与恢复

- `.github/workflows/controlled-replay.yml` 默认只按准确来源 Run、Batch 和有效 generation 7 恢复历史候选；支持 `round1`、`round2`、`round1_round2` 和 `withdrawal_pdf`。唯一例外是 `round1_round2` 可显式锁定未发布 generation 6，同时必须提供紧邻上一 Batch 的准确最新 generation 7 错误样本账本。
- generation 6 受控重做只把最新 generation 7 中缺少的 `screening_error_samples` 合并到隔离数据库副本；既有记录不一致、Batch 不相邻或候选谱系不同立即停止，来源 artifact 保持不变。
- 完整两轮受控重做在 Round 1 验证成功后，只在隔离副本中把当前候选及当前 `round1_v20`、`round2_v15`、`profile_v2` 兼容身份和策略对应的 `screening_completion` 重新绑定到新 Run；历史 Profile 内容仅用于定位和审计来源，不注入新请求。必须核对完成总数和入选数后才能进入 Round 2，历史来源账本不得改写。
- `.github/workflows/controlled-replay-publish.yml` 只发布通过审计的完整两轮受控重做结果，不调用模型；结果允许 Top 0 至 5，可更新原 Issue，也可在严格确认当日日报 Issue 不存在后创建一次 Issue。
- `.github/workflows/automation-round2-recovery.yml` 只恢复符合固定条件的 Round 2 `empty_content` 失败。它可严格读取未发布 G6、已发布失败 G7，或自动保留的已发布失败来源副本；每次恢复都必须锁定来源 Run、尝试次数、Batch、artifact、全文、候选哈希、失败 Issue 和费用，并在新的明确授权下最多增加一次 Round 2。
- 当前正常运行统一使用 `round1_v20 + round2_v15`，不另行注入 Profile 内容。仍符合现行规则的 v21 旧 Round 2 recovery 必须精确保持 `round2_v14 + profile_v2`：固定核验旧 v14 Prompt、Profile JSON、候选顺序、全文、工具 schema、原请求哈希、来源策略、费用和恢复次数，并继续使用原请求身份，不迁移为 v15、不改写为 v22。任一字段不一致立即停止；使用 v20/v15 重新筛选历史候选只能走 `controlled replay`。
- 恢复成功后，workflow 只在零模型审计通过时，将精确来源的失败 Issue 更新为正常日报并写入新的正常 G7；不得创建同日报第二个 Issue，也不得删除失败 Issue。恢复再次得到失败、空内容、无效 JSON、费用超限或任一身份不一致时，保留现场并停止，不自动重试。
- `.github/workflows/automation-round2-recovery-publish.yml` 只处理“恢复模型调用成功、发布步骤失败”的现存正常 G6。它必须锁定原失败 Run、恢复 Run及其三段 job 结论、Batch、日期、Issue 和 artifact 集合；仅允许当前或唯一批准前任 G6，并只为 Round 2 恢复发布审计接受代码固定的历史源策略哈希。该入口不读取 Secret、不调用模型，只更新原失败 Issue并写入新 G7。
- 普通滚动恢复仅为已经审计发布的 B28/G7 接受一次代码固定的完整历史身份，联合锁定策略哈希、Batch 身份、发布 Run、Issue、generation 和状态；任一字段变化即拒绝。下一批写入当前 v22 策略后，不再依赖该兼容入口。
- `.github/workflows/daily.yml` 只发布指定且已审计的 generation 6，不重新筛选或调用模型。
- 已发布报告修复入口只允许处理其代码中固定的展示或时间归属问题；必须保持筛选结果、错误样本、全文数据库和最新账本前向一致。
- 历史展示问题的原 generation 7 已按保留策略清理时，以当前 Issue 正文及其显式 SHA-256 为唯一报告源，同时用最新 generation 7 核验目标 Batch、Issue 映射和候选身份。修复只能改变 Issue 正文的目标 Markdown 字符，并在最新滚动账本追加审计记录；不得重建历史日报、修改最新日报正文或调用模型。
- 展示修复必须先通过 GitHub Markdown 预览，再更新原 Issue 正文；修复后重新读取 GitHub HTML，确认目标摘要不存在错误的 `<code>` 或 `<del>` 标签。

来源 artifact 缺失、过期、损坏或身份不一致时不得猜测替代来源，也不得退回到按日期自动选择。

连续日期恢复必须先发布前一日 generation 7，再用该新账本触发后一日 `full`；不得用后一日失败 generation 6 中重复的候选，也不得用 `manual_backfill` 替代自动模式的计划日期。

## 错误样本记录

`.github/workflows/screening-error-samples.yml` 是独立零模型入口：

- 只接受用户授权并确认的漏选、误选和错标。
- 恢复最新有效 generation 7，事务写入后生成新的 generation 7。
- 与正式自动化共用并发锁。
- 不修改日报、Issue、提示词、定时或运行开关，不读取模型 Secret。
- 完全相同记录幂等成功；冲突或分类不一致整批失败。

## 本地验证

使用当前已安装环境，不安装或升级依赖：

```powershell
python -B -m unittest discover -s tests -v
git diff --check
git status --short
```

重要代码或治理变更还应检查：

- 所有 Git 跟踪 Python 文件可通过 AST 解析。
- 所有 workflow YAML 可解析，Action 版本和最小权限保持受控。
- 策略文件和其保护文件的 SHA-256 一致。
- 测试、报告、数据库、日志、PDF 和 Secret 没有进入 Git。

## DeepSeek Responses兼容性诊断

`.github/workflows/deepseek-responses-probe.yml` 只用于区分基础 Responses调用契约与正式长输入问题：

- 必须由 `workflow_dispatch` 显式确认，并且手动开关为 `true`、定时开关为 `false`、Run attempt为1。
- 在读取 Secret 前运行完整离线测试，并用峰值价格确认固定短请求的最坏费用不超过¥0.01。
- 复用正式 Round 2 模型、思考强度、函数 schema和自动工具选择，不读取历史全文或数据库，最大输出512 Token且只允许一次 HTTP请求。
- HTTP 400/422 最多读取8 KiB错误 JSON，只保留经过长度限制及凭据脱敏的 `code`、`type`、`param` 和短消息；不保存原始错误体、请求、模型正文、响应 ID、提示词或 Secret。
- 诊断 artifact只含请求身份、状态码、脱敏错误、usage和费用。只有 HTTP 200 且实际返回唯一目标函数调用才算成功；HTTP 200普通文本、空内容、错误函数及所有非200状态均失败关闭。
- 探针不发布日报、不修改 Issue或自动化账本；开关恢复方式由对应获批运行计划决定。

## Desktop 公开发布

私人仓库 `yyyang20/arXivKaleid` 是应用源码、测试、构建配置和项目文档的唯一开发源；公开仓库 [yyyang20/arXivKaleid-Desktop](https://github.com/yyyang20/arXivKaleid-Desktop) 只用于同步明确允许的公开文档，并通过 GitHub Releases 提供 Windows portable 成品，不作为第二开发源。

Windows portable 的公开 README、EULA、隐私、安全说明和发布清单维护在 `docs/public_release/`；该目录只允许文本，不保存 ZIP、SHA-256 或运行数据。第三方声明继续以 `packaging/windows/THIRD_PARTY_NOTICES.txt` 为唯一源文本。公开仓库只通过 Git commit/push 同步发布清单允许的公开文档，其中 `RELEASE_CHECKLIST.md` 仅供私人开发仓库核对，不进入公开仓库或 portable ZIP。

Desktop 诊断日志在源码 `.desktop-runtime/logs/` 或 portable `runtime/logs/`，不得写入 `_internal` 或用户目录。构建和发行扫描必须拒绝 `runtime/`、`logs/`、JSONL、SQLite、PDF、Secret 与本机路径；日志创建或写入失败只允许降级为当前会话内存诊断，不能改变分析成败。

本地构建继续只生成项目内被忽略的 `dist/`、`release/` 和 `.desktop-build/` 产物：`dist/` 保存未压缩发行目录，`release/` 只保留当前构建或发行候选的 ZIP 和 `.sha256`，两者都不进入 Git 历史。正式历史版本由 GitHub Releases 保存，ZIP 和 `.sha256` 只作为 GitHub Release assets 发布。构建器不自动创建公开仓库、标签或 Release，也不上传文件。每次公开发布必须基于与最新 `origin/main` 一致的干净提交重建 ZIP，按 [公开发布检查清单](public_release/RELEASE_CHECKLIST.md) 核对文件集、构建身份、用户数据排除、portable 验证和 SHA-256。

每个新的 Desktop Release 正文必须包含 `## 本次更新`，用面向用户的 1～5 条简短要点说明该版本更新了什么。首个公开版本概括首次提供的主要功能；后续版本只概括相对于上一公开版本的主要新增、修改或修复，必须依据冻结构建提交、版本间 Git 差异和 `docs/CHANGELOG.md`，不得凭印象编写。平台、下载、SmartScreen、DeepSeek 数据发送与费用、SHA-256 等通用说明必须保留，但不能代替版本更新说明。发布 Draft 前必须检查该章节存在且非空；发布后必须重新读取公开 Release，核对正文与 Tag、assets、digest 及其他必要说明。

同步公开文档、创建标签或 Release、上传成品都是独立 GitHub 写操作，必须在当次获得授权。本地不长期保留第二份公开仓库检出；既有 immutable Release 的标签、ZIP 和校验文件不得修改，需要修正成品时发布新版本。Release 正文应在发布前完成；只有在用户明确授权的文案修正任务中，才可以单独编辑已发布的正文，且必须在操作前后对比 Tag 目标和全部 assets 的名称、字节数与 digest，确认发行身份未改变。

公开发布分为前后两个连续阶段：构建和成品发布必须基于发布前与最新 `origin/main` 一致的干净提交；公开 Release 及未登录核验成功后，仍须在同一公开发布任务中按 [文档路由](README.md#文档更新规则) 更新私人仓库 `docs/CHANGELOG.md`。发布后文档收口不得改变已经冻结的成品或公开 Release，也不得拆成新的独立任务。公开仓库写操作授权不自动覆盖私人仓库的 Git commit/push；尚未取得后者授权时，先完成本地文档修改、必要测试和 `git diff --check`，明确报告待提交状态，不得因此省略文档或宣称整个公开发布任务已经完成。

## 功能开发与上线

1. 从最新 `origin/main` 创建 `codex/` 前缀分支。
2. 完成最小范围实现、测试和文档同步。
3. 执行完整本地离线验证和 `git diff --check`。
4. 用户授权 GitHub 写操作后，推送冻结提交并创建 Draft PR。
5. 运行获准的零费用 Ubuntu 离线验证和 cloud dry run；确认不读取模型 Secret、不调用模型。
6. 审查最终差异、测试和云端结果，再将 PR 标记 ready 并按项目约定合并。
7. 合并后核验 `main`、工作树和相关实时云端状态；只有用户明确要求时才修改仓库开关或执行付费验证。

文档或测试专用修改若不改变正式运行行为，不需要关闭自动定时；若合并时正式任务正在运行，应等待其完成后再合并，不主动改变开关。

## 付费与失败规则

- 未获得用户当次明确授权时，只允许零模型、零费用检查。
- 授权必须明确具体入口、次数和费用上限；每个逻辑模型调用最多一个 HTTP attempt，失败后不自动重试或重跑。
- 费用预检使用配置中的峰值快照；实际费用按每次调用前的北京时间价格档位记录。单批预期费用和工作流绝对上限均由配置固定。
- 单篇身份、标签、理由、PDF、全文或 Token 问题按当前规范逐篇排除；系统身份、JSON结构、费用、数据库、策略哈希、artifact 或发布完整性错误整批停止。
- 失败时保留安全结构诊断、HTTP 状态、`finish_reason`、usage 和费用；日报不保存或打印模型原始响应、响应 ID、完整 prompt、全文或 Secret。

## 项目内产物

本地数据库、PDF、报告、日志、缓存和审计目录均属于运行产物，不得提交。用户电脑上的所有主动下载和临时审计内容只能写入项目内被忽略的明确目录；不得删除既有忽略目录或未跟踪内容。
