# Desktop 运行与验证

当前开发版本为 `0.1.0-alpha.14`，尚未公开发布，按单次构建流程准备人工验收。已公开下载为 alpha 13 Windows x64 portable，下载与运行方式见下文。

本文档维护源码启动、依赖、本机构建、portable 验证及运行排错细节。开发与公开发布流程见根 [运行手册](../OPERATIONS.md)，安全、授权和完成要求以根 [AGENTS.md](../../AGENTS.md) 为准，阅读与更新路由见 [文档索引](../README.md)。

## 源码启动

在项目根目录、已经具备依赖的 Windows Python 环境中运行：

```powershell
python -B -m desktop.app
```

开发版使用 PySide6 / Qt `6.9.2`、PySide6-Fluent-Widgets `1.11.3` 基础版、PySideSix-Frameless-Window `0.8.2`、darkdetect `0.8.0`、Windows pywin32 `312`，均固定在 [requirements-desktop.txt](../../requirements-desktop.txt)。不安装 full 的 scipy、pillow、colorthief。该文件同时引入 [requirements.txt](../../requirements.txt) 中的 pypdf 核心运行依赖；[requirements-build.txt](../../requirements-build.txt) 单独固定 PyInstaller 及构建依赖。

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

解压 `arXivKaleid-<version>-windows-x64.zip` 到当前用户可写目录，双击 `arXivKaleid.exe`；不需要 Python、Conda、PySide6、pypdf 或 Git。当前公开成品为 [alpha 13](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.13)，从合并后的干净 main 重建，tag 与 `BUILD_INFO.json` 的发行提交一致，已完成零模型 portable 验证及匿名成品、源码核验。具体发行身份与验证记录见 [CHANGELOG](../CHANGELOG.md)。在设置页输入自己的 DeepSeek API Key，按“使用说明”设置并保存两轮提示词，首页先获取候选，再执行分析。

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

构建前必须在已授权范围内冻结干净最终提交 C；可使用 `codex/` 开发分支，要求 C 是已核验 `origin/main` 基线的后代，不要求 HEAD 等于基线。源码 GUI 验证在冻结前完成，最终 portable 验证在构建后完成。产物为根 `dist/` 下带版本目录、`release/` 下唯一同名 ZIP 和 `.zip.sha256`。`BUILD_INFO.json` 记录实际分支、基线、C、正式用途和环境版本。进程锁内检查已有输出：相同 C 或仅有允许的非打包文档差异时原样复用，不再次执行 PyInstaller；输出和记录异常即停止。确需新内容构建时，旧干净发行物移入本次 `.desktop-build/managed-artifacts/build-<ID>/preserved/previous-*` 留存并核对移动前后逐文件哈希；已有 `runtime/` 的发行目录拒绝覆盖。stage、缓存和重复构建副本在保存审计证据后收尾，最终输出和未取得技术副本清理资格的 previous-* 不自动清理。今后明确登记的旧技术副本仅在替代新包 ZIP 完整性、逐文件哈希校验和完整 portable 验证全部通过后按根运行手册自动清理，ZIP、SHA-256 和审计继续保留。构建目录、vendor 下载和输出均被 Git 忽略。所有下载都先校验固定哈希，缓存不符即停止。

当前环境原包缓存位置可能已失效，构建入口支持显式许可输入 manifest，Python 入口对应 `--license-input-manifest`。准备输入只读 Conda 元数据并复制必要许可，不修改环境或原归档。首次在现有历史来源哈希已经核验且复制范围获准后执行以下命令；目标目录存在时拒绝覆盖：

```powershell
& $desktopPython -X utf8 -B scripts/portable_licenses.py --source-archive '.desktop-build/alpha10-audit/archived-release/arXivKaleid-0.1.0-alpha.9-windows-x64.zip' --source-sha256 'c48a8dba1ff58ba6bada8d2a5aea847beaefbe3ad164e59881c1430c9d4c487d' --output '.desktop-build/inputs/conda-licenses/alpha9/manifest.json'
```

