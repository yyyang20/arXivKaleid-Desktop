# Desktop 运行与验证

最新公开成品为 alpha 10 Windows x64 portable，下载与运行方式见下文。

本文档维护源码启动、依赖、本机构建、portable 验证及运行排错细节。开发与公开发布流程见根 [运行手册](../OPERATIONS.md)，安全、授权和完成要求以根 [AGENTS.md](../../AGENTS.md) 为准，阅读与更新路由见 [文档索引](../README.md)。

## 源码启动

在项目根目录、已经具备依赖的 Windows Python 环境中运行：

```powershell
python -B -m desktop.app
```

当前版本为 `0.1.0-alpha.11`，第一阶段为本地技术候选，尚未公开发布。使用 PySide6 / Qt `6.9.2`、PySide6-Fluent-Widgets `1.11.3` 基础版、PySideSix-Frameless-Window `0.8.2`、darkdetect `0.8.0`、Windows pywin32 `312`，均固定在 [requirements-desktop.txt](../../requirements-desktop.txt)。不安装 full 的 scipy、pillow、colorthief。该文件同时引入 [requirements.txt](../../requirements.txt) 中的 pypdf 核心运行依赖；[requirements-build.txt](../../requirements-build.txt) 单独固定 PyInstaller 及构建依赖。

仅在用户已授权且已核实为本项目专用的 Conda 环境中，才可安装 Desktop 依赖：

```powershell
python -m pip install --no-cache-dir --requirement requirements-desktop.txt
```

执行安装前应按根安全规则核验 `python -c "import sys; print(sys.executable); print(sys.prefix)"`，并将安装临时目录限制在项目内被忽略的运行目录。不能在 `base`、系统 Python 或其他项目环境安装；已有匹配依赖时直接使用，不改动环境。

需要系统已有 `curl` 和可读取的 `Asia/Shanghai` 时区数据库。可只读检查：

```powershell
curl --version
python -B -c "from zoneinfo import ZoneInfo; print(ZoneInfo('Asia/Shanghai'))"
```

Windows 环境缺少时区数据时会安全停止抓取，不自动安装额外包、不修改系统时区或环境配置。构建专用 Conda 环境使用已授权的 `tzdata=2026c`，portable 从该环境收集 `Asia/Shanghai` 数据。GUI 缺少 PySide6 时，应按上述授权边界准备环境。

## Portable 用户

解压 `arXivKaleid-<version>-windows-x64.zip` 到当前用户可写目录，双击 `arXivKaleid.exe`；不需要 Python、Conda、PySide6、pypdf 或 Git。当前公开成品为 [alpha 10](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.10)，从合并后的干净 main 提交 `89804e2895691d88f6ae62277e219de8f98ef0f9` 重建。正式 portable 已完成五组 DPI 各 43 个 GUI 状态、真实 12/20 pt 字体、整卡点击与键盘、研究要求保存/保护/跨进程恢复、请求哈希及安全边界、历史原文、DPAPI、普通重启与 DLL 来源等零模型验证；匿名 ZIP、校验文件和 94 个 tag 源码文件与发行提交一致。源码 GUI 在设置页输入自己的 DeepSeek API Key，首页先获取候选，再执行分析。

所有用户数据都在 EXE 同级 `runtime/`，诊断日志位于 `runtime/logs/`。删除整个 portable 目录相当于卸载并删除运行数据；当前版本不自动轮换或删除旧日志。不要把包含个人 `runtime/` 的已使用目录重新发给其他人，应发送构建生成的干净 ZIP。DPAPI 绑定 Windows 用户与电脑，复制文件夹给另一个用户/电脑后，旧 `secret.dat` 通常无法解密，需要重新输入 Key。

第一版只支持 Windows 10/11 x64，未代码签名，可能触发 SmartScreen；不绕过系统安全机制。没有安装器、自动更新或 ARM64 版本。

## 本机构建

资源 allowlist 同时收集 `docs/desktop/USER_GUIDE.md` 和 `assets/user-guide.svg` 到 `_internal/` 原相对路径，`BUILD_INFO.json` 绑定其原始字节哈希，源码与冻结程序的说明及书本图标一致。仅修改指南文字时无需修改 GUI 代码，但仍须按现有更新义务检查、测试并重新打包；已交付 ZIP 不自动更新。

