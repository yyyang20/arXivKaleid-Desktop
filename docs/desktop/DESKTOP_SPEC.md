# Desktop 规范

当前版本 `0.1.0-alpha.4` 使用 PySide6，包含 API Key 密码输入框、获取最新候选和开始两轮分析两个按钮，以及真实运行进度、候选统计、两层诊断和渲染后的 Markdown 日报。

## Portable 路径与资源

Windows 10/11 x64 发行形式为 PyInstaller one-folder ZIP，解压后双击 `arXivKaleid.exe`。application root 是 EXE 所在目录，只读 bundled resource root 是其 `_internal/`，所有运行数据均在 EXE 同级 `runtime/`。源码模式继续使用项目根与 `.desktop-runtime/`。两种模式共用相同配置、策略和两份现行自包含提示词，不复制业务规则。

路径由 `sys.frozen`、`sys.executable` 和资源目录确定，不依赖 cwd；拒绝 `..`、越界路径及 symlink/reparse point。运行目录不可写或资源缺失时，GUI 显示固定错误并禁止操作，不回退到 AppData、Documents、home 或注册表。bundled curl 缺失或哈希不符时安全失败，不搜索系统 PATH；源码模式继续使用系统 curl。

portable 的 SQLite、锁、缓存、PDF、诊断日志、DPAPI 密文分别使用 `runtime/work/`、`runtime/cache/arxiv/`、`runtime/pdfs/<候选日期>/`、`runtime/logs/`、`runtime/config/secret.dat`。文档下文的 `.desktop-runtime/` 路径指源码模式，portable 对应替换为 `runtime/`。IANA `Asia/Shanghai` 数据随包提供，不更改业务时区语义。

## API Key

- Key 可留空，候选抓取不依赖 Key。
- 输入完成（失去焦点或回车）后自动保存；关闭空闲窗口时也尝试保存尚未保存的编辑。
- 使用标准库 `ctypes` 调用当前 Windows 用户上下文的 `CryptProtectData` / `CryptUnprotectData`。不使用机器级共享保护、不写注册表。
- 仅将 DPAPI 密文原子替换到运行根的 `config/secret.dat`，临时文件也只包含密文。
- 启动自动恢复到密码框；清空输入后保存加密空值，下次启动保持为空。
- 保存或解密失败只显示固定安全信息，不输出 Key、底层异常或响应正文；不影响候选抓取。开始分析前必须有非空 Key，未保存的编辑必须成功加密保存；失败时不启动分析。
- 关闭时若尚未保存的编辑加密保存失败，窗口保持打开以显示错误并允许重试。
- 不读取旧 `config/local_secret.json`，不写 `config.json`、SQLite 或日志。密文通常需同一电脑的同一 Windows 用户解密，不应当作可移植配置。

## 候选抓取

点击瞬间记录当前 UTC 日期。从此日期开始搜索 offset `0..14`，包括当天和此前 14 个日历日。固定分类为 `gr-qc`、`astro-ph.HE`、`astro-ph.GA`。

每个日期先通过已有 submittedDate Query API 分页读取三个分类，再只保留 `published` 属于该 UTC 日期的有效记录。按 `(arxiv_id, version)` 批内合并，同版本跨分类只保留一次，不同版本分别保留；沿用已有 `updated DESC` 排序。最近非空日期就是唯一候选日期，不向更早日期凑数。

复用 `main.py` 的 URL、curl HTTP/1.1、Atom 解析、分页、日期回退及合并函数。每页 1000 条，请求间隔 5 秒，单次超时 90 秒；沿用 submittedDate 核心传输默认单次请求，不额外添加自动重试。请求间隔文件只写入 `.desktop-runtime/cache/arxiv/`。

## 统计与内存快照