manifest 记录组件 name/version/build/包归档 SHA-256、来源发行归档 SHA-256 和逐文件 SHA-256；构建重新校验来源归档、安装身份和完整许可文件集，缺失或不符即停止，不使用全局 JSON 映射。`.desktop-build/vendor/`、`.desktop-build/inputs/` 和上述 alpha 9 来源 ZIP 持续受保护。构建直接使用受跟踪入口与稳定 manifest，退役的历史包装脚本和旧许可副本不再作为构建输入。来源 ZIP 所在历史目录必须按实际文件依赖划定保留范围；其他历史内容仍须依赖复核及当次明确授权，不按目录年龄或名称自动删除。

最终包统一记录 `purpose=public-release`、`release_workflow=single-build-v1`，实际构建分支可以是开发分支；用途不表示已公开，不在验收或合并后修改 BUILD_INFO。旧用途兼容读取，仅用于保留历史事实及原清理策略，不批量迁移。旧发行物、历史材料和受保护输入在构建、验证及失败阶段不清理。正式发布及新旧远端核验成功后，按当次分别列明的授权收口 `release/` 旧 ZIP 和与对应正式 ZIP 逐文件一致、已无构建、验证或待审用途的旧版本 `dist/`；只保留当前正式版本及仍有明确必要的成品。准确路径、完整文件集、哈希、进程退出与含 `runtime/` 目录保护见 [本地历史 portable 收口](../OPERATIONS.md#本地历史-portable-收口)，该节不清理 `.desktop-build/` 或其他材料；当前自产工作文件按 [临时产物生命周期](../OPERATIONS.md#临时产物生命周期) 收尾，两者授权范围不同。

发行物包含 GPL-3.0-only 应用 LICENSE、第三方声明、实际依赖许可证及上述八项上游源码归档，允许按 LGPL 替换动态库；这些归档用于分发材料，用户无需解包。源码 manifest 显式记录各组件许可，版本与固定运行依赖一致；gzip/xz 归档原样保留并核验 SHA-256。许可证缺失、源码入口/归档哈希不符、用户运行文件或本机个人路径进入发行树时，构建失败。ZIP 生成后逐文件重新比对哈希；构建审计目录记录模块/运行时 hook 清单及各主要组成的未压缩字节数。

发行根目录另外从 `docs/public_release/` 的固定 allowlist 复制 `README.md`、`EULA.txt`、`PRIVACY.md` 和 `SECURITY.md`；根仓库 README 不作为打包 README。`RELEASE_CHECKLIST.md` 只供开发与发布核对，不进入 ZIP。第三方声明继续从 `packaging/windows/THIRD_PARTY_NOTICES.txt` 收集，不维护第二份源文本。

根目录 LICENSE 是应用许可唯一源，单独复制到 portable 根目录。最终待发布包首次构建即生成同版本 tag 的免费源码链接，并明确发布前入口尚未开放；人工验收通过后原样发布，tag 提交必须等于 BUILD_INFO.json 的 C。旧技术包的本地源码说明继续兼容核验，不重写历史包。第三方源码覆盖、独立执行平台/工具与系统库的排除理由见 THIRD_PARTY_NOTICES.txt。

## Portable 零模型验证

alpha 13 源码冻结前，还须运行 `scripts/validate_physics_categories.py`：在独立受管 runtime 核对官方 Physics 51 个 ID、名称与归属，逐类执行轻量真实 API 查询，再通过真实 GUI/QThread 验收多个 Archive 的今天和历史完整抓取、分类组合及快照冻结。轻量探针不是完整抓取，历史验收须有非空更新版本；所有入口硬禁止模型，失败不自动重跑。离线回归明确验证 V1/V2/V3 的首次／最新日期与已有中间日关联语义。

若前述验证在 51 类探针及 17 组完整抓取均已通过后中断，先按受管清单保留证据、获得当次授权并结案中断。可用 `--continue-evidence '<已结案运行>/evidence/physics-validation.json'` 仅补做尚未执行的真实历史关联刷新；入口核验原目录及全部前置结果，输出绑定原证据 SHA-256 的合并报告，不重跑已通过网络用例，也不将原中断记录改称成功。

冻结 portable 的五组 DPI 验证包含分类窗口、选择边界、恢复确认、保存失败、坏配置修复和跨进程分类恢复；真实网络验证继续使用全新副本和现行请求／性能门槛。全部自动验证通过后，仅将当次明确列明的其他旧 `release/` ZIP 与校验文件原样归档保留，交付唯一最终待发布包及校验文件供人工验收；此时不执行 GitHub 写操作，也不宣称已公开。

```powershell
& $desktopPython -X utf8 -B scripts/validate_windows_portable.py
# 仅在获准真实 arXiv 请求后执行；不会调用模型。
& $desktopPython -X utf8 -B scripts/validate_windows_portable.py --arxiv
# 历史日期须在 GUI 365 天范围内；诊断仍硬禁止模型，额外核验精确版本 PDF。
& $desktopPython -X utf8 -B scripts/validate_windows_portable.py --arxiv --arxiv-date '2026-08-03'
# 冻结前：真实 GUI/QThread 验证首次／最新提交日、历史补查及每次 120 秒性能门槛。
& $desktopPython -X utf8 -B scripts/validate_arxiv_history.py
```

验证器先检查干净发行树，再复制到本次 `.desktop-build/managed-artifacts/portable-<ID>/work/`，使用非源码 cwd 和仅包含 Windows System32 的 PATH 启动 EXE。诊断窗口创建前仅对当前诊断线程调用 [ImmDisableIME](https://learn.microsoft.com/en-us/windows/win32/api/imm/nf-imm-immdisableime)，避免第三方输入法 DLL 干扰严格来源检查；不改变系统输入法、正式 GUI 或普通重启入口，不覆盖真实输入法交互。检查资源、Qt GUI/Markdown、进度控件、首次告知默认拒绝、DPAPI 假值、SQLite、pypdf、时区、bundled curl 与重启；独立版本元数据库用合成记录验证跨进程内容哈希恢复。`--arxiv` 通过实际 GUI/QThread 完整抓取所查分类，不点击分析；`--arxiv-date` 可选择范围内历史日期，并检验当前版本元数据与明确版本 PDF 最终 URL 和 SHA-256。历史日期验收须包含更新版本，零结果不能替代成功验证。诊断进程硬禁用模型，所有结果只写新副本 runtime 和受管摘要，不接触真实 Key，已有 runtime 拒绝诊断。

`validate_arxiv_history.py` 在独立受管 runtime 检查当天、近期及数月前日期的冷查询，记录各来源分页、请求数和完整抓取耗时，单次验收不得超过 120 秒，失败不自动重跑。固定多版本论文用于最新版与 PDF 校验；历史关联夹具来自真实指定旧版 Atom，明确标注为合成关联，不冒充过去实际抓取。补查必须取得联网最新版并保留原记录、快照；验收全程禁止 OAI 和模型。Advanced Search 页面变化、服务及网络延迟可导致验收失败，不能截断结果满足性能门槛。

五组 DPI、恢复、普通重启和反例全部完成，所属进程退出且合成数据隐私检查通过后，将报告、必要截图及脱敏会话 JSONL 复制到本次 `evidence/` 并核对哈希，再释放九份程序副本和合成运行数据。失败保存已有安全证据；证据收集失败则保留 work 并停止后续运行。原始干净发行目录始终受保护，人工验收截图保留到明确结案，规则见根运行手册的 [临时产物生命周期](../OPERATIONS.md#临时产物生命周期)。

`BUILD_INFO.json` 的 `build_run_id` 将 portable 验证绑定到具体构建运行 ID、冻结 commit、ZIP SHA-256 和完整文件清单摘要。新包全部自动校验通过且验证正常收尾后，自动清理符合条件的旧技术候选解压副本，无需人工确认，验证仍保留待人工验收状态及截图。人工验收仅用于取得原包发布资格；用户检查界面与操作、确认合格并关闭程序后，执行以下仅记录发布人工验收结案的命令，同时绑定 `.sha256` 文件的字节哈希。它不推送 Git、不发布、不删除截图，也不解锁副本清理：

```powershell
& $desktopPython -X utf8 -B scripts/local_artifacts.py close-review --run-id '<portable-ID>'
```

验证失败、五组 DPI／恢复／反例未完成、证据不符、包已变动或进程未退出均拒绝结案。旧技术副本必须有新策略登记，与保留 ZIP 完整一致，且无用户数据、链接或有效使用引用；正式旧包、未标记历史记录及其他 previous-* 不自动删除。有效引用消失后，在下一次受管入口的容量检查前复核清理，不启动后台任务。ZIP 和审计仍占空间；删除异常停止入口，须人工处理。旧构建无新绑定仍可验证，但不会自动解锁技术副本清理。

默认离线验证在真实 Windows 平台抓取七个主要状态、详情、失败、设置禁改与最小窗口截图，使用真实 QThread 与合成计算，不分发 unittest/mock 或开发 QA 脚本。Python 网络及非本地版本检查的子进程传输被硬禁止。新建副本写入合成 DPAPI 后，只有匹配验证器随机诊断令牌的新进程才检查恢复；普通 GUI 额外重复启动/关闭三次。图像和诊断只在副本 `runtime/work/`，绝不回流发行包。

当前机器真实 DPI 验证不设置缩放变量，记录 Qt 实测 DPR；其他 100/125/150/200% 倍率仅通过子进程 `QT_SCALE_FACTOR` 模拟，以真实 DPR 换算并校验实测结果。`validation.json` 分别标记 native/simulated；不修改全局显示设置、注册表或永久环境变量。同机隔离不能代替干净第二台 Windows 的兼容性覆盖。

多显示器下隔离视觉验证窗口优先固定到物理分辨率较低的屏幕，并记录每张截图的屏幕名称；各模式必须使用同一屏幕，避免不同原生 DPI 导致倍率换算错误。只移动诊断窗口，不改变正式 GUI 启动位置或系统显示设置。

Qt `QDomDocument.setContent` 弃用提示暂为非阻塞已知问题，以冻结 SVG 显示为验收依据。测试退出 GC 告警通过冻结程序重复窗口释放、私有内存采样、进程退出与锁释放验证；不为消除 warning 大范围重构，实际泄漏/崩溃/挂起时停止验收。窗口模式可能不输出 stderr，不能据日志为空宣称告警已消失。

人工验收通过后，不再构建 portable；通过一个普通 merge commit 的 PR 合并包含 C 的代码和发行文档，保留 C 的 SHA。合并提交 M 只允许根手册规定、由脚本共享名单核验的非打包维护文档差异。同步合并结果并执行 `verify_public_release.py --preflight --review-run-id <portable-ID>`，tag 指向 C，上传已验收的同一份 ZIP 和 `.sha256`。当前仓库没有 GitHub workflow。构建入口只写入项目内被忽略的 `dist/`、`release/` 和 `.desktop-build/`，不自动创建仓库、Release 或上传文件。任何公开 GitHub 写操作必须另行获得授权并按 [公开发布检查清单](../public_release/RELEASE_CHECKLIST.md) 执行。

## 运行目录与凭据

完整研究 Prompt 在本目录 `config/round1_research_prompt.json` 与 `round2_research_prompt.json` 中以 UTF-8 明文保存，格式为 `research_prompt_v1`。重启生效；损坏或未知格式会阻止分析，不自动覆盖。通过 GUI 确认恢复对应轮默认可修复覆盖记录，内置资源损坏则重新解压。新版本解压到新目录使用其默认提示词，不自动读取或迁移旧目录 `runtime/`。

完整研究提示词变更须先通过源码离线测试、实际 GUI、请求哈希/缓存/Token 预算与隐私边界验证，再冻结本地中文 commit。还须验证跨领域结果、可选自由评价、无固定阅读级别，以及改变第一轮研究顺序和评价不影响第二轮请求、缓存身份或 Token 并列裁决；两轮超过默认篇数仍接受全部合法结果，第二轮在冻结校验器、真实日报和历史中保持模型顺序。构建前核验已有 vendor 固定哈希，缺失或异常时停止，不安装依赖或下载替代材料。自动合成数据只写验证副本；分别验收五页、两轮查看/编辑、保存失败、恢复确认、离开保护、分析禁改、原生 DPI 与模拟 100/125/150/200%，并检查输入法提交中文、滚动及较大字体。提示词入口还须核对实际历史卡片外框、间隔与图标，执行整卡各区域点击及回车/空格进入，检查悬停/聚焦、最小窗口下实际放大文字的换行和裁切及中文“提示词”文案。此处输入法事件检查不等于所有第三方 IME 的实机兼容覆盖。

干净构建目录与 ZIP 必须无 runtime、测试/个人数据和用户覆盖配置，逐文件核对资源、许可、身份和哈希。最终待发布包和正式上传包是同一份 `public-release` 成品，BUILD_INFO 及同版 tag 均绑定 C，branch 保留实际构建分支。确有新内容构建时，构建器按准确文件名把同名旧 ZIP/校验和被替换候选原样移入被忽略的构建审计目录，核对留存哈希，不删除历史材料。交付时 `release/` 只保留当次待发布 ZIP 与 `.sha256`；其他既有 ZIP 必须依当次明确范围原样归档保留，不能当作正式发布后的删除收口，也不能仅凭版本号认定本地包等于远端正式包。

待发布包交付即停止，不 push、PR、merge、tag、GitHub Release 或上传资产。正式发布需另行授权，人工通过后按上述原包流程执行。若人工验收发现问题并需要修改，不记录批准；修复后完成相关验证和文档，冻结新 C，重新构建、自动验证并人工验收，不增加拒绝命令或永久拒绝状态。C 和验收文件未变时不得重建；发布后事实与最终哈希保存于 Release 正文及受管审计，不追加发布状态文档 PR，不重建或替换已公开资产。

`.desktop-runtime/` 位于项目根目录并被 Git 忽略。源码诊断日志位于 `.desktop-runtime/logs/`；`config/secret.dat` 为当前 Windows 用户的 DPAPI 密文；`cache/arxiv/last_request_time.txt` 为请求间隔记录。成功候选先事务保存到 `metadata/arxiv_versions.sqlite`（schema v1），再在当前进程冻结；分析将快照复制到本次工作库。重复抓取仍联网刷新；未知 schema、损坏、哈希冲突及保存失败保留原库并阻止本批分析，不从旧工作库或日报导入候选。

日志文件按进程创建，完整 JSON 行写入后立即 flush。GUI 的“打开日志目录”只在持久日志可用时启用；目录创建、序列化或写入失败时显示“诊断仅保留在当前会话”，原业务错误和成功状态不受影响。日志可随时在应用关闭后手工删除；分享前仍应检查内容，只提供与问题相关的文件，不要发送整个 `runtime/`。

当前分析主库为 `work/arxiv_kaleid.sqlite`，全文库为 `work/round2_inputs.sqlite`。每个新 attempt 只重置这两个已知文件及 `-journal`、`-wal`、`-shm` sidecar；不得手工删除其他运行数据。`work/analysis.lock` 使用进程文件锁，其他分析运行时安全拒绝；正常退出或进程退出均释放锁。新 PDF 位于 `pdfs/version-evidence-v1/<候选日期>/`，来源证据与 SHA-256 成功校验才复用；旧 PDF 保留。版本元数据库与日报历史不参与重置。

alpha 9 历史库独立位于 `history/history.sqlite`，以明文保存当次原始日报与最小元数据，不存 Key 或提取全文工作数据，不参与工作库重置。无 Key 也可查看历史；正常零推荐保留，同日多次分别保留。确认删除只删除该条历史，不删除 PDF、日志或凭据，不提供恢复。历史不自动清理，升级 alpha 8 后不会从旧工作库补造记录。数据库损坏、未知版本或读写失败时保留原文件并单独提示；保存失败不否定分析成功，不人工重试或重新调用模型。

卡片分别显示实际抓取完成时间（北京时间）和本次论文日期，列表仍按日报完成时间倒序。当前独立历史 schema 为 v2，不迁移前一未公开 alpha 9 技术候选的 schema v1 测试历史；从该候选升级时解压到新的可写目录，不复制旧 `runtime/`，也不自动清空旧目录。

输入框失去焦点或按回车会自动加密保存，下次启动自动恢复。解密失败时重新输入；保存失败时检查项目运行目录权限。不要展示、复制或读取用户真实 Secret 来排错。

网络、XML 或分页错误后界面保留阶段、稳定代码、影响和建议，旧候选不会继续用于分析；不自动重跑。HTTP 状态与 curl exit code 在日志 details 中，不形成逐数值错误代码。抓取或分析期间需等当前工作结束后关闭窗口。

## 使用与资源限制

首页默认点击“抓取今天”，或通过旁边日历选择日期后点击“抓取 YYYY-MM-DD”；日期选择本身不联网。查看北京时间抓取日期和两个统计，在设置页输入有效 Key 并确保加密保存成功，再点击“开始两轮分析”。全部候选进入同一次 Round 1，安全超限时停止，不截断或拆批。两轮各最多一次 HTTP 请求，不额外 self check。零入围或无合格全文会明确说明 Round 2 未调用。

每次启动后的首次分析会在消费快照和创建分析线程前显示数据发送和本地保存告知，默认选项为拒绝。拒绝后当前候选仍可再次决定，不会调用模型。

分析失败后不会重试，也不能再次消费旧快照。缺少 Key 或保存失败时不启动分析；Token 预检失败或 usage 无法确认时安全停止。不要通过展示 Secret、prompt、模型原始响应或 PDF 全文排错。新分析需要重新抓取并按根规则获得相应模型授权。

## 离线验证

使用说明需实际验证五页导航、只读 Markdown、四个蓝色链接的鼠标与键盘跳转、未保存保护、最小窗口、大字体、正文滚动及分析期间查看。冻结程序增加指南加载、缺失、篡改和重启检查；仍禁止真实网络与模型调用，证据按受管生命周期留存。

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

完整发现包含 Desktop 子集；只检查 Desktop 时可使用 `python -X utf8 -B scripts/run_local_checks.py --pattern 'test_desktop*.py'`，两种发现均包含治理测试。日志及测试返回码保存在运行记录的 evidence；普通证据自动保留每类最近两次成功和最近一次普通失败诊断，不因第三次运行要求人工清理。普通失败允许修复后明确再次启动；未结案故障、待审材料、中断或清理失败仍按根手册停止入口，仅完整自动验证通过的 portable 待发布人工验收记录不阻止后续技术验证，证据保护和容量门槛不变。

新增测试使用 `.codex-validation/` 下的隔离目录，不接触实际 Desktop Secret。治理测试只读文档、JSON 配置及源码 AST；检查文档与发行资料契约，不启动应用或读取运行数据。GUI 测试自动使用 Qt offscreen；有 PySide6 与 qfluentwidgets 时必须实际执行，缺少 GUI 依赖时安全跳过 GUI 部分；pipeline 和 Secret 文件边界测试不依赖 Qt。Windows 额外运行真实 DPAPI 往返测试，仅使用合成假值；非 Windows 跳过该项。所有跳过须报告原因和未覆盖范围，涉及 GUI、DPAPI 或 Windows 发行的任务不能以跳过代替对应验收。

普通单元测试的网络全部使用 mock，不访问真实 arXiv、不调用真实 DeepSeek、不下载真实 PDF。分析测试使用真实客户端解析合成 HTTP 响应，核验单次 attempt、输入边界、自由评价及无效可选字段容错、Token 预算、页数门控与日报事实；冻结诊断也使用合成数据执行当前技术协议校验。诊断测试覆盖身份关联、作用域、线程安全、内存降级、绝对路径与敏感内容 canary；GUI 测试覆盖展示失败不改写分析成功。构建扫描必须拒绝 `runtime/`、`logs/`、JSONL、SQLite、PDF 和 Secret。Git/GitHub 收尾遵循根 [运行手册](../OPERATIONS.md)。