alpha 7 收集 Qt Core/Gui/Widgets/Svg/SvgWidgets/Xml 和 Windows 平台、Windows 11 样式、SVG 图标插件及 ICO 图像解码插件；不带 QtNetwork、qsvg 图像插件或 offscreen 平台。Fluent 图标/QSS 由其内嵌 Qt 资源模块提供，不从网络获取图标；正式 `assets/app-icon.ico` 同时进入 `_internal/` 资源和 EXE 文件图标。应用图标可用现有项目环境运行 `python -B scripts/generate_app_icon.py` 从 SVG 母版确定性重建，不额外引入图像依赖。不额外收集整个 site-packages、full 依赖或系统字体。实际模块/PYZ、运行时 hook、DLL 和插件随冻结成品核验。

源码 manifest 固定 QtBase、QtSvg、PySide/Shiboken、pypdf、Fluent、frameless、darkdetect 和 pywin32 的版本、许可及哈希；原始归档随包保留。新增依赖的原许可与版权文本按实际分发内容收集，pywin32 保留多层同名许可；实际使用的 hooks-contrib 运行时 hook 保留原许可。Fluent 1.11.3 wheel/sdist 许可原文及元数据声明 GPLv3，上游另有商业用途措辞；歧义记录在第三方声明，不因此推定本项目必须购买许可或改变 GPL-3.0-only。原许可、版权和固定对应源码全部保留；只有与实际分发直接相关且现有材料无法解决的具体发行权利缺口才停止 Release，不把其他项目当成许可依据。

仅使用已授权的 `arxivkaleid-desktop` 专用 Conda 环境。构建基线为 Python `3.13.15` x64、`tzdata=2026c`、PySide6 `6.9.2`、pypdf `6.14.2`，以及固定在构建依赖文件中的 PyInstaller `6.22.3`。环境创建/安装须另获授权，构建器本身不安装软件。创建时禁用 shortcuts 和用户环境登记，将 Conda package cache 与安装 TEMP/TMP 限制在项目忽略目录；保留包缓存中的 `info/licenses/` 供构建读取。

将 `$desktopPython` 设为该专用环境 `python.exe` 的绝对路径，先核验 `sys.executable` 与 `sys.prefix`，然后执行：

```powershell
& $desktopPython -B -c "import sys; print(sys.executable); print(sys.prefix)"
./scripts/build_windows_portable.ps1 -PythonExe $desktopPython -LicenseInputManifest '.desktop-build/inputs/conda-licenses/alpha9/manifest.json'
```

构建入口按现有 spec 生成 Windows GUI one-folder；资源 allowlist 包含 `config.json`、正式 ICO、使用说明 SVG 与 Markdown、两份现行技术协议和两份默认完整研究提示词，不包含用户覆盖记录、历史提示词、领域标签模块、独立自动化策略或 Profile 文件。`BUILD_INFO.json` 的 `resource_hashes` 绑定全部允许资源，发行树检查同时复核配置内部的提示词哈希。curl `8.22.0_2` 来自 curl 官方固定 x64 归档，并核验 manifest 中的 SHA-256 后才运行。时区数据来自专用环境，不依赖系统 IANA 数据。

构建前必须在已授权范围内冻结干净提交；本地技术验证使用 `codex/` 开发分支，要求其是已核验 `origin/main` 基线的后代，不要求 HEAD 等于基线。产物为根 `dist/` 下带版本目录、`release/` 下同名 ZIP 和 `.zip.sha256`。`BUILD_INFO.json` 记录分支、基线、冻结提交、验证用途和环境版本。旧干净发行物移入本次 `.desktop-build/managed-artifacts/build-<ID>/preserved/previous-*` 留存并核对移动前后逐文件哈希；已有 `runtime/` 的发行目录拒绝覆盖。stage、缓存和重复构建副本在保存审计证据后收尾，最终输出和 previous-* 不自动清理。构建目录、vendor 下载和输出均被 Git 忽略。所有下载都先校验固定哈希，缓存不符即停止。

当前环境原包缓存位置可能已失效，构建入口支持显式许可输入 manifest，Python 入口对应 `--license-input-manifest`。准备输入只读 Conda 元数据并复制必要许可，不修改环境或原归档。首次在现有历史来源哈希已经核验且复制范围获准后执行以下命令；目标目录存在时拒绝覆盖：