| 统计 | 定义 |
|---|---|
| 原始条目数 | 目标日期三个分类的有效条目总数，去重前计算；排除 off date，即合并后数量加重复数量 |
| 去重后候选数 | 目标日期按完整身份合并后的数量 |
| 本次进入 Round 1 数量 | 去重后数量与 100 的较小值；只表示锁定的候选数量，不表示已执行分析 |

成功后快照包含候选日期、完成时间、三个数量和排序后最多 100 篇的完整 metadata。候选内容为只读内存对象，抓取阶段不写历史文件或 SQLite，也不查询历史去重。快照另有只能从未尝试变成已尝试的一次性分析标记。

候选日期为 UTC 日历日期。抓取完成时间在快照完成时记录，通过 `zoneinfo.ZoneInfo("Asia/Shanghai")` 显示为 `YYYY-MM-DD HH:MM:SS 北京时间`。

## 状态与线程

- 初始：获取按钮可用，分析按钮禁用。
- 开始抓取：立即丢弃旧快照及其分析标记、清空旧统计和日报、禁用两个按钮，显示正在抓取。
- 完整成功：显示新统计、恢复获取按钮、启用分析按钮。
- 任一分页、分类、解析或其他异常：不返回部分候选，恢复获取按钮，分析按钮保持禁用。
- 15 个日期均无候选：返回 `AKO-FETCH-NO_CANDIDATES` 正常 outcome，不作为程序故障。

抓取与分析分别使用 QThread，GUI 主线程只更新界面；同一窗口不允许并发抓取或分析。运行期间关闭窗口会显示等待提示，任务结束后可以关闭，避免销毁运行中的线程。当前没有中途取消按钮。

## 运行进度反馈

- 候选抓取使用 indeterminate busy 进度条，不根据时间、请求数或日期窗口估算百分比。
- 抓取状态显示正在检查的真实 UTC 日期、当前分类 `N / M` 和当前日期已经取得的有效条目数；`M` 来自运行时分类集合，不依赖固定三个分类。
- 当前日期计数只包含 `published` 属于该日期的条目；切换到更早日期时从 0 重新累计。
- 分析使用 Round 1、PDF 下载、全文提取、Round 2、日报五个真实阶段的步骤状态，不显示百分比。
- PDF 的 `已处理 X / Y` 以 Round 1 实际入围下载任务为总数，成功、复用或单篇失败得到明确结果后都计为已处理，不表述为成功下载数。
- 全文的 `已处理 X / Y` 只以成功下载或复用、真正进入全文处理的 PDF 为总数；提取失败和超过 60 页仍属于已处理的明确结果。
- 零候选、Round 1 零入围、无 PDF、无合格全文或 Round 2 零推荐沿用现行业务语义，并把未执行阶段标记为跳过；真实失败停留在对应阶段。
- 进度由后台结构化事件提供，GUI 不自行推算业务量。事件不增加 arXiv、PDF 或模型请求，也不改变共享筛选、门控、重试、日报或 SQLite 规则。

## 一次性两轮分析

每次启动窗口后，第一次开始分析前显示简短告知：标题、摘要、通过门控后的 PDF 提取全文和 Prompt 内置研究边界会发送到用户自己的 DeepSeek API，并可能产生费用；API Key 在本机由 DPAPI 加密保存，PDF、SQLite 和缓存位于本机，没有维护者服务器中转或遥测。对话框默认为拒绝；拒绝时不创建分析 attempt、不消费快照、不启动线程或模型请求。用户接受后，本次窗口生命周期不重复提示。

分析前检查冻结快照、尚未尝试标记和 Key。通过检查后立即消费快照并禁用两个按钮；后台直接按冻结顺序处理相同 `(arxiv_id, version)` 候选，不重新请求 arXiv、不改变日期、不从历史 SQLite 重建候选。

分析复用现行自包含 Prompt、`profile_v2` 兼容身份、模型、严格校验、选择策略、PDF 下载和全文门控函数；不会另行发送 Research Profile 内容。共享业务规则见 [PROJECT_SPEC.md](../PROJECT_SPEC.md)。Round 1 完成状态和入围结果写入当前工作库，PDF 只处理实际入围论文，Round 2 只接受当前 run 的合格全文，不补位或重排。

