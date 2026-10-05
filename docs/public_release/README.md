# arXivKaleid Desktop

arXivKaleid Desktop 是面向黑洞与致密天体强引力成像、偏振和新时空解研究的 Windows 论文筛选工具。

本文档说明 Windows portable 的下载和使用方式。

当前版本为 `0.1.0-alpha.10`，新增两轮独立研究要求管理，历史详情明确显示“生成时间”；第三方原许可、版权声明和固定对应源码随包提供。构建用途与对应提交以 `BUILD_INFO.json` 为准。

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

候选抓取不需要 API Key。首页默认“抓取今天”，可点击旁边日历选择今天及此前 365 天内的日期；选日期不会联网，随后点击抓取按钮才查询。论文按首次提交时间 `published` 换算成北京时间归属日期，同一天再次抓取可能得到更多候选；查不到就显示无候选，不查询其他日期。去重后候选全部进入同一次 Round 1，超过现有模型安全限制时停止，不截断或拆批。Key 位于设置页。两轮分析使用你自己的 DeepSeek API Key，可能产生费用；开始分析前应用会显示数据发送告知，只有同意后才会调用模型。

历史页保存完整成功运行当次实际生成的原始日报，重启后无 Key 也能查看。列表按完成时间倒序显示北京时间、候选数和最终推荐数；点击卡片查看详情，右侧删除需确认。同日多次成功分别保存，正常零推荐也保存，不自动清理或限制保存数量。失败运行不进入历史；保存失败会提示“分析成功，历史保存失败”，本次日报仍可查看，关闭后不会保留。历史正文以明文保存在 `runtime/history/history.sqlite`，删除只影响该条历史，无恢复、搜索、筛选、导出或收藏。

“提示词”页分别管理两轮研究要求：默认只读可复制，编辑后保存才生效，取消恢复最后保存值，确认恢复默认会持久替换本轮覆盖记录。离开时保护未保存修改；分析期间只可查看。仅自定义研究目标、关注/排除条件和阅读偏好，标签、数量、排序机制、输出结构和费用边界固定。文本不能空白、含 NUL 或超过 10,000 字符。

研究要求以明文保存于本 portable 的 `runtime/config/round1_research_requirements.json` 与 `round2_research_requirements.json`，重启继续生效，分析时发送给 DeepSeek。不要填写凭据或不宜外发的私人内容。损坏记录阻止分析，确认恢复对应轮默认后修复；新版本解压到新目录从其默认要求开始，不迁移旧目录。历史详情的生成时间是日报生成完成时间，与卡片抓取时间不同。

API Key 使用 Windows DPAPI 在本机加密保存。PDF、SQLite、缓存和脱敏诊断日志位于 EXE 同级 `runtime/` 目录。GUI 会显示失败阶段、稳定错误代码、影响、建议和日志关联；日志不上传，应用没有维护者服务器中转或遥测。不要转发已使用过的 portable 目录。

详细数据说明见 [PRIVACY.md](PRIVACY.md)，使用本软件前请阅读 [EULA.txt](EULA.txt)。

## 卸载

删除整个解压后的 portable 目录，即会删除程序及其本地运行数据。删除前请先自行备份需要保留的内容。

## 许可与安全

Copyright (c) 2026 yyyang20. 应用采用 **GPL-3.0-only**，完整许可文本见 portable 根目录的 `LICENSE`，说明见 [EULA.txt](EULA.txt)。本软件无保证；允许使用、研究、修改和分发，包括商业使用。分发受 GPL 覆盖的修改版本须按 GPLv3 提供相应源码，私人修改不要求公开。

{{APPLICATION_SOURCE_NOTICE}}

构建器按用途填入对应源码说明；本地技术候选不生成虚假的公开下载链接。源码归档用于研究、修改和构建，不是可直接运行的 Windows 成品；已公开成品下载见 [Releases](https://github.com/yyyang20/arXivKaleid-Desktop/releases)。

第三方组件保留各自许可；声明见 `THIRD_PARTY_NOTICES.txt`，许可文本位于 `licenses/`，QtBase、QtSvg、PySide/Shiboken、pypdf、Fluent、frameless、darkdetect 和 pywin32 固定源码位于 `licenses/sources/`。来源、版本与哈希见 `licenses/components.json`；Fluent GPLv3 元数据与上游商业用途措辞的歧义，以及通用执行平台、独立工具和系统库的范围说明见第三方声明。既有历史发行资料不变。

安全问题报告方式见 [SECURITY.md](SECURITY.md)。不要在公开 Issue 中张贴 API Key、`secret.dat`、SQLite、PDF 或整个 `runtime/` 目录。
