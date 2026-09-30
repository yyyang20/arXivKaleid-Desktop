# 功能变更记录

## 2026-09-30：Desktop 两层错误诊断

- Desktop GUI 新增阶段、类别级稳定错误代码、影响范围、操作建议及 session/operation/snapshot/fetch/run 关联，正常无候选、零入围、长论文/Token 排除、无合格全文和零推荐继续作为业务 outcome。
- 新增源码 `.desktop-runtime/logs/` 与 portable `runtime/logs/` 脱敏 JSONL，记录阶段生命周期、耗时、计数和既有安全结构诊断；写盘失败降级到 512 条内存 ring，不改变原业务结果。
- PDF/全文失败改按实际影响范围判定：已确认单篇且共享状态完整时继续，SQLite、共享组件、runtime 存储或无法确认范围时整批失败关闭；未枚举异常不自动升级。
- 日报已生成而 GUI Markdown 渲染失败时保持分析成功；发行扫描新增 JSONL 排除，版本升级为 `0.1.0-alpha.4`，筛选、模型次数、自动重试和费用规则不变。

## 2026-09-29：Desktop Release 版本更新说明治理

- 明确每个新的 Desktop GitHub Release 都必须包含面向用户的非空“本次更新”：首个公开版本概括首次能力，后续版本依据冻结提交、Git 差异和变更记录概括相对上版的 1～5 条主要变化。
- 公开发布检查清单新增发布前非空检查和发布后公开回读；平台、下载、SmartScreen、DeepSeek、费用和 SHA-256 等通用说明不能代替版本更新说明。
- 依据三个实际公开 ZIP 的构建提交与版本间差异，补齐 alpha.1 首发能力、alpha.2 运行进度反馈和 alpha.3 自包含 Prompt 收敛的公开版本更新说明；Tag 和 Release assets 保持不变。

## 2026-09-28：arXiv 正式 metadata 成功/失败对称取证

- 正式候选收集对每个可解析 HTTP 响应输出同一 transport schema，使 200 与非 200 可直接比较实际目标 IP、HTTP/TLS timing 和白名单 CDN/cache 响应头。
- 只复用同次 curl 已有的 write-out 和临时响应头；不新增网络请求，不改变 URL、分页、UA/Accept、HTTP/1.1、超时、间隔、重试、Desktop 或独立 probe 的成功日志行为。

## 2026-09-28：Windows Desktop alpha.3 公开发行

- [`v0.1.0-alpha.3`](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.3) 已作为 immutable prerelease 发布到公开仓库，成品从私人仓库冻结提交 `73acce981bbfac1e15b68e77981aa4b3b2f59831` 重新构建。
- 公开资产仅包含 `arXivKaleid-0.1.0-alpha.3-windows-x64.zip` 与对应 `.sha256`；ZIP SHA-256 为 `d5a3c78c2112b01ae1fe12172aa28655ee73da264878db0c930237eddac480b0`。
- 完整离线测试、Desktop portable 零模型自检、资源与隐私扫描、GitHub asset digest 及未登录下载核验均通过；公开隐私说明已同步，既有 alpha.1 和 alpha.2 标签、Release、资产及说明未修改。

## 2026-09-28：两份自包含 Prompt 收敛

- 新正常运行统一使用自包含的 `round1_v20 + round2_v15`，两份 Prompt 吸收相同的普适研究边界，不改变四类标签、Top 10/Top 5、顺序、不补位、模型、费用、PDF 门控、日报模板或 SQLite schema v5；请求不再注入独立 Research Profile 内容，仅保留 `profile_v2` 兼容身份。
- 自动化策略升级到 `automation_policy_v22`，Desktop、正式自动化、云端预检和受控重做共用现行消息构造；portable 只打包配置、策略和两份现行 Prompt，旧 Prompt 与 `profile_v1/profile_v2` 资源继续只读保留。
- 符合规则的旧 Round 2 recovery 及其未计费 HTTP 400 传输恢复精确保留 `round2_v14 + profile_v2` Prompt、Profile、请求哈希和来源策略语义，不自动迁移；使用 v20/v15 重新筛选历史候选仅走 `controlled replay`，并使用独立的新缓存、调用账本和结果身份。
- Desktop 源码版本升级为 `0.1.0-alpha.3`，用于承载本次 Prompt/Profile 收敛；公开发行事实将在成品发布并完成下载核验后另行记录。

