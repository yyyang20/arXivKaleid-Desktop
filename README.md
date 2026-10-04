# arXivKaleid Desktop

arXivKaleid Desktop 是面向黑洞与致密天体强引力成像、偏振和新时空解研究的 Windows 论文筛选工具。

本仓库包含 Desktop 应用源码、测试、构建配置和项目文档；Windows portable 成品通过 GitHub Releases 提供。

当前版本为 `0.1.0-alpha.8`，支持选择北京时间首次提交日期，并将全部去重候选放入同一次 Round 1；保留现有模型安全限制。alpha 8 尚未公开发布，本地 portable 技术候选以 `BUILD_INFO.json` 中的冻结提交和用途为准。最新公开成品为 [alpha 7.1 Windows x64 portable](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.7.1)，下载 ZIP 和同名校验文件后按下述方式核验。

## 开发方式

本项目主要采用 vibe coding 方式开发。项目维护者负责需求、产品决策、规则制定和验收；Codex 作为主要 coding agent，负责代码实现、测试和文档维护。

## 下载与校验

从 GitHub Releases 下载同一版本的两个文件：

- `arXivKaleid-<version>-windows-x64.zip`
- `arXivKaleid-<version>-windows-x64.zip.sha256`

GitHub 自动生成的 `Source code` 压缩包包含源码，不是可直接运行的 Windows portable 成品。

在 PowerShell 中校验 ZIP：

```powershell
Get-FileHash -Algorithm SHA256 .\arXivKaleid-<version>-windows-x64.zip
```

输出必须与 `.zip.sha256` 中的值完全一致。

## 使用

将 ZIP 完整解压到当前用户可写目录，然后双击 `arXivKaleid.exe`。支持 Windows 10/11 x64，无需安装 Python、Conda 或 Git。

当前版本未进行代码签名，Windows SmartScreen 可能显示风险提示。请先核对下载来源和 SHA-256，不要关闭或绕过系统安全机制。

## 费用与数据

候选抓取不需要 API Key。首页默认“抓取今天”，旁边日历可选择今天及此前 365 天的日期；选择后点击主按钮才开始抓取，只查询所选日期，不自动回退。论文按 `published` 首次提交时间换算成北京时间归属日期，同日稍后重新抓取可能得到更多候选。Key 位于设置页，“开始两轮分析”仍是独立入口。两轮分析使用你自己的 DeepSeek API Key，可能产生费用；开始分析前应用会显示数据发送告知，只有同意后才会调用模型。

API Key 使用 Windows DPAPI 在本机加密保存。PDF、SQLite、缓存和脱敏诊断日志位于 EXE 同级 `runtime/` 目录。GUI 会显示失败阶段、稳定错误代码、影响、建议和日志关联；日志不上传，应用没有维护者服务器中转或遥测。不要转发已使用过的 portable 目录。

详细数据说明见 [PRIVACY.md](PRIVACY.md)，使用本软件前请阅读 [EULA.txt](EULA.txt)。

## 卸载

删除整个解压后的 portable 目录，即会删除程序及其本地运行数据。删除前请先自行备份需要保留的内容。

## 许可与安全

Copyright (c) 2026 yyyang20. 自有应用源码、测试、构建脚本、配置、提示词和项目文档采用 **GPL-3.0-only**，完整文本见 [LICENSE](LICENSE)，许可说明见 [EULA.txt](EULA.txt)。本软件无保证；允许使用、研究、修改和分发，包括商业使用。分发受 GPL 覆盖的衍生版本须继续按 GPLv3 提供相应源码，私人修改不要求公开。

公开 alpha 7.1 的[对应源码归档](https://github.com/yyyang20/arXivKaleid-Desktop/archive/refs/tags/v0.1.0-alpha.7.1.zip)包含源码、资源、测试及构建说明；对应 [Windows portable Release](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.7.1) 的 `BUILD_INFO.json` 与 tag 均指向发行提交 `8ad5826861d1490545dbec068ba0451343bccb39`。匿名下载成品及源码已核验，不要用浮动 main 代替已发行版本源码；发布后的 main 可以包含文档收口提交。

第三方组件保留各自许可，声明见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)。完整许可文本及实际分发库的固定对应源码随 portable ZIP 提供，清单见 `licenses/components.json`。本版许可不改写既有历史发行资料。

alpha 6 新增 Fluent、frameless、darkdetect 和 pywin32。Fluent 1.11.3 的 wheel/sdist 原许可及元数据声明 GPLv3，上游另有商业用途措辞；歧义记录在第三方声明，不因此推定本项目必须购买商业许可或改变 GPL-3.0-only。第三方原许可、版权及固定对应源码随包保留，自有应用许可不代替第三方许可。

安全问题报告方式见 [SECURITY.md](SECURITY.md)。不要在公开 Issue 中张贴 API Key、`secret.dat`、SQLite、PDF 或整个 `runtime/` 目录。

## 源码运行与项目文档

在项目根目录、已具备 `requirements-desktop.txt` 依赖的 Windows Python 环境中运行：

```powershell
python -B -m desktop.app
```

运行数据位于被 Git 忽略的 `.desktop-runtime/`。依赖、构建与验证方式见 [Desktop 运行手册](docs/desktop/DESKTOP_OPERATIONS.md)；项目说明从 [文档索引](docs/README.md) 进入。
