# 功能变更记录

本文件保留 Desktop 与通用功能的简短历史。代码解耦前的条目来自原项目：当时的发行来源、构建提交、兼容行为和治理测试仅表示历史事实，不代表当前仓库仍有这些入口或测试。现行行为以代码、配置、测试和 [当前业务规范](PROJECT_SPEC.md) 为准；详细历史通过 Git、PR 和 Issue 查询。

## 2026-10-04：Desktop alpha 7.1 非空证据校验修复

- 补充 `unicodedata` 标准库导入，修复 Round 1 模型返回非空证据后，本地校验触发未预见异常的问题；筛选逻辑、Prompt、GUI、模型配置和依赖保持不变。
- 新增非空标题/摘要证据、Unicode 与空白规范化、无效证据容错及两轮合成 HTTP 回归测试；冻结程序诊断增加非空证据校验，不访问真实 arXiv 或 DeepSeek。
- Desktop 版本更新为 `0.1.0-alpha.7.1`；现行版本说明和 portable 对应源码入口同步，历史发行保持不变。

## 2026-10-03：Desktop alpha.7 公开发行

- 已公开 [v0.1.0-alpha.7 immutable prerelease](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.7)，仅上传 Windows x64 ZIP 和 `.sha256`；发行 tag 与 BUILD_INFO 均指向合并后的 main 提交 `fc7ca1cb0eab1759cdb4c2d1ea0d5d5f2f0a23ce`，用途为 public-release。
- ZIP 为 118,590,285 字节，SHA-256 为 `bdaf603fe7018b6524fc9a3de5a56d71ba584420e9a8315fc0269a5b5d1c22be`；匿名下载成品、校验文件和 tag 源码通过，82 个源码文件与发行提交逐字节一致，GitHub asset digest 与本地摘要一致。
- [PR #8](https://github.com/yyyang20/arXivKaleid-Desktop/pull/8) 冻结提交及发行 main 均通过 129 项完整离线测试、零跳过和 44 个 Python 文件 AST 检查；技术与正式包分别完成原生 DPR 1.5、模拟 100/125/150/200% 的 13 状态 GUI、三次重启、合成 DPAPI 恢复、资源缺失拒绝与运行目录占用验证。
- A0 图标六个尺寸及透明角、Qt/EXE 资源核验通过；标题栏、资源管理器、任务栏与 Alt+Tab 已完成实际检查，正式包任务栏和 Alt+Tab 经用户人工确认。核验八组对应源码、许可、资源 allowlist 及隐私扫描；技术 ZIP 未上传。
- alpha 1～6 的历史 tag、Release 正文及资产身份与发布前基线完全一致。本条仅为发布后文档收口，不重建或替换已发布资产；最终 main 与发行 tag 分别记录。同机隔离未覆盖第二台干净 Windows；未调用真实 arXiv、PDF 或 DeepSeek，模型调用与费用均为 0。

## 2026-10-03：Desktop alpha.7 视觉统一与 A0 应用图标

- 版本升级为 `0.1.0-alpha.7`；首页、历史页和设置页统一为浅灰蓝背景、低对比度卡片边框、8 px 圆角和一致页面间距，API Key 输入框融入同色系并保留聚焦蓝色描边。
- 根据冻结 A0 方向重绘透明画布 SVG 母版，确定深海军蓝底板与四色几何 K；生成包含 16、24、32、48、64、256 px 图层的 ICO，16/24 px 使用无渐变简化层。
- 同一 ICO 接入 Qt 应用/窗口图标、冻结资源 allowlist、Qt ICO 解码插件和 PyInstaller EXE 配置；完整 129 项离线测试与 13 状态源码 GUI 视觉 QA 通过。候选、分析、日报、DPAPI、密码显示与其他业务行为不变；构建和发行验证不调用真实 arXiv 或 DeepSeek。

## 2026-10-02：公开维护文本语言统一

- 根协作规则明确后续 PR、Issue、发行说明、公开评论、提交说明和项目说明默认使用简体中文；保留英文标识、必要技术术语及第三方原文，避免无必要的中英文混写。
- 文档索引、运行手册和发布检查清单只引用根规则，治理测试覆盖规则缺失及引用回退。历史 PR、Release、tag、资产和评论不回改，不变更版本或重建已发布成品。

## 2026-10-02：测试版展示名称一致性

- 逐处统一现行 README、运行手册、发布清单、CHANGELOG 和第三方声明中的测试版展示名称为小写 alpha；文档索引明确长期规范，维护源和副本保持一致。
- 治理测试覆盖全部现行说明文本及大小写反例；标准版本标识、文件名、URL、正式版本常量和 BUILD_INFO 不变。历史 GitHub Release、tag、资产和对应源码不改写，本次不重建已发布成品。

## 2026-10-02：Desktop alpha.6 公开发行

- 已公开 [v0.1.0-alpha.6 immutable prerelease](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.6)，仅上传 Windows x64 ZIP 与校验文件；发行 tag 和 BUILD_INFO 提交均为 `83a58b1abb03235924b96e8d3227f75ca0831436`，用途为 public-release。
- 正式 ZIP 为 118,577,625 字节，SHA-256 为 `14b2c6095fda1c0159289eac30e90a7f05ad25d3178a87eaeb62aca76839b02e`；匿名下载成品及校验文件通过，77 个公开源码文件与发行提交逐字节一致。
- [PR #4](https://github.com/yyyang20/arXivKaleid-Desktop/pull/4) 冻结提交及正式 main 均通过 123 项离线测试、零跳过，包含 Windows GUI 与合成 DPAPI；正式 main 重新构建并通过原生 DPR 1.5 和模拟 1.0/1.25/1.5/2.0、每组 13 个 GUI 状态、凭据恢复及三次独立重启验收。同机隔离未覆盖第二台干净 Windows。
- 核验八项固定对应源码及 216 项原许可文本；Fluent 1.11.3 原始 LICENSE 与包声明已核对，商业措辞歧义记录在 NOTICE，未发现当前材料无法解决的具体发行权利缺口，自有 GPL-3.0-only 不变。
- alpha 1～5 历史 tag、Release 正文及资产身份与基线一致；alpha 4 的原私人源码构建身份按其原 Release 正文核验，alpha 4/5 正式成品公开下载和校验通过。按新收口规则，发布任务在全部核验通过及已获授权后仅清理 release/ 的旧 ZIP/校验文件，其他材料保留。
- 本条为发布后文档收口，不重建或替换已发布资产；最终 main 与发行 tag 分别记录。未访问真实业务 arXiv/PDF/DeepSeek，模型调用与费用均为 0。

## 2026-10-02：正式构建身份与本地历史 portable 收口规则

- 正式 main 构建必须等于已核验的 origin/main，BUILD_INFO 区分 public-release 与开发分支技术验证；增加身份拒绝测试。
- 两块显示器使用不同原生 DPI 时，冻结验收窗口固定在主屏并逐截图核验屏幕身份，避免跨屏启动位置让模拟倍率失真；不改变正式 GUI 或系统显示设置。
- 每次发布在新旧公开资产核验通过且删除获授权后，release/ 只保留最新正式 ZIP 和校验文件；构建/失败阶段保留材料，不清理源码、审计或其他运行数据。新增治理测试与内存反例。
- 核对 Fluent 1.11.3 原许可、版权和包声明并记录商业措辞歧义；保留 GPL-3.0-only 与原许可/对应源码方案，不仅因商业措辞停止发行。本条不表示 alpha 6 已公开发布。

## 2026-10-02：Desktop alpha.6 portable 构建与隔离验收支持

- 补齐八项固定源码归档、原许可和实际分发组件清单；最小 Qt/插件收口，保留 Fluent 内嵌资源和必要 pywin32 DLL/运行时 hook，不使用 full 或系统字体。
- 构建要求开发分支干净冻结提交及已核验 main 基线，不要求两个提交相同；记录模块、构建身份、文件哈希和体积组成。正式 Release 仍在合并后的 main/tag 重建核验。
- 离线故障注入确认后最小修复 portable 后置检查失败仍可能报告成功的路径，增加完整诊断回归测试；冻结验收提供主要 GUI 状态、合成 DPAPI 恢复、实际/模拟 DPI、缺失资源、运行目录拒绝和重复生命周期验证，不调用真实业务网络或模型。
- 冻结模拟缩放发现顶部按钮重叠后，改用信息卡实时高度，并允许小窗口缩小可滚动日报正文；信息架构和业务不变，增加大字体/小窗口及冻结控件不重叠检查。
- 自有许可保持 GPL-3.0-only；第三方材料沿用现有 NOTICE/source manifest/source bundle，Fluent 许可说明适用关系及公开 Release 留待后续核对与授权。

## 2026-10-02：Desktop alpha.6 Fluent GUI 本地实现

- 统一版本升级为 `0.1.0-alpha.6`，使用基础版 PySide6-Fluent-Widgets，拆出首页、历史占位页、设置页及可复用任务区域；版本显示动态读取，API Key 移入设置页并保留 DPAPI、自动保存与分析期间禁用行为。
- 首页保留两个独立按钮和常驻抓取信息；抓取采用不定进度，分析显示真实五阶段状态，成功后收缩为可展开摘要，失败保留诊断和日志入口，日报继续占主要空间。
- 候选冻结与一次性分析、模型次数、零自动重试、PDF/全文门控、费用、SQLite、Prompt 和日报业务契约保持不变；新增依赖固定版本，更新 portable 资源与许可收集准备。
- 完整离线测试 115 项、零跳过，包含 Windows GUI 与 DPAPI 假值；隔离 mock 视觉 QA 覆盖 13 个状态及 DPR 1.25/2.25 两组缩放。未访问真实 arXiv/DeepSeek、未产生模型费用，尚未构建或发布 alpha.6 portable；Fluent 许可确认及第三方对应源码清单补全留作正式发行前置条件。

## 2026-10-01：Desktop alpha.5 公开发行

- 已公开 [v0.1.0-alpha.5 prerelease](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.5)，同步提供 GPL-3.0-only 完整源码、文档及 Windows portable；EULA 为许可说明与使用告知，第三方保留各自许可及实际要求的对应源码。
- 发行 tag 与最终 portable 构建提交均为 `b06321bd8b8e7b3d4dc3f61016ced09b1eeafce7`；ZIP SHA-256 为 `8cad5043e92741345a40caae800df90f2e24f9a1a0b0a8b4531e33c48795b40c`，仅上传 ZIP 与 `.sha256`。
- 冻结 [PR #2](https://github.com/yyyang20/arXivKaleid-Desktop/pull/2) 和合并后 main 均通过完整离线测试 108 项、零跳过，包含真实 Windows GUI 与 DPAPI 假值；两次 portable 构建及零模型验证通过。
- 未登录下载验证公开 ZIP、校验文件、BUILD_INFO、许可证及第三方源码哈希；[tag 源码归档](https://github.com/yyyang20/arXivKaleid-Desktop/archive/refs/tags/v0.1.0-alpha.5.zip)的 72 个文件逐一与发行提交字节一致。alpha.1 至 alpha.4 的 tag、Release 正文、资产及摘要与任务开始基线完全一致。
- 本条为发布后文档收口，不重建或替换已发布资产；发行/tag 提交与后续 main 文档提交分别记录。没有真实模型、付费调用或真实 arXiv 验证，未引入 Fluent Widgets。

## 2026-10-01：Desktop alpha.5 GPL 开源发行准备

- 应用版本升级为 `0.1.0-alpha.5`，自有源码、测试、构建脚本、配置、Prompt 和项目文档采用 GPL-3.0-only；新增标准 LICENSE，EULA 改为许可说明与使用告知。
- portable 增加应用 LICENSE 与精确版本 tag 的源码入口，保留第三方许可与 Qt/PySide 对应源码，补充 pypdf 固定源码归档及 gzip/xz 收集和发行校验。
- 保留筛选、GUI、模型次数、费用、配置和 Prompt 字节；未引入 Fluent Widgets。实际公开发行记录在发布验收后补记，alpha.1 至 alpha.4 历史发行保持原状。

## 2026-10-01：Desktop 根协作规则收尾

- 根协作规则明确项目名称为 `arXivKaleid-Desktop`，移除不存在的 workflow、GitHub-hosted runner 和仓库变量表述，实时核验对象对齐 Git、PR、tag、Release 与 release asset。
- 保留其余安全、授权、文档更新、测试、构建和发布规则；敏感信息条款使用“发布资产”表述，未改变应用或现有治理测试。

## 2026-10-01：缓存复用测试 SQLite 连接清理

- 显式关闭缓存复用测试中的全文库连接，修复新定位的第三处 SQLite `ResourceWarning`；保留原有事务行为、缓存校验和模型调用断言。
- 本次仅修改测试资源清理并记录变更，未改变应用源码、数据库 schema、配置或依赖。

## 2026-10-01：解耦测试 SQLite 连接清理

- 为解耦测试中的两处内存 SQLite 连接增加显式关闭，避免连接回收时产生 `ResourceWarning`；保留原有事务行为、schema 校验与断言。
- 本次仅调整测试资源清理并记录变更，未改变应用源码、数据库 schema、配置或依赖。

## 2026-10-01：Desktop 治理补全

- 根协作规则补齐 Codex 持续后台任务授权、进程级临时环境、Desktop 运行与发行校验边界，以及测试跳过的报告和验收要求；应用正常 QThread 保持原有授权语义。
- 明确根与 Desktop 子文档分工，补齐版本身份表和公开文档副本的更新路由；运行手册同步可恢复 TEMP/TMP 的验证方式，两层结构文档登记治理测试，并补记此前完成的代码解耦。
- 新增标准库离线治理测试，核对规则、阅读与更新路由、当前身份、文档链接、发行资料副本和发布检查要求，以内存反例检查缺失身份、断链、越界与副本漂移；完整离线测试及静态检查通过。未改变应用源码、配置、Prompt、依赖或发行版本，未构建或发布成品。

## 2026-10-01：Desktop 文档解耦第一阶段

- 清理现行文档中的 Online 自动化、generation/artifact、错误样本、恢复、Issue 日报发布和跨仓维护要求，纠正已删除模块与治理测试的引用。
- README 区分源码仓库与 portable 使用说明；业务、运行和结构文档对应当前冻结快照、schema v1 工作库、显式凭据注入、GUI 日报及现有打包资源。
- 保留已有 Desktop 行为、通用协作、安全与 Release 要求；未新增治理规则，未改变源码、配置、提示词、依赖或发行版本，也未发布成品。

## 2026-10-01：Desktop 代码解耦

- 引入 Desktop 独立配置与预算校验，裁剪核心模块中的 Online 专属入口、策略和 Profile 文件依赖，对齐当前工作库 schema v1 与 portable 资源集合。
- 保留 alpha.4 的两轮筛选、PDF 门控、模型次数、费用和日报行为，新增合成行为基线、配置与独立源码运行测试；应用版本仍为 `0.1.0-alpha.4`，未发布成品。

## 2026-09-30：Desktop 两层错误诊断

- Desktop GUI 新增阶段、类别级稳定错误代码、影响范围、操作建议及 session/operation/snapshot/fetch/run 关联，正常无候选、零入围、长论文/Token 排除、无合格全文和零推荐继续作为业务 outcome。
- 新增源码 `.desktop-runtime/logs/` 与 portable `runtime/logs/` 脱敏 JSONL，记录阶段生命周期、耗时、计数和既有安全结构诊断；写盘失败降级到 512 条内存 ring，不改变原业务结果。
- PDF/全文失败改按实际影响范围判定：已确认单篇且共享状态完整时继续，SQLite、共享组件、runtime 存储或无法确认范围时整批失败关闭；未枚举异常不自动升级。
- 日报已生成而 GUI Markdown 渲染失败时保持分析成功；发行扫描新增 JSONL 排除，版本升级为 `0.1.0-alpha.4`，筛选、模型次数、自动重试和费用规则不变。

## 2026-09-29：Desktop Release 版本更新说明治理

- 明确每个新的 Desktop GitHub Release 都必须包含面向用户的非空“本次更新”：首个公开版本概括首次能力，后续版本依据冻结提交、Git 差异和变更记录概括相对上版的 1～5 条主要变化。
- 公开发布检查清单新增发布前非空检查和发布后公开回读；平台、下载、SmartScreen、DeepSeek、费用和 SHA-256 等通用说明不能代替版本更新说明。
- 依据三个实际公开 ZIP 的构建提交与版本间差异，补齐 alpha.1 首发能力、alpha.2 运行进度反馈和 alpha.3 自包含 Prompt 收敛的公开版本更新说明；Tag 和 Release assets 保持不变。

## 2026-09-28：Windows Desktop alpha.3 公开发行

- [`v0.1.0-alpha.3`](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.3) 当时作为 immutable prerelease 发布到公开仓库，成品从原私人仓库冻结提交 `73acce981bbfac1e15b68e77981aa4b3b2f59831` 重新构建。
- 公开资产仅包含 `arXivKaleid-0.1.0-alpha.3-windows-x64.zip` 与对应 `.sha256`；ZIP SHA-256 为 `d5a3c78c2112b01ae1fe12172aa28655ee73da264878db0c930237eddac480b0`。
- 完整离线测试、Desktop portable 零模型自检、资源与隐私扫描、GitHub asset digest 及未登录下载核验均通过；公开隐私说明已同步，既有 alpha.1 和 alpha.2 标签、Release、资产及说明未修改。

## 2026-09-28：两份自包含 Prompt 收敛

- 正常请求改用自包含的 `round1_v20 + round2_v15`，两份 Prompt 吸收相同的普适研究边界，不改变四类标签、Top 10/Top 5、顺序、不补位、模型、费用或 PDF 门控；请求不再注入独立 Research Profile 内容，仅保留 `profile_v2` 兼容身份。
- Desktop 源码版本升级为 `0.1.0-alpha.3`，承载本次 Prompt/Profile 收敛；当时的 portable 仍包含策略资源，当前资源集合以构建配置为准。

## 2026-09-27：Windows Desktop alpha.2 公开发行

- `v0.1.0-alpha.2` 当时作为 immutable prerelease 发布到公开下载仓库 [`yyyang20/arXivKaleid-Desktop`](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.2)。
- Release 只包含版本化 Windows x64 portable ZIP 和对应 `.sha256`；成品从当时已验收的干净 `main` 重建，通过完整离线测试、portable 零模型验证、本地与 GitHub asset digest 及未登录下载核验。
- 既有 `v0.1.0-alpha.1` immutable Release 保持不变，本次 DeepSeek 调用和模型费用均为 0。

## 2026-09-27：Desktop 真实运行进度反馈

- 候选抓取增加 indeterminate 进度，显示真实 UTC 检查日期、动态分类 N/M 和当前日期有效条目数；日期切换时重新计数。
- 两轮分析增加 Round 1、PDF、全文、Round 2 和日报步骤状态；PDF 与全文 X/Y 来自真实任务结果，合法零结果显示跳过，失败停留在实际阶段。
- PDF 与全文处理只增加默认关闭的可选观察回调；筛选、门控、网络请求、模型调用、重试和 SQLite 行为不变。

## 2026-09-27：arXiv transport 安全取证辅助函数

- 元数据非 200 路径从同一次 curl 请求附加采集实际目标 IP、HTTP 版本、SSL 校验、固定 timing 和白名单响应头；不启用 verbose，不新增网络请求。
- 取证使用有界日志和项目内短期响应头文件，未知头、Cookie、stderr、代理值和原始内容均不输出；准备、解析、日志和清理异常全部 fail-open。
- 抽取 write-out 与响应头解析为纯函数，未改变原请求参数和返回行为。

## 2026-09-26：arXiv 非 200 响应安全诊断

- curl 元数据入口在非 200 响应时追加有界结构化日志，只记录状态、尝试次数、正文长度、前 8 KiB 样本哈希和固定特征标签，不输出响应原文或 stderr。
- 诊断异常与原 HTTP 处理隔离；请求参数、1000 条分页、超时、间隔、重试、错误码及业务身份保持不变，不新增网络请求或文件。
- 补充模拟响应、安全边界及请求行为回归测试；诊断只提供后续定位线索，不代表 406 根因已确认或修复。

## 2026-09-26：Windows Desktop 首次公开发行与文档治理收口

- `v0.1.0-alpha.1` 当时作为 prerelease 发布到公开下载仓库 `yyyang20/arXivKaleid-Desktop`，既有 immutable Release 资产保持不变。
- README 补充 vibe coding 开发方式和人工/Codex 职责，文档路由补齐 Desktop 版本、构建基线和实际 Release 发布职责；未改变 Desktop 功能、业务规则、prompt、profile 或构建产物。

## 2026-09-26：Windows Desktop 公开发布准备

- 新增 portable 使用所需的 README、EULA、隐私、安全说明和发布检查清单；ZIP 留在被忽略的 `release/`。
- Desktop 每次启动后首次分析前显示 DeepSeek 数据发送、费用、DPAPI 凭据与本地数据告知；默认拒绝，拒绝时不消费快照或调用模型，冻结 EXE 的零模型诊断直接覆盖该拒绝路径。
- portable 构建按 allowlist 将应用 README、EULA、隐私和安全说明放入 ZIP 根目录，保留第三方声明、许可和对应源码归档；筛选、prompt、profile、PDF 门控和费用规则不变。

## 2026-09-25：Windows Desktop portable 打包

- 新增 Windows x64 PyInstaller one-folder 构建入口，固定构建依赖与官方 curl 校验清单，生成本地 portable ZIP、SHA-256 和提交身份。
- 分离只读资源与可写 runtime，保留源码 `.desktop-runtime/`；补充冻结模式路径、工作库、两轮 mock 集成、curl 和发行扫描测试。
- 随包提供 IANA 时区、第三方声明、许可证和 Qt 对应源码；增加全新副本的 GUI、DPAPI 假值、依赖、重启及可选真实 arXiv 零模型验证。

## 2026-09-25：Windows Desktop 第二阶段

- 分析按钮直接消费一次冻结候选快照，复用 Round 1、PDF、60 页全文门控、Round 2 和 usage 审计；Key 显式注入，不额外 self check，每轮最多一次 HTTP attempt，累计预算上限 ¥3.00。
- 当前工作 SQLite 与 PDF 隔离在 `.desktop-runtime/`，只重置已知工作库及 sidecar；文件锁保护并发，失败不重试、不补位、不查询历史候选。
- 新增 Desktop Markdown 外壳与 QTextBrowser 渲染，后台阶段信号、消费后按钮状态及对应离线集成测试。

## 2026-09-25：Windows Desktop 第一阶段

- 新增 Desktop `0.1.0-alpha.1` PySide6 GUI，复用 submittedDate 核心抓取最近非空日期，显示批内去重统计并冻结最多 100 篇内存候选。
- API Key 使用当前 Windows 用户 DPAPI 自动加密保存和恢复；Secret 与请求间隔缓存隔离在被忽略的 `.desktop-runtime/`。
- 新增后台线程及失败失效边界测试、Desktop 文档与独立固定依赖；当时分析按钮仅显示阶段说明，尚未连接模型、PDF、SQLite 或日报。

## 2026-09-25：清理未使用的目录脚手架

- 移除本地入口创建但从未被读取的 `input_code/`、`input_papers/`，并清理未使用的 `paths.extracted_dir` 配置、校验和测试夹具残留。

## 2026-09-25：仓库卫生与本地产物清理

- 补充根级 `.codex-audit-*/` 和 `.pytest_cache/` 忽略规则，并同步运行产物文档。
- 清理已确认的 Python 字节码、pytest 缓存、旧审计目录和空临时目录；业务规范、筛选逻辑和运行数据不变。

## 2026-09-25：项目专用 Conda 环境授权例外

- 电脑安全边界新增唯一、受控的项目专用 Conda 环境例外：仅在用户明确授权后，允许维护指定环境及当前任务明确需要的 Python 依赖；`base`、其他环境、系统 Python、PATH、Windows 设置、全局软件和其他项目继续禁止修改。
- 当时治理测试同步核验默认项目外只读、授权范围、依赖范围、越界重新授权及绝对禁止项，其他项目安全边界保持不变。

## 2026-09-21：V4.1 Flash 模型与价格快照

- Round 1 / Round 2 模型更新为 `deepseek-flash`（DeepSeek-V4.1-Flash），同步当时确认的人民币峰谷价格快照。

## 2026-09-20：arXiv 元数据 curl 传输层修复

- 元数据请求改用系统 `curl --http1.1`，保持现有 URL、User-Agent、`Accept: application/atom+xml`、90 秒超时、5 秒请求间隔、Atom/XML 解析及错误语义。

## 2026-09-18：按需阅读与必要授权治理调整

- `AGENTS.md` 改为按任务影响范围读取文档，取消项目级“最终执行计划”和固定授权口令机制。
- 保留付费调用、外部状态变更、破坏性操作、技术栈变化及业务规范要求的必要授权和绝对安全边界。
- 当时治理测试校验文档、身份、安全和授权契约，不再锁定 `AGENTS.md` 的批准哈希。

## 2026-09-11：DeepSeek 思考模式工具兼容修复

- Round 2 保留 Responses、思考强度和唯一命名函数，但移除供应商明确不支持的强制 `tool_choice`；普通文本、空内容、多重或错误函数调用继续失败关闭。

## 2026-09-10：DeepSeek Responses 安全兼容性诊断

- HTTP 400/422 只从最多 8 KiB 的 JSON 错误体提取并脱敏 `code`、`type`、`param` 和短消息；原始正文、请求、提示词、响应和凭据均不保存。

## 2026-09-10：Round 2 Responses 传输

- Round 2 当时改用 DeepSeek Responses API，在保留模型、思考强度、提示词和筛选策略的前提下，强制唯一命名函数并只解析 `function_call.arguments`；Round 1 继续使用 Chat Completions。强制 `tool_choice` 后于 2026-09-11 移除。

## 2026-09-10：Round 2 命名工具输出传输

- Round 2 当时在保留模型、思考模式、提示词和筛选策略的前提下，改由唯一强制命名工具提交结构化参数，不再依赖偶发为空的正式 `content`；每个逻辑调用仍只有一次 HTTP attempt。
- 缺失、多重或错误工具调用以及供应商安全终止原因以不含正文的错误审计停止。

## 2026-09-08：日报摘要反引号显示修复

- arXiv 摘要中的 LaTeX 双反引号在进入 Markdown 前进行实体编码，避免跨句误形成灰底代码段；日报模板版本、章节和字段顺序保持不变。

## 2026-09-01：需求澄清与严格授权

- 当时要求在写入前消除需求歧义，并以唯一、完整的最终执行计划确定范围。
- 当时曾要求用户紧邻计划单独回复“授权”才允许写入；该临时机制已由 2026-09-18 的治理调整取消，不构成现行授权规则。
- 优化按需文档阅读、已有改动处理、确定性工具和安全删除规则，不改变业务行为或电脑安全边界。

## 2026-09-01：项目文档治理与新对话接续

- 新建项目级 `AGENTS.md`，固化一个独立任务对应一个对话、启动必读顺序、完成定义、文档同步和项目目录安全边界。
- 建立唯一现行 `docs/` 文档体系，将业务规范、运行手册、项目结构和功能记录分离，删除工作树中的失效规范及旧流水账。
- 当时新增治理测试，校验文档路径、内部链接、当前身份、安全条款和 `AGENTS.md` 归一化 SHA-256。