## 2026-09-27：公开发布后私人仓库文档收口

- 明确 Windows portable 必须先从干净 `main` 冻结构建并完成公开发布，随后仍在同一任务中按文档路由更新私人仓库 `docs/CHANGELOG.md`；发布后文档收口不改变已经冻结的成品或 immutable Release。
- 公开仓库写操作授权不自动覆盖私人仓库的 Git commit/push；缺少后者授权时，先完成本地文档修改和验证，明确报告待提交状态，不得省略文档或提前宣称整个发布任务完成。
- 公开发布检查清单补充发布后收口步骤，并由治理测试锁定该顺序、授权边界和完成条件；现有干净提交构建、资产校验、失败停止及 immutable Release 规则保持不变。

## 2026-09-27：Windows Desktop alpha.2 公开发行

- `v0.1.0-alpha.2` 已作为 immutable prerelease 发布到公开下载仓库 [`yyyang20/arXivKaleid-Desktop`](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.2)。
- Release 只包含版本化 Windows x64 portable ZIP 和对应 `.sha256`；成品从已验收的干净 `main` 重建，通过完整离线测试、portable 零模型验证、本地与 GitHub asset digest 及未登录下载核验。
- 公开 allowlist 文档与维护源已经一致，因此没有创建无意义的公开文档提交；既有 `v0.1.0-alpha.1` immutable Release 保持不变，本次 DeepSeek 调用和模型费用均为 0。

## 2026-09-27：Desktop 真实运行进度反馈

- 候选抓取增加 indeterminate 进度，显示真实 UTC 检查日期、动态分类 N/M 和当前日期有效条目数；日期切换时重新计数。
- 两轮分析增加 Round 1、PDF、全文、Round 2 和日报步骤状态；PDF 与全文 X/Y 来自真实任务结果，合法零结果显示跳过，失败停留在实际阶段。
- 共享 PDF 与全文处理只增加默认关闭的可选观察回调；筛选、门控、网络请求、模型调用、重试、SQLite 和正式自动化行为不变。

## 2026-09-27：arXiv 正式 metadata 第四阶段现场取证

- 正式 metadata 非 200 路径从同一次 curl 请求附加采集实际目标 IP、HTTP 版本、SSL 校验、固定 timing 和白名单响应头；不启用 verbose，不新增网络请求。
- 取证使用独立有界日志和项目内短期响应头文件，未知头、Cookie、stderr、代理值和原始内容均不输出；准备、解析、日志和清理异常全部 fail-open。
- 抽取第三阶段已验证的 write-out 与响应头解析为共享纯函数；正式 URL、分页、UA/Accept、HTTP/1.1、超时、间隔、重试和 200 路径行为不变。

## 2026-09-27：arXiv 传输环境第三阶段诊断

- 新增本地 Windows 与 GitHub-hosted Ubuntu 24 共用的独立 transport probe；每个环境固定先请求完整正式 G1 URL，再请求同分页的 Desktop `submittedDate` URL，形成最小 2×2 对照且不自动重试。
- 独立采集 curl/TLS、DNS、实际目标 IP、HTTP/TLS timing、实际请求头匹配和白名单响应头；正文只保留长度、哈希及 Atom 计数，不修改正式 `fetch_arxiv_metadata()` 或自动化行为。
- 新增冻结提交、显式授权、正式任务活跃检查、临时目录和只读 workflow 边界及全模拟安全测试；模型调用为 0，尚未提交、推送或触发云端实验。

## 2026-09-27：云端 arXiv 分页对照诊断

