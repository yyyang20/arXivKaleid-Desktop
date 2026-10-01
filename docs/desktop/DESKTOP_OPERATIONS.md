# Desktop 运行与验证

本文档维护源码启动、依赖、本机构建、portable 验证及运行排错细节。开发与公开发布流程见根 [运行手册](../OPERATIONS.md)，安全、授权和完成要求以根 [AGENTS.md](../../AGENTS.md) 为准，阅读与更新路由见 [文档索引](../README.md)。

## 源码启动

在项目根目录、已经具备依赖的 Windows Python 环境中运行：

```powershell
python -B -m desktop.app
```

当前版本为 `0.1.0-alpha.6`，使用 PySide6 / Qt `6.9.2`、PySide6-Fluent-Widgets `1.11.3` 基础版、PySideSix-Frameless-Window `0.8.2`、darkdetect `0.8.0`、Windows pywin32 `312`，均固定在 [requirements-desktop.txt](../../requirements-desktop.txt)。不安装 full 的 scipy、pillow、colorthief。该文件同时引入 [requirements.txt](../../requirements.txt) 中的 pypdf 核心运行依赖；[requirements-build.txt](../../requirements-build.txt) 单独固定 PyInstaller 及构建依赖。

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

发行后解压 `arXivKaleid-<version>-windows-x64.zip` 到当前用户可写目录，双击 `arXivKaleid.exe`；不需要 Python、Conda、PySide6、pypdf 或 Git。Alpha 6 正式发行必须从合并后的 main 重建并验收，不能用 Alpha 5 或开发分支技术构建代替。源码 GUI 在设置页输入自己的 DeepSeek API Key，首页先获取候选，再执行分析。

所有用户数据都在 EXE 同级 `runtime/`，诊断日志位于 `runtime/logs/`。删除整个 portable 目录相当于卸载并删除运行数据；当前版本不自动轮换或删除旧日志。不要把包含个人 `runtime/` 的已使用目录重新发给其他人，应发送构建生成的干净 ZIP。DPAPI 绑定 Windows 用户与电脑，复制文件夹给另一个用户/电脑后，旧 `secret.dat` 通常无法解密，需要重新输入 Key。

第一版只支持 Windows 10/11 x64，未代码签名，可能触发 SmartScreen；不绕过系统安全机制。没有安装器、自动更新或 ARM64 版本。

## 本机构建

Alpha 6 收集 Qt Core/Gui/Widgets/Svg/SvgWidgets/Xml 和 Windows 平台、Windows 11 样式、SVG 图标插件；不带 QtNetwork、qsvg 图像插件或 offscreen 平台。Fluent 图标/QSS 由其内嵌 Qt 资源模块提供，不从网络获取图标；不额外收集整个 site-packages、full 依赖或系统字体。实际模块/PYZ、运行时 hook、DLL 和插件随冻结成品核验。

源码 manifest 固定 QtBase、QtSvg、PySide/Shiboken、pypdf、Fluent、frameless、darkdetect 和 pywin32 的版本、许可及哈希；原始归档随包保留。新增依赖的原许可与版权文本按实际分发内容收集，pywin32 保留多层同名许可；实际使用的 hooks-contrib 运行时 hook 保留原许可。Fluent 1.11.3 wheel/sdist 许可原文及元数据声明 GPLv3，上游另有商业用途措辞；歧义记录在第三方声明，不因此推定本项目必须购买许可或改变 GPL-3.0-only。原许可、版权和固定对应源码全部保留；只有与实际分发直接相关且现有材料无法解决的具体发行权利缺口才停止 Release，不把其他项目当成许可依据。

仅使用已授权的 `arxivkaleid-desktop` 专用 Conda 环境。构建基线为 Python `3.13.15` x64、`tzdata=2026c`、PySide6 `6.9.2`、pypdf `6.14.2`，以及固定在构建依赖文件中的 PyInstaller `6.22.3`。环境创建/安装须另获授权，构建器本身不安装软件。创建时禁用 shortcuts 和用户环境登记，将 Conda package cache 与安装 TEMP/TMP 限制在项目忽略目录；保留包缓存中的 `info/licenses/` 供构建读取。

将 `$desktopPython` 设为该专用环境 `python.exe` 的绝对路径，先核验 `sys.executable` 与 `sys.prefix`，然后执行：