GUI 当前 Key 显式注入两个客户端，Round 2 可选 override 未提供时保留传统调用行为。Desktop 不执行 DeepSeek self check；每轮最多一次 HTTP attempt，失败不重试。无候选不调用模型，Round 1 零入围或无合格全文不调用 Round 2，两轮均允许零推荐。

请求前使用现有保守 Token 估算、上下文安全余量和峰值价格，Round 2 额外计入结果工具载荷；下一轮预检累计本批已发生费用。单批上限 ¥3.00，usage 或费用无法确认时停止后续调用；实际费用按调用时价格快照落库。

SQLite 只保存当前 attempt 的内部状态，不是历史数据库。分析期间持有操作系统文件锁，阻止其他窗口或进程重置正在使用的工作库；进程退出自动释放锁。新 attempt 只重新初始化两个固定工作库及其 SQLite sidecar，保留 PDF、Secret 和缓存。

阶段状态区分准备、Round 1、PDF 与全文、Round 2 输入门控、Round 2、日报生成与完成。成功或失败后恢复获取按钮，分析按钮保持禁用；失败显示阶段、类别级稳定错误代码、原因、影响、建议和关联身份，不显示底层异常、模型原始响应、prompt 或全文。重新分析必须重新获取候选。

## 两层诊断与失败范围

GUI 将系统错误、单篇局部问题和正常业务 outcome 分开显示。HTTP 状态和 curl exit code 只作为结构化 details，不拆分稳定代码；`transient` 与是否允许自动重试独立，alpha.4 不新增自动重试。paper-scope 问题按代码聚合，论文身份只写入本地日志。

PDF 与全文异常按实际影响范围判断：已进入单篇边界、共享组件和 SQLite 完整、单篇失败成功持久化时继续后续论文；共享组件初始化、SQLite、runtime 存储或全局状态失败时停止批次；无法确认时使用阶段 `UNEXPECTED` 失败关闭。未枚举异常类型本身不等于系统错误。

“无成功 PDF”或“无合格全文”只有在全部未通过项均为已确认单篇问题或正常门控、失败记录全部持久化且共享完整性检查通过时才是 outcome。正常无候选、零入围、长论文或 Token 排除、零推荐不作为程序故障。

每个进程在源码 `.desktop-runtime/logs/` 或 portable `runtime/logs/` 创建一个 `desktop-<UTC>-<session_id>.jsonl`。事件记录阶段 start/complete/skip/outcome/fail、耗时、计数和 session/operation/snapshot/fetch/run 关联。未预见异常的 traceback 仅保留模块、函数和行号。日志写入失败时降级为最多 512 条内存事件，不改变业务结果。

日志不保存 API Key、Authorization、Cookie、DPAPI 密文、完整 prompt、摘要、全文、逐页文本、模型请求或原始响应、response ID、curl stderr、HTTP 正文或未知响应头、环境变量全集、命令行全集、traceback locals 或用户绝对路径。

## Desktop 日报

`desktop/report.py` 从当前 run 的 SQLite 读取计数、筛选结果、标签、PDF 页数、门控决定、模型、Token 和费用，复用正式 Round 2 推荐内容区块。薄 Desktop 外壳显示候选日期、两轮输入和结果数量，以及 Round 1 入围详情；不伪造自动化、计划槽位或发布身份。

日报通过 `QTextBrowser.setMarkdown()` 渲染；仅允许打开正常的 HTTPS arXiv 摘要和 PDF 链接，本地 PDF 以相对路径信息显示。日报不写入根 `reports/daily/`，没有编辑、导出或历史管理功能。分析及日报生成成功后若仅 GUI Markdown 渲染失败，SQLite run 保持成功，界面明确显示“分析成功、日报展示失败”。