```powershell
& $desktopPython -X utf8 -B scripts/portable_licenses.py --source-archive '.desktop-build/alpha10-audit/archived-release/arXivKaleid-0.1.0-alpha.9-windows-x64.zip' --source-sha256 'c48a8dba1ff58ba6bada8d2a5aea847beaefbe3ad164e59881c1430c9d4c487d' --output '.desktop-build/inputs/conda-licenses/alpha9/manifest.json'
```

manifest 记录组件 name/version/build/包归档 SHA-256、来源发行归档 SHA-256 和逐文件 SHA-256；构建重新校验来源归档、安装身份和完整许可文件集，缺失或不符即停止，不使用全局 JSON 映射。`.desktop-build/vendor/`、`.desktop-build/inputs/` 和上述 alpha 9 来源 ZIP 持续受保护。构建直接使用受跟踪入口与稳定 manifest，退役的历史包装脚本和旧许可副本不再作为构建输入。来源 ZIP 所在历史目录必须按实际文件依赖划定保留范围；其他历史内容仍须依赖复核及当次明确授权，不按目录年龄或名称自动删除。

正式构建要求分支为 `main` 且 HEAD 等于已核验的 `origin/main`，`BUILD_INFO.json` 的 `purpose` 为 `public-release`；开发分支为 `local-portable-technical-validation`。旧发行物、历史材料和受保护输入在构建、验证及失败阶段不清理。正式发布及新旧远端核验成功后，按当次分别列明的授权收口 `release/` 旧 ZIP 和与对应正式 ZIP 逐文件一致、已无构建、验证或待审用途的旧版本 `dist/`；只保留当前正式版本及仍有明确必要的成品。准确路径、完整文件集、哈希、进程退出与含 `runtime/` 目录保护见 [本地历史 portable 收口](../OPERATIONS.md#本地历史-portable-收口)，该节不清理 `.desktop-build/` 或其他材料；当前自产工作文件按 [临时产物生命周期](../OPERATIONS.md#临时产物生命周期) 收尾，两者授权范围不同。

发行物包含 GPL-3.0-only 应用 LICENSE、第三方声明、实际依赖许可证及上述八项上游源码归档，允许按 LGPL 替换动态库；这些归档用于分发材料，用户无需解包。源码 manifest 显式记录各组件许可，版本与固定运行依赖一致；gzip/xz 归档原样保留并核验 SHA-256。许可证缺失、源码入口/归档哈希不符、用户运行文件或本机个人路径进入发行树时，构建失败。ZIP 生成后逐文件重新比对哈希；构建审计目录记录模块/运行时 hook 清单及各主要组成的未压缩字节数。

发行根目录另外从 `docs/public_release/` 的固定 allowlist 复制 `README.md`、`EULA.txt`、`PRIVACY.md` 和 `SECURITY.md`；根仓库 README 不作为打包 README。`RELEASE_CHECKLIST.md` 只供开发与发布核对，不进入 ZIP。第三方声明继续从 `packaging/windows/THIRD_PARTY_NOTICES.txt` 收集，不维护第二份源文本。

根目录 LICENSE 是应用许可唯一源，单独复制到 portable 根目录。打包 README 的源码说明由构建器按用途填入：本地技术候选绑定本地冻结 commit，标明未公开发布；正式发行才生成同版本 tag 的免费源码链接，tag 提交必须等于 BUILD_INFO.json。第三方源码覆盖、独立执行平台/工具与系统库的排除理由见 THIRD_PARTY_NOTICES.txt。

## Portable 零模型验证

```powershell
& $desktopPython -X utf8 -B scripts/validate_windows_portable.py
# 仅在获准真实 arXiv 请求后执行；不会调用模型。
& $desktopPython -X utf8 -B scripts/validate_windows_portable.py --arxiv
```

验证器先检查干净发行树，再复制到本次 `.desktop-build/managed-artifacts/portable-<ID>/work/`，使用非源码 cwd 和仅包含 Windows System32 的 PATH 启动 EXE。诊断窗口创建前仅对当前诊断线程调用 [ImmDisableIME](https://learn.microsoft.com/en-us/windows/win32/api/imm/nf-imm-immdisableime)，避免第三方输入法 DLL 干扰严格来源检查；不改变系统输入法、正式 GUI 或普通重启入口，不覆盖真实输入法交互。检查资源、Qt GUI/Markdown、进度控件、首次分析告知默认拒绝、拒绝后快照未消费且分析线程未启动、DPAPI 假值、SQLite、pypdf、时区、bundled curl 与重启。`--arxiv` 通过实际 GUI/QThread 抓取候选，不点击分析按钮；诊断进程硬禁用模型入口。结果只写验证副本的 `runtime/` 和项目内验证摘要，不接触真实 Key。已有 runtime 的副本拒绝诊断。

五组 DPI、恢复、普通重启和反例全部完成，所属进程退出且合成数据隐私检查通过后，将报告与必要截图复制到本次 `evidence/` 并核对哈希，再释放九份程序副本和合成运行数据。失败保存已有安全证据；证据收集失败则保留 work 并停止后续运行。原始干净发行目录始终受保护，人工验收截图保留到明确结案，规则见根运行手册的 [临时产物生命周期](../OPERATIONS.md#临时产物生命周期)。

默认离线验证在真实 Windows 平台抓取七个主要状态、详情、失败、设置禁改与最小窗口截图，使用真实 QThread 与合成计算，不分发 unittest/mock 或开发 QA 脚本。Python 网络及非本地版本检查的子进程传输被硬禁止。新建副本写入合成 DPAPI 后，只有匹配验证器随机诊断令牌的新进程才检查恢复；普通 GUI 额外重复启动/关闭三次。图像和诊断只在副本 `runtime/work/`，绝不回流发行包。

当前机器真实 DPI 验证不设置缩放变量，记录 Qt 实测 DPR；其他 100/125/150/200% 倍率仅通过子进程 `QT_SCALE_FACTOR` 模拟，以真实 DPR 换算并校验实测结果。`validation.json` 分别标记 native/simulated；不修改全局显示设置、注册表或永久环境变量。同机隔离不能代替干净第二台 Windows 的兼容性覆盖。

多显示器下隔离诊断窗口固定到现有主屏，并记录每张截图的屏幕名称；各模式必须使用同一屏幕，避免不同原生 DPI 导致倍率换算错误。只移动诊断窗口，不改变正式 GUI 启动位置或系统显示设置。

Qt `QDomDocument.setContent` 弃用提示暂为非阻塞已知问题，以冻结 SVG 显示为验收依据。测试退出 GC 告警通过冻结程序重复窗口释放、私有内存采样、进程退出与锁释放验证；不为消除 warning 大范围重构，实际泄漏/崩溃/挂起时停止验收。窗口模式可能不输出 stderr，不能据日志为空宣称告警已消失。

发布前在已冻结 PR 提交上完成构建和本地离线验证；合并后同步干净 main，再用相同入口重建最终本地 ZIP。当前仓库没有 GitHub workflow。构建入口只写入项目内被忽略的 `dist/`、`release/` 和 `.desktop-build/`，不自动创建仓库、Release 或上传文件。任何公开 GitHub 写操作必须另行获得授权并按 [公开发布检查清单](../public_release/RELEASE_CHECKLIST.md) 执行。

## 运行目录与凭据

完整研究 Prompt 在本目录 `config/round1_research_prompt.json` 与 `round2_research_prompt.json` 中以 UTF-8 明文保存，格式为 `research_prompt_v1`。重启生效；损坏或未知格式会阻止分析，不自动覆盖。通过 GUI 确认恢复对应轮默认可修复覆盖记录，内置资源损坏则重新解压。alpha 11 解压到新目录使用新版默认，不读取或迁移 alpha 10 `runtime/`。

完整研究提示词变更须先通过源码离线测试、实际 GUI、请求哈希/缓存/预算与隐私边界验证，再冻结本地中文 commit。还须验证跨领域结果、可选自由评价、无固定阅读级别，以及改变第一轮研究顺序和评价不影响第二轮请求、缓存身份或 Token 并列裁决；两轮超过默认篇数仍接受全部合法结果，第二轮在冻结校验器、真实日报和历史中保持模型顺序。构建前核验已有 vendor 固定哈希，缺失或异常时停止，不安装依赖或下载替代材料。自动合成数据只写验证副本；分别验收五页、两轮查看/编辑、保存失败、恢复确认、离开保护、分析禁改、原生 DPI 与模拟 100/125/150/200%，并检查输入法提交中文、滚动及较大字体。提示词入口还须核对实际历史卡片外框、间隔与图标，执行整卡各区域点击及回车/空格进入，检查悬停/聚焦、最小窗口下实际放大文字的换行和裁切及中文“提示词”文案。此处输入法事件检查不等于所有第三方 IME 的实机兼容覆盖。

干净构建目录与 ZIP 必须无 runtime、测试/个人数据和用户覆盖配置，逐文件核对资源、许可、身份和哈希。技术候选的 `BUILD_INFO.json` 使用 `local-portable-technical-validation`，README 绑定冻结 commit；正式包使用 `public-release` 并绑定正式 main 与同版 tag。构建器按准确文件名把同名旧 ZIP/校验和被替换候选原样移入被忽略的构建审计目录，核对留存哈希，不删除历史材料。第一阶段交付时 `release/` 只保留当次技术候选 ZIP 与 `.sha256`；其他既有 ZIP 必须依当次明确范围原样归档保留，不能当作正式发布后的删除收口，也不能仅凭版本号认定本地包等于远端正式包。

本地技术候选交付即停止，不 push、PR、merge、tag、GitHub Release 或上传资产。正式发布需另行授权，从合并后的干净 main 重建，技术候选不可直接发布。发布及匿名核验通过后，仅以文档 PR 收口，不重建或替换已公开资产。

`.desktop-runtime/` 位于项目根目录并被 Git 忽略。源码诊断日志位于 `.desktop-runtime/logs/`；`config/secret.dat` 为当前 Windows 用户的 DPAPI 密文；`cache/arxiv/last_request_time.txt` 为请求间隔记录。候选在当前进程冻结；开始分析后将其写入本次工作库，不作为跨运行历史。

日志文件按进程创建，完整 JSON 行写入后立即 flush。GUI 的“打开日志目录”只在持久日志可用时启用；目录创建、序列化或写入失败时显示“诊断仅保留在当前会话”，原业务错误和成功状态不受影响。日志可随时在应用关闭后手工删除；分享前仍应检查内容，只提供与问题相关的文件，不要发送整个 `runtime/`。

当前分析主库为 `work/arxiv_kaleid.sqlite`，全文库为 `work/round2_inputs.sqlite`。每个新 attempt 只重置这两个已知文件及 `-journal`、`-wal`、`-shm` sidecar；不得手工删除其他运行数据。`work/analysis.lock` 使用进程文件锁，其他分析运行时安全拒绝；正常退出或进程退出均释放锁。PDF 保留在 `pdfs/<候选日期>/`，继续使用完整 arXiv ID 和 version 安全文件名。

alpha 9 历史库独立位于 `history/history.sqlite`，以明文保存当次原始日报与最小元数据，不存 Key 或提取全文工作数据，不参与工作库重置。无 Key 也可查看历史；正常零推荐保留，同日多次分别保留。确认删除只删除该条历史，不删除 PDF、日志或凭据，不提供恢复。历史不自动清理，升级 alpha 8 后不会从旧工作库补造记录。数据库损坏、未知版本或读写失败时保留原文件并单独提示；保存失败不否定分析成功，不人工重试或重新调用模型。

卡片分别显示实际抓取完成时间（北京时间）和本次论文日期，列表仍按日报完成时间倒序。当前独立历史 schema 为 v2，不迁移前一未公开 alpha 9 技术候选的 schema v1 测试历史；从该候选升级时解压到新的可写目录，不复制旧 `runtime/`，也不自动清空旧目录。

输入框失去焦点或按回车会自动加密保存，下次启动自动恢复。解密失败时重新输入；保存失败时检查项目运行目录权限。不要展示、复制或读取用户真实 Secret 来排错。

网络、XML 或分页错误后界面保留阶段、稳定代码、影响和建议，旧候选不会继续用于分析；不自动重跑。HTTP 状态与 curl exit code 在日志 details 中，不形成逐数值错误代码。抓取或分析期间需等当前工作结束后关闭窗口。

## 使用与费用

首页默认点击“抓取今天”，或通过旁边日历选择日期后点击“抓取 YYYY-MM-DD”；日期选择本身不联网。查看北京时间抓取日期和两个统计，在设置页输入有效 Key 并确保加密保存成功，再点击“开始两轮分析”。全部候选进入同一次 Round 1，安全超限时停止，不截断或拆批。两轮各最多一次 HTTP 请求，不额外 self check；全批费用上限 ¥3.00。零入围或无合格全文会明确说明 Round 2 未调用。

每次启动后的首次分析会在消费快照和创建分析线程前显示数据发送、费用和本地保存告知，默认选项为拒绝。拒绝后当前候选仍可再次决定，不会调用模型或产生模型费用。

分析失败后不会重试，也不能再次消费旧快照。缺少 Key 或保存失败时不启动分析；Token、费用或 usage 无法确认时安全停止。不要通过展示 Secret、prompt、模型原始响应或 PDF 全文排错。新分析需要重新抓取并按根规则获得相应付费授权。

## 离线验证

使用说明需实际验证五页导航、只读 Markdown、四个蓝色链接的鼠标与键盘跳转、未保存保护、最小窗口、大字体、正文滚动及分析期间查看。冻结程序增加指南加载、缺失、篡改和重启检查；仍禁止真实网络与付费调用，证据按受管生命周期留存。

隔离视觉 QA 使用已有环境运行 `python -X utf8 -B scripts/visual_qa_desktop.py`，入口为当前进程设置独立 TEMP/TMP 并在退出时恢复。该入口不启动正式业务、不读取用户 Secret、不运行真实网络或模型；内存假 Key、合成快照/事件与 SQLite 事实经过真实校验器、日报生成器、QThread 与 GUI，urllib/socket/子进程网络入口硬禁止。截图与 qa.json 保存在 `.desktop-build/managed-artifacts/visual-<ID>/evidence/`，包含初始、抓取、成功收缩/展开、分析、真实生成日报、设置/禁改、历史空态/列表/详情/零推荐、确认删除及保存失败、两轮研究要求查看/编辑和保护对话框、最小窗口和实际放大的控件字体；线程和数据库退出后收尾本次 work，截图等待明确结案。Windows 使用当前进程 `QT_QPA_PLATFORM=windows`；可分别以 `QT_SCALE_FACTOR=1` 和 `1.5` 复核不同缩放，包含日期弹层、边界月份和最小窗口日历；实际 DPR 在 qa.json 中记录，不改变持久系统设置。

在项目根目录及已有匹配依赖的环境中运行。推荐受管薄入口，独立 TEMP/TMP 和测试夹具根只作用于子进程；已有 TemporaryDirectory、addCleanup 和测试内容保持原职责：

```powershell
python -X utf8 -B scripts/run_local_checks.py --pattern 'test_desktop_governance.py'
if ($LASTEXITCODE -ne 0) { throw 'Desktop governance tests failed.' }
python -X utf8 -B scripts/run_local_checks.py
if ($LASTEXITCODE -ne 0) { throw 'Offline tests failed.' }
git check-ignore -v .desktop-runtime/logs/diagnostic-test.jsonl
git diff --check
git status --short
```

完整发现包含 Desktop 子集；只检查 Desktop 时可使用 `python -X utf8 -B scripts/run_local_checks.py --pattern 'test_desktop*.py'`，两种发现均包含治理测试。日志及测试返回码保存在运行记录的 evidence；普通证据自动保留每类最近两次成功和最近一次普通失败诊断，不因第三次运行要求人工清理。普通失败允许修复后明确再次启动；未结案故障、待审材料、中断或清理失败仍按根手册停止入口，不绕过保护和容量门槛。

新增测试使用 `.codex-validation/` 下的隔离目录，不接触实际 Desktop Secret。治理测试只读文档、JSON 配置及源码 AST；检查文档与发行资料契约，不启动应用或读取运行数据。GUI 测试自动使用 Qt offscreen；有 PySide6 与 qfluentwidgets 时必须实际执行，缺少 GUI 依赖时安全跳过 GUI 部分；pipeline 和 Secret 文件边界测试不依赖 Qt。Windows 额外运行真实 DPAPI 往返测试，仅使用合成假值；非 Windows 跳过该项。所有跳过须报告原因和未覆盖范围，涉及 GUI、DPAPI 或 Windows 发行的任务不能以跳过代替对应验收。

普通单元测试的网络全部使用 mock，不访问真实 arXiv、不调用真实 DeepSeek、不下载真实 PDF。分析测试使用真实客户端解析合成 HTTP 响应，核验单次 attempt、输入边界、自由评价及无效可选字段容错、预算、页数门控与日报事实；冻结诊断也使用合成数据执行当前技术协议校验。诊断测试覆盖身份关联、作用域、线程安全、内存降级、绝对路径与敏感内容 canary；GUI 测试覆盖展示失败不改写分析成功。构建扫描必须拒绝 `runtime/`、`logs/`、JSONL、SQLite、PDF 和 Secret。Git/GitHub 收尾遵循根 [运行手册](../OPERATIONS.md)。