- 新增独立手动 Ubuntu probe，固定 1000 → 100 → 100 → 1000，复用正式 URL 和 curl metadata 传输；记录每次 attempt 的安全诊断、重试、Atom 有效性和环境身份，不改变正式分页或业务逻辑。
- 冻结提交及首次 Run 校验、正式任务活跃检查、只读权限和临时目录隔离；不读模型 Secret、不调用模型、不恢复或写入正式状态、不上传 artifact。结果仅进入日志和 Job Summary，未触发云端实验。
- 诊断背景为连续三天 G1 首条 `gr-qc / start=0 / max_results=1000` 返回 406：[9 月 24 日](https://github.com/yyyang20/arXivKaleid/actions/runs/36015269274)、[9 月 25 日](https://github.com/yyyang20/arXivKaleid/actions/runs/36151801386)、[9 月 26 日](https://github.com/yyyang20/arXivKaleid/actions/runs/36247925955)。第三天后续阶段全部跳过；这只补充历史失败证据，不证明分页因果，也不构成提前调整正式参数的依据。
- 补充全模拟实验与 workflow 安全约束测试，同步运行手册和文件职责；模型费用为 0，实际运行仍消耗 Actions 时间。

## 2026-09-26：arXiv 非 200 响应安全诊断

- 共享 curl 元数据入口在非 200 响应时追加有界结构化日志，只记录状态、尝试次数、正文长度、前 8 KiB 样本哈希和固定特征标签，不输出响应原文或 stderr。
- 诊断异常与原 HTTP 处理隔离；请求参数、1000 条分页、超时、间隔、重试、错误码及业务身份保持不变，不新增网络请求、文件或 artifact。
- 补充模拟响应、安全边界及请求行为回归测试；诊断只提供后续定位线索，不代表 406 根因已确认或修复。

## 2026-09-26：Windows Desktop 首次公开发行与文档治理收口

- `v0.1.0-alpha.1` 已作为 prerelease 发布到公开下载仓库 `yyyang20/arXivKaleid-Desktop`，既有 immutable Release 资产保持不变。
- 私人仓库继续作为唯一开发源，公开仓库只同步允许的下载文档；本地 `dist/`、`release/` 与 GitHub Releases 的职责边界已写入现行文档。
- 公开 README 补充 vibe coding 开发方式和人工/Codex 职责，治理矩阵补齐 Desktop 版本、构建基线、公开仓库身份和实际 Release 发布路由；本次不改变 Desktop 功能、业务规则、prompt、profile 或构建产物。

## 2026-09-26：Windows Desktop 公开发布准备

- 新增公开下载仓库所需的 README、EULA、隐私、安全说明和发布检查清单，文档仅在私有开发仓库维护，ZIP 继续留在被忽略的 `release/`。
- Desktop 每次启动后首次分析前显示 DeepSeek 数据发送、费用、DPAPI 凭据与本地数据告知；默认拒绝，拒绝时不消费快照或调用模型，冻结 EXE 的零模型诊断直接覆盖该拒绝路径。
- portable 构建按 allowlist 将应用 README、EULA、隐私和安全说明放入 ZIP 根目录，继续保留现有第三方声明、许可和对应源码归档；筛选、prompt、profile、PDF 门控和费用规则不变。

## 2026-09-25：Windows Desktop portable 打包

- 新增 Windows x64 PyInstaller one-folder 构建入口，固定构建依赖与官方 curl 校验清单，生成本地 portable ZIP、SHA-256 和提交身份。
- 分离只读资源与可写 runtime，保留源码 `.desktop-runtime/` 和共享 CLI 行为；补充冻结模式路径、工作库、两轮 mock 集成、curl 和发行扫描测试。
- 随包提供 IANA 时区、第三方声明、许可证和 Qt 对应源码；增加全新副本的 GUI、DPAPI 假值、依赖、重启及可选真实 arXiv 零模型验证。

## 2026-09-25：Windows Desktop 第二阶段

- 分析按钮直接消费一次冻结候选快照，复用 Round 1、PDF、60 页全文门控、Round 2 和 usage 审计；Key 显式注入，不额外 self check，每轮最多一次 HTTP attempt，累计预算上限 ¥3.00。
- 当前工作 SQLite 与 PDF 隔离在 `.desktop-runtime/`，只重置已知工作库及 sidecar；文件锁保护并发，失败不重试、不补位、不查询历史候选。
- 新增薄 Desktop Markdown 外壳与 QTextBrowser 渲染，后台阶段信号、消费后按钮状态及对应离线集成测试；共享 Round 2 默认调用行为保持不变。

## 2026-09-25：Windows Desktop 第一阶段

- 新增 Desktop `0.1.0-alpha.1` PySide6 GUI，复用 submittedDate 核心抓取最近非空日期，显示批内去重统计并冻结最多 100 篇内存候选。
- API Key 使用当前 Windows 用户 DPAPI 自动加密保存和恢复；Secret 与请求间隔缓存隔离在被忽略的 `.desktop-runtime/`。
- 新增后台线程及失败失效边界测试、Desktop 文档与独立固定依赖；分析按钮仅显示阶段说明，不连接模型、PDF、SQLite 或日报。
- 旧探针测试改用隔离项目夹具，允许完整测试的临时目录限制在真实仓库内，同时保留项目内 Secret 拒绝检查。

## 2026-09-25：清理未使用的目录脚手架

- 移除本地入口创建但从未被读取的 `input_code/`、`input_papers/`，并清理未使用的 `paths.extracted_dir` 配置、校验和测试夹具残留。
- 项目结构补充 `.vscode/settings.json` 的职责与可移植边界；筛选、PDF、日报、数据库、workflow 和自动化策略不变。

## 2026-09-25：仓库卫生与本地产物清理

- 补充根级 `.codex-audit-*/` 和 `.pytest_cache/` 忽略规则，并同步运行产物文档。
- 清理已确认的 Python 字节码、pytest 缓存、旧审计目录和空临时目录；业务规范、筛选逻辑、workflow 和运行数据不变。

## 2026-09-25：项目专用 Conda 环境授权例外

- 电脑安全边界新增唯一、受控的项目专用 Conda 环境例外：仅在用户明确授权后，允许维护指定环境及当前任务明确需要的 Python 依赖；`base`、其他环境、系统 Python、PATH、Windows 设置、全局软件和其他项目继续禁止修改。
- 治理测试同步核验默认项目外只读、授权范围、依赖范围、越界重新授权及绝对禁止项，其他项目安全边界保持不变。

## 2026-09-21：V4.1 Flash 与 18:03 自动化基线

- Round 1 / Round 2 正式模型更新为 `deepseek-flash`（DeepSeek-V4.1-Flash），同步最新人民币峰谷价格与 pricing hash。
- 正式 `schedule` / `full` 计划槽位从北京时间 18:33 调整为 18:03；历史批次、旧 artifact 和历史修复身份保持不变。
- 自动化策略升级到 v21，predecessor 精确指向修改前 v20 policy 哈希；thinking、reasoning、上下文、输出上限、Round 2 transport、费用上限和筛选逻辑不变。

本文件从项目文档治理基线开始，简要记录每个已经完成并验收的独立功能。当前行为始终以代码、配置、测试和 [当前业务规范](PROJECT_SPEC.md) 为准；更早的详细开发流水由 Git、PR、Issue 和 artifact 保存。

## 2026-09-20：arXiv元数据 curl 传输层修复

- 元数据请求改用系统 `curl --http1.1`，保持现有 URL、User-Agent、`Accept: application/atom+xml`、90 秒超时、5 秒请求间隔、Atom/XML 解析及错误语义。
- 仅 HTTP 429、HTTP 5xx 和 timeout 保留一次有界重试；HTTP 406、curl 不可用和其他连接错误安全停止，不改变候选、分页、数据库、模型、日报或正式 workflow 入口。

## 2026-09-18：按需阅读与必要授权治理调整

- `AGENTS.md` 改为按任务影响范围读取文档，取消项目级“最终执行计划”和固定授权口令机制。
- 保留付费调用、外部状态变更、破坏性操作、技术栈变化及业务规范要求的必要授权和绝对安全边界。
- 治理测试继续校验文档、身份、安全和授权契约，不再锁定 `AGENTS.md` 的批准哈希。

## 2026-09-15：arXiv系统级429服务优先处置

- 运行手册补充系统级 HTTP 429 的识别依据、官方信息来源和合规停止条件，明确不得通过并发、频繁重跑或轮换出口规避限制。
- 服务优先恢复时允许定时任务按既有失败关闭边界每日尝试；首次成功按当天计划槽位恢复服务，故障期间缺失日期以后只保留人工回查身份，不重建为原日期正式自动日报。
- 本次只更新运维文档，不改变候选、筛选、模型、提示词、费用、重试、配置、workflow 或自动化代码。

## 2026-09-13：B28恢复账本前向接续

- 普通滚动恢复只为已审计发布的 B28/G7 接受一次完整固定的历史身份，使其下一批迁移到当前 v20 策略；策略哈希、Batch 身份、generation、发布 Run、Issue 或状态任一不符仍失败关闭。
- 不改变候选、模型、提示词、筛选、PDF、日报、费用或重试规则。

## 2026-09-13：arXiv元数据瞬态失败容错

- 正式候选扫描对 HTTP 429、HTTP 5xx 和超时增加一次零费用、60 秒退避的有界重试，单次超时调整为 90 秒并保留明确安全错误码。
- 自动化策略升级到 v20；分类、分页、扫描范围、候选上限、模型、提示词、筛选、PDF、schema、日报、费用和付费模型零重试规则不变。

## 2026-09-13：历史恢复结果零模型发布修复

- Round 2 恢复发布审计只为固定历史失败 G7 接受精确旧策略哈希，同时只允许当前或唯一批准前任的正常 G6；普通 artifact 与日报发布兼容范围不变。
- 新增独立零模型发布入口，严格核验原失败 Run、恢复 Run、job 结论、Batch、日期、Issue 和 artifact 后更新原 Issue并生成 G7，不重复调用模型。
- 自动化策略升级到 v19；模型、提示词、筛选、schema和日报模板不变。

## 2026-09-13：跨平台策略哈希与恢复链修复

- 批准前任策略哈希改为与运行时一致的 UTF-8 LF 规范化身份，避免 Windows CRLF 哈希在 Ubuntu 恢复时被拒绝。
- 自动化策略升级到 v18；历史传输恢复分别锁定含 G5/G6 的 artifact 来源 Run 和最新失败 Run，其他模型、提示词、筛选、schema 和日报行为不变。

## 2026-09-11：DeepSeek思考模式工具兼容修复

- Round 2 保留 Responses、思考强度和唯一命名函数，但移除供应商明确不支持的强制 `tool_choice`；普通文本、空内容、多重或错误函数调用继续失败关闭。
- 自动化策略升级到 v17，缓存请求身份同步变更；仅允许从准确的 v16 HTTP 400失败链迁移到新预约，提示词、筛选策略、数据库 schema和日报模板不变。
- 最小探针只有收到合法目标函数调用才成功，HTTP 200但输出无效不再视为兼容。
- 受控探针在64 Token时确认供应商输出被截断，因此将上限调整为512 Token；仍保持单次 HTTP请求和¥0.01最坏费用上限。

## 2026-09-10：DeepSeek Responses安全兼容性诊断

- HTTP 400/422 只从最多 8 KiB 的 JSON 错误体提取并脱敏 `code`、`type`、`param` 和短消息；原始正文、请求、提示词、响应和凭据均不保存。
- 新增一次性最小 Responses兼容性探针，复用正式 Round 2 模型、思考强度、函数 schema和强制工具选择，但只发送固定短文本且最大输出64 Token。
- 探针由手动与定时双开关、显式付费确认、单次 attempt、¥0.01费用上限和私有诊断 artifact共同约束，不修改日报、Issue、账本或正式运行行为。

## 2026-09-10：Round 2 Responses传输与失败恢复迁移

- Round 2 改用 DeepSeek Responses API，在保留模型、思考强度、提示词和筛选策略的前提下，强制唯一命名函数并只解析 `function_call.arguments`；Round 1 继续使用 Chat Completions。
- 自动化策略升级到 v16，缓存、云端状态、usage和受控重做同步使用新的传输身份；正常非空与四种退化日报规则不变。
- 对已发生的 v15 HTTP 400 恢复增加一次性严格迁移：必须同时核验同 Run 的 G5/G6、唯一 HTTP 400 attempt、无 Token费用、未发布和完整原恢复谱系，才可重绑为新的 Responses预约。

## 2026-09-10：Round 2命名工具输出传输

- Round 2 在保留模型、思考模式、提示词和筛选策略的前提下，改由唯一强制命名工具提交结构化参数，不再依赖偶发为空的正式 `content`；每个逻辑调用仍只有一次 HTTP attempt。
- 输出传输进入缓存、云端状态和自动化策略身份；缺失、多重或错误工具调用以及供应商安全终止原因均以不含正文的明确错误审计停止。
- `empty_content` 恢复兼容升级前的精确请求身份：只允许批准前任策略迁移，并在新预约时切换到当前策略；正常 Top 0 至 5 和既有三类退化日报保持不变。

## 2026-09-09：Round 2空内容恢复来源与受控发布

- `empty_content` 保持为失败而非零入选退化；已发布失败 G7 及其长期恢复副本均可在严格身份校验后作为一次 Round 2 人工恢复来源。
- 恢复仅复用已审计的 Round 1 与全文输入，并在一次有效 Round 2 后以零模型审计更新精确失败 Issue、写入新的正常 G7；不创建重复日报、不删除 Issue、不自动重试。
- 自动日报对新的 `empty_content` 失败保留 400 天的已发布恢复来源，通用 G2至G6 清理不再删除唯一恢复证据。

## 2026-09-08：日报摘要反引号显示修复

- arXiv 摘要中的 LaTeX 双反引号在进入 GitHub Markdown 前进行实体编码，避免跨句误形成灰底代码段；日报模板版本、章节和字段顺序保持不变。
- 已发布历史日报可锁定当前 Issue 正文，并用最新 generation 7 滚动账本核验 Batch 与 Issue 身份；只更新正文展示、追加修复审计，不重新筛选或调用模型。
- 发布前后均检查 GitHub 渲染结果不含错误的代码或删除线标签，且候选、筛选、全文和最新日报内容保持不变。

## 2026-09-07：退化日报与未发布 G6 受控恢复

- 无候选、Round 1 零入选、Round 2 零入选和无合格全文均保持 v14 标准日报结构，并从成功调用账本显示实际模型、Token 与费用。
- 完整两轮受控重做可严格锁定未发布 G6，并把相邻最新 G7 中新增的错误样本安全合并到隔离数据库副本。
- 受控发布允许审计后的 Top 0 至 5，并支持在当日日报不存在时创建 Issue；连续日期恢复必须从新发布 G7 顺序推进下一 Batch。
- 修复未发布 G6 中同策略 `screening_completion` 仍绑定旧 Run 导致 Round 2 无法读取新结果的问题；零入选和非零入选均在隔离副本中重新绑定并校验。

## 2026-09-06：错误样本独立恢复有效账本

- 错误样本工作流只恢复最新有效 generation 7 滚动 artifact，自动跳过失败日报留下的 generation 2 至 6 artifact。
- 保持历史批次错误样本可独立补录，不触发日报、不调用模型，也不改变日报与全文数据库。

## 2026-09-01：需求澄清与严格授权

- 明确要求在写入前消除需求歧义，并以唯一、完整的最终执行计划确定范围。
- 当时曾要求用户紧邻计划单独回复`授权`才允许写入；该临时机制已由 2026-09-18 的治理调整取消，不构成现行授权规则。
- 优化按需文档阅读、已有改动处理、确定性工具和安全删除规则，不改变业务行为或电脑安全边界。

## 2026-09-01：项目文档治理与新对话接续

- 新建项目级 `AGENTS.md`，固化一个独立任务对应一个对话、启动必读顺序、完成定义、文档同步和项目目录安全边界。
- 建立唯一现行 `docs/` 文档体系，将业务规范、运行手册、项目结构和功能记录分离，删除工作树中的失效V1规范及旧流水账。
- 根README保留稳定项目概览和最多100个未处理版本持续向前扫描的核心逻辑，不保存容易过期的云端状态。
- 新增治理测试，校验文档路径、内部链接、当前身份、安全条款和 `AGENTS.md` 归一化SHA-256。
- 本次不改变候选、筛选、提示词、研究画像、PDF、数据库、日报、费用、自动化策略或运行开关。

## 2026-09-01：治理基线记录的当时能力

以下仅记录当时已经具备的能力，不定义现行规则：

- 正式自动与手动回查按版本 `updated` 从截止时间向前扫描最多100篇，不使用固定日历天数下限。
- Round 1最多10篇，经过精确版本PDF和60页全文门控后，Round 2最多5篇；两轮均允许不足或0篇。
- 日报的数据层与Markdown展示层已经分离，并显示Round 2三类最终标签及未推荐数量。
- 自动、手动回查和受控重做使用明确且互不歧义的时间归属。
- 错误样本库保存经确认的漏选、误选和错标，保持幂等、冲突拒绝且不自动修改提示词。
