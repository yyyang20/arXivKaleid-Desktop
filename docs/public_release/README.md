# arXivKaleid Desktop

arXivKaleid Desktop 是面向黑洞与致密天体强引力成像、偏振和新时空解研究的 Windows 论文筛选工具。

本文档说明 Windows portable 的下载和使用方式。

当前版本为 `0.1.0-alpha.5`。

## 开发方式

本项目主要采用 vibe coding 方式开发。项目维护者负责需求、产品决策、规则制定和验收；Codex 作为主要 coding agent，负责代码实现、测试和文档维护。

## 下载与校验

从 GitHub Releases 下载同一版本的两个文件：

- `arXivKaleid-<version>-windows-x64.zip`
- `arXivKaleid-<version>-windows-x64.zip.sha256`

GitHub 自动生成的 `Source code` 压缩包不是可直接运行的 Windows portable 成品。

在 PowerShell 中校验 ZIP：

```powershell
Get-FileHash -Algorithm SHA256 .\arXivKaleid-<version>-windows-x64.zip
```

输出必须与 `.zip.sha256` 中的值完全一致。

## 使用

将 ZIP 完整解压到当前用户可写目录，然后双击 `arXivKaleid.exe`。支持 Windows 10/11 x64，无需安装 Python、Conda 或 Git。

当前版本未进行代码签名，Windows SmartScreen 可能显示风险提示。请先核对下载来源和 SHA-256，不要关闭或绕过系统安全机制。

## 费用与数据

候选抓取不需要 API Key。两轮分析使用你自己的 DeepSeek API Key，可能产生费用；开始分析前应用会显示数据发送告知，只有同意后才会调用模型。

API Key 使用 Windows DPAPI 在本机加密保存。PDF、SQLite、缓存和脱敏诊断日志位于 EXE 同级 `runtime/` 目录。GUI 会显示失败阶段、稳定错误代码、影响、建议和日志关联；日志不上传，应用没有维护者服务器中转或遥测。不要转发已使用过的 portable 目录。

详细数据说明见 [PRIVACY.md](PRIVACY.md)，使用本软件前请阅读 [EULA.txt](EULA.txt)。

## 卸载

删除整个解压后的 portable 目录，即会删除程序及其本地运行数据。删除前请先自行备份需要保留的内容。

## 许可与安全

Copyright (c) 2026 yyyang20. 应用采用 **GPL-3.0-only**，完整许可文本见 portable 根目录的 `LICENSE`，说明见 [EULA.txt](EULA.txt)。本软件无保证；允许使用、研究、修改和分发，包括商业使用。分发受 GPL 覆盖的修改版本须按 GPLv3 提供相应源码，私人修改不要求公开。

本版[对应源码下载](https://github.com/yyyang20/arXivKaleid-Desktop/archive/refs/tags/v0.1.0-alpha.5.zip)固定到 `v0.1.0-alpha.5`，包含应用源码、配置、Prompt、测试、构建脚本及说明，对应 `BUILD_INFO.json` 中的提交。源码归档用于研究、修改和构建，不是可直接运行的 Windows 成品；发行下载入口为 [Alpha 5 Release](https://github.com/yyyang20/arXivKaleid-Desktop/releases/tag/v0.1.0-alpha.5)。

第三方组件保留各自许可；声明见 `THIRD_PARTY_NOTICES.txt`，许可文本位于 `licenses/`，QtBase、PySide/Shiboken 和 pypdf 对应源码位于 `licenses/sources/`。未修改的通用执行平台、独立工具和系统库的范围说明见第三方声明。alpha.1 至 alpha.4 历史发行资料不变。

安全问题报告方式见 [SECURITY.md](SECURITY.md)。不要在公开 Issue 中张贴 API Key、`secret.dat`、SQLite、PDF 或整个 `runtime/` 目录。