```powershell
& $desktopPython -B -c "import sys; print(sys.executable); print(sys.prefix)"
./scripts/build_windows_portable.ps1 -PythonExe $desktopPython
```

构建入口按现有 spec 生成 Windows GUI one-folder；资源 allowlist 仅包含 `config.json` 和两份现行自包含 Prompt，不包含独立自动化策略或 Profile 文件。curl `8.22.0_2` 来自 curl 官方固定 x64 归档，并核验 manifest 中的 SHA-256 后才运行。时区数据来自专用环境，不依赖系统 IANA 数据。

构建前必须在已授权范围内冻结干净提交；本地技术验证使用 `codex/` 开发分支，要求其是已核验 `origin/main` 基线的后代，不要求 HEAD 等于基线。产物为根 `dist/` 下带版本目录、`release/` 下同名 ZIP 和 `.zip.sha256`。`BUILD_INFO.json` 记录分支、基线、冻结提交、验证用途和环境版本。旧干净发行物移入本次 `.desktop-build/build-*/` 留存；已有 `runtime/` 的发行目录拒绝覆盖。构建目录、vendor 下载和输出均被 Git 忽略。所有下载都先校验固定哈希，缓存不符即停止。

正式构建要求分支为 `main` 且 HEAD 等于已核验的 `origin/main`，`BUILD_INFO.json` 的 `purpose` 为 `public-release`；开发分支为 `local-portable-technical-validation`。构建和验证阶段的材料留存不等于发布后永久保留 `release/` 旧 ZIP；正式发布及远端核验成功后的精确清理见 [本地历史 portable 收口](../OPERATIONS.md#本地历史-portable-收口)，不清理 `.desktop-build/` 或其他材料。

发行物包含 GPL-3.0-only 应用 LICENSE、第三方声明、实际依赖许可证及上述八项上游源码归档，允许按 LGPL 替换动态库；这些归档用于分发材料，用户无需解包。源码 manifest 显式记录各组件许可，版本与固定运行依赖一致；gzip/xz 归档原样保留并核验 SHA-256。许可证缺失、源码入口/归档哈希不符、用户运行文件或本机个人路径进入发行树时，构建失败。ZIP 生成后逐文件重新比对哈希；构建审计目录记录模块/运行时 hook 清单及各主要组成的未压缩字节数。

发行根目录另外从 `docs/public_release/` 的固定 allowlist 复制 `README.md`、`EULA.txt`、`PRIVACY.md` 和 `SECURITY.md`；根仓库 README 不作为打包 README。`RELEASE_CHECKLIST.md` 只供开发与发布核对，不进入 ZIP。第三方声明继续从 `packaging/windows/THIRD_PARTY_NOTICES.txt` 收集，不维护第二份源文本。

根目录 LICENSE 是应用许可唯一源，单独复制到 portable 根目录。应用对应源码通过 README 与 Release 正文指向同一版本 tag 的免费源码归档提供，包含配置、Prompt 和构建说明；源码 tag 的提交必须等于 BUILD_INFO.json 的冻结提交。第三方源码覆盖、独立执行平台/工具与系统库的排除理由见 THIRD_PARTY_NOTICES.txt；不能仅因某组件随包分发就要求全部构建环境源码。

## Portable 零模型验证

```powershell
& $desktopPython -X utf8 -B scripts/validate_windows_portable.py
# 仅在获准真实 arXiv 请求后执行；不会调用模型。
& $desktopPython -X utf8 -B scripts/validate_windows_portable.py --arxiv
```

验证器先检查干净发行树，再复制到项目内新的 `.desktop-build/validation-*/`，使用非源码 cwd 和仅包含 Windows System32 的 PATH 启动 EXE。检查资源、Qt GUI/Markdown、进度控件、首次分析告知默认拒绝、拒绝后快照未消费且分析线程未启动、DPAPI 假值、SQLite、pypdf、时区、bundled curl 与重启。`--arxiv` 通过实际 GUI/QThread 抓取候选，不点击分析按钮；诊断进程硬禁用模型入口。结果只写验证副本的 `runtime/` 和项目内验证摘要，不接触真实 Key。已有 runtime 的副本拒绝诊断。

默认离线验证在真实 Windows 平台抓取七个主要状态、详情、失败、设置禁改与最小窗口截图，使用真实 QThread 与合成计算，不分发 unittest/mock 或开发 QA 脚本。Python 网络及非本地版本检查的子进程传输被硬禁止。新建副本写入合成 DPAPI 后，只有匹配验证器随机诊断令牌的新进程才检查恢复；普通 GUI 额外重复启动/关闭三次。图像和诊断只在副本 `runtime/work/`，绝不回流发行包。

当前机器真实 DPI 验证不设置缩放变量，记录 Qt 实测 DPR；其他 100/125/150/200% 倍率仅通过子进程 `QT_SCALE_FACTOR` 模拟，以真实 DPR 换算并校验实测结果。`validation.json` 分别标记 native/simulated；不修改全局显示设置、注册表或永久环境变量。同机隔离不能代替干净第二台 Windows 的兼容性覆盖。

多显示器下隔离诊断窗口固定到现有主屏，并记录每张截图的屏幕名称；各模式必须使用同一屏幕，避免不同原生 DPI 导致倍率换算错误。只移动诊断窗口，不改变正式 GUI 启动位置或系统显示设置。

Qt `QDomDocument.setContent` 弃用提示暂为非阻塞已知问题，以冻结 SVG 显示为验收依据。测试退出 GC 告警通过冻结程序重复窗口释放、私有内存采样、进程退出与锁释放验证；不为消除 warning 大范围重构，实际泄漏/崩溃/挂起时停止验收。窗口模式可能不输出 stderr，不能据日志为空宣称告警已消失。

发布前在已冻结 PR 提交上完成构建和本地离线验证；合并后同步干净 main，再用相同入口重建最终本地 ZIP。当前仓库没有 GitHub workflow。构建入口只写入项目内被忽略的 `dist/`、`release/` 和 `.desktop-build/`，不自动创建仓库、Release 或上传文件。任何公开 GitHub 写操作必须另行获得授权并按 [公开发布检查清单](../public_release/RELEASE_CHECKLIST.md) 执行。

## 运行目录与凭据

`.desktop-runtime/` 位于项目根目录并被 Git 忽略。源码诊断日志位于 `.desktop-runtime/logs/`；`config/secret.dat` 为当前 Windows 用户的 DPAPI 密文；`cache/arxiv/last_request_time.txt` 为请求间隔记录。候选在当前进程冻结；开始分析后将其写入本次工作库，不作为跨运行历史。

日志文件按进程创建，完整 JSON 行写入后立即 flush。GUI 的“打开日志目录”只在持久日志可用时启用；目录创建、序列化或写入失败时显示“诊断仅保留在当前会话”，原业务错误和成功状态不受影响。日志可随时在应用关闭后手工删除；分享前仍应检查内容，只提供与问题相关的文件，不要发送整个 `runtime/`。

当前分析主库为 `work/arxiv_kaleid.sqlite`，全文库为 `work/round2_inputs.sqlite`。每个新 attempt 只重置这两个已知文件及 `-journal`、`-wal`、`-shm` sidecar；不得手工删除其他运行数据。`work/analysis.lock` 使用进程文件锁，其他分析运行时安全拒绝；正常退出或进程退出均释放锁。PDF 保留在 `pdfs/<候选日期>/`，继续使用完整 arXiv ID 和 version 安全文件名。

输入框失去焦点或按回车会自动加密保存，下次启动自动恢复。解密失败时重新输入；保存失败时检查项目运行目录权限。不要展示、复制或读取用户真实 Secret 来排错。

网络、XML 或分页错误后界面保留阶段、稳定代码、影响和建议，旧候选不会继续用于分析；不自动重跑。HTTP 状态与 curl exit code 在日志 details 中，不形成逐数值错误代码。抓取或分析期间需等当前工作结束后关闭窗口。

## 使用与费用

先在首页点击“获取最新候选”，查看冻结日期与数量；在设置页输入有效 Key 并确保加密保存成功，再回首页点击“开始两轮分析”。两轮各最多一次 HTTP 请求，不额外 self check；全批费用上限 ¥3.00。正常结果在窗口中渲染显示，零入围或无合格全文会明确说明 Round 2 未调用。

每次启动后的首次分析会在消费快照和创建分析线程前显示数据发送、费用和本地保存告知，默认选项为拒绝。拒绝后当前候选仍可再次决定，不会调用模型或产生模型费用。

分析失败后不会重试，也不能再次消费旧快照。缺少 Key 或保存失败时不启动分析；Token、费用或 usage 无法确认时安全停止。不要通过展示 Secret、prompt、模型原始响应或 PDF 全文排错。新分析需要重新抓取并按根规则获得相应付费授权。

## 离线验证

隔离视觉 QA 使用已有环境运行 `python -X utf8 -B scripts/visual_qa_desktop.py`，执行前按下述方式限制 TEMP/TMP。该入口不调用正式 main、不读取用户 Secret、不运行真实候选或分析；内存假 Key、合成快照/事件/Markdown 驱动真实 QThread 与 GUI，urllib/socket/子进程网络入口硬禁止。截图与 qa.json 写入 `.codex-validation/alpha6-visual-qa/capture-*/`，包含初始、抓取、成功收缩/展开、分析、日报、设置/禁改、历史、最小窗口及失败。Windows 使用当前进程 `QT_QPA_PLATFORM=windows`；可分别以 `QT_SCALE_FACTOR=1` 和 `1.5` 复核不同缩放，实际 DPR 在 qa.json 中记录，不改变持久系统设置。

在项目根目录及已有匹配依赖的环境中运行；临时目录只作用于当前 PowerShell 进程及其子进程，结束后恢复原值，不修改用户或系统的持久设置。先使用已有路径检查函数确认临时目录仍在项目内且没有目录链接：

```powershell
python -X utf8 -B -c "from pathlib import Path; from desktop.paths import checked_path; print(checked_path(Path.cwd(), '.codex-validation', 'test-temp'))"
if ($LASTEXITCODE -ne 0) { throw 'Test temporary path is unsafe.' }
$desktopTestTemp = Join-Path (Get-Location) '.codex-validation/test-temp'
New-Item -ItemType Directory -Force -Path $desktopTestTemp | Out-Null
$desktopOldTemp = $env:TEMP
$desktopOldTmp = $env:TMP
try {
    $env:TEMP = $desktopTestTemp
    $env:TMP = $desktopTestTemp
    python -X utf8 -B -m unittest discover -s tests -p 'test_desktop_governance.py' -v
    if ($LASTEXITCODE -ne 0) { throw 'Desktop governance tests failed.' }
    python -X utf8 -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Offline tests failed.' }
} finally {
    $env:TEMP = $desktopOldTemp
    $env:TMP = $desktopOldTmp
}
git check-ignore -v .desktop-runtime/logs/diagnostic-test.jsonl
git diff --check
git status --short
```

完整发现包含 Desktop 子集；只检查 Desktop 时可将完整发现命令替换为 `python -X utf8 -B -m unittest discover -s tests -p 'test_desktop*.py' -v`，两种发现均包含治理测试。

新增测试使用 `.codex-validation/` 下的隔离目录，不接触实际 Desktop Secret。治理测试只读文档、JSON 配置及源码 AST；检查文档与发行资料契约，不启动应用或读取运行数据。GUI 测试自动使用 Qt offscreen；有 PySide6 与 qfluentwidgets 时必须实际执行，缺少 GUI 依赖时安全跳过 GUI 部分；pipeline 和 Secret 文件边界测试不依赖 Qt。Windows 额外运行真实 DPAPI 往返测试，仅使用合成假值；非 Windows 跳过该项。所有跳过须报告原因和未覆盖范围，涉及 GUI、DPAPI 或 Windows 发行的任务不能以跳过代替对应验收。

普通单元测试的网络全部使用 mock，不访问真实 arXiv、不调用真实 DeepSeek、不下载真实 PDF。分析测试使用真实客户端解析合成 HTTP 响应，核验单次 attempt、冻结顺序、预算、页数门控与日报事实；诊断测试覆盖身份关联、作用域、线程安全、内存降级、绝对路径与敏感内容 canary；GUI 测试覆盖展示失败不改写分析成功。构建扫描必须拒绝 `runtime/`、`logs/`、JSONL、SQLite、PDF 和 Secret。Git/GitHub 收尾遵循根 [运行手册](../OPERATIONS.md)。
