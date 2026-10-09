# 公开发布检查清单

本清单用于将当前 Desktop 仓库中已验收的 Windows portable 成品发布到本仓库 GitHub Releases。ZIP 和 `.sha256` 不得进入 Git 历史。

## 冻结开发版本

- [ ] 本地分支为 `main`，HEAD 与最新 `origin/main` 一致，工作树干净。
- [ ] Desktop 版本号、README、Desktop 文档和 CHANGELOG 一致。
- [ ] 完整离线测试、Desktop 测试、Python AST 检查和 `git diff --check` 通过。
- [ ] 已核对测试跳过原因和未覆盖范围；GUI、Windows DPAPI 测试在具备依赖的 Windows 环境实际执行，治理静态检查不代替成品验证。
- [ ] 本次构建不读取真实 Secret、不调用 DeepSeek、不产生模型费用。

## 构建与验证

- [ ] 当前 Fluent 1.11.3 许可原文、版权、wheel/sdist 元数据和上游说明已核对，商业用途措辞歧义已记录；不因商业许可或措辞本身推定必须购买许可或改变应用 GPL-3.0-only。仅发现与本次实际分发直接相关且现有 LICENSE/NOTICE/对应源码方案无法解决的具体发行权利缺口时停止 Release；新增组件的原许可、版权、版本和源码哈希已核验。
- [ ] 冻结程序实际显示 Fluent SVG 图标、五个导航页面、运行/收缩/失败详情和 Markdown；验证 DPI/缩放、DPAPI 假值与 pywin32 DLL/hooks，并记录包体积变化。
- [ ] 使用说明正文和书本图标与源码资源一致；只读复制、滚动、大字体、四个内部链接、未保存保护及重启验证通过，缺失或篡改资源安全拒绝。
- [ ] 已移除 portable README 的发行准备提示；本版 tag/源码与实际发布身份一致，不能把未发布入口当作可用下载。

- [ ] 仅在已授权的 `arxivkaleid-desktop` 专用 Conda 环境中执行构建。
- [ ] 重新生成 `release/arXivKaleid-<version>-windows-x64.zip` 和对应 `.zip.sha256`。
- [ ] `BUILD_INFO.json` 记录预期版本、冻结提交、x64 架构且 `working_tree_clean` 为 `true`。
- [ ] ZIP 根目录包含 `LICENSE`、`README.md`、`EULA.txt`、`PRIVACY.md`、`SECURITY.md` 和 `THIRD_PARTY_NOTICES.txt`，完整第三方许可仍在 `licenses/`。
- [ ] 应用许可为 `GPL-3.0-only`；LICENSE 与根维护源字节一致，EULA 不增加额外限制。
- [ ] portable README 的对应源码入口固定到本版 tag，tag 提交与 BUILD_INFO.json 一致，不使用浮动 main。
- [ ] QtBase、QtSvg、PySide/Shiboken、pypdf、Fluent、frameless、darkdetect、pywin32 对应源码归档完整；许可、版本及 SHA-256 与源码 manifest 一致，独立工具/执行平台及系统库的排除理由已核对。
- [ ] 发行扫描未发现 `runtime/`、API Key、`secret.dat`、SQLite、PDF、日志或本机私有路径。
- [ ] 发行扫描显式拒绝诊断 JSONL；ZIP 与 `_internal/` 均不含运行日志或可写日志目录。
- [ ] source `.desktop-runtime/logs/` 由 Git ignore 覆盖，`git status --short --ignored` 只把它显示为 ignored。
- [ ] 执行 portable 零模型验证；只在另行授权后才执行真实 arXiv 网络验证。
- [ ] 本地重新计算 ZIP SHA-256，与 `.zip.sha256` 完全一致。

## 发行文档核对

- [ ] portable 根目录的 `README.md`、`EULA.txt`、`PRIVACY.md`、`SECURITY.md` 来自 `docs/public_release/` 固定 allowlist；根仓库 README 不作为打包 README。
- [ ] 第三方声明唯一来源是 `packaging/windows/THIRD_PARTY_NOTICES.txt`。
- [ ] 应用 LICENSE 唯一来源是仓库根目录，不在 docs/public_release/ 重复维护。
- [ ] 根 EULA、PRIVACY、SECURITY 与 `docs/public_release/` 维护源保持字节一致，根第三方声明与 packaging 维护源一致；两份用途不同的 README 分别核对。
- [ ] `RELEASE_CHECKLIST.md` 不进入 portable ZIP，发行物不含用户运行数据。

## 发布 GitHub Release

- [ ] 本次发行说明已按根目录 [公开维护文本语言](../../AGENTS.md#公开维护文本语言) 核对。
- [ ] 已获得当次 GitHub 写操作授权，并重新只读核对目标仓库、标签和现有 Release。
- [ ] 保存并对比全部既有历史 tag、Release 正文、资产 id/名称/字节数/digest 基线，既有历史发行保持不变。
- [ ] 目标公开仓库已启用 immutable releases 和 private vulnerability reporting。
- [ ] 先创建 Draft Release，alpha/beta 版本标记为 prerelease。
- [ ] 只上传带版本号的 ZIP 和对应 `.zip.sha256`，不上传使用过的 portable 目录。
- [ ] 发布前核对 GitHub 返回的 asset 文件名、字节数和 SHA-256 digest。
- [ ] Release notes 包含非空的 `## 本次更新`：首个公开版本概括首次提供的主要功能，后续版本依据冻结提交、Git 差异和 `CHANGELOG` 用 1～5 条概括相对上一公开版本的主要新增、修改或修复。
- [ ] Release notes 另行保留平台、下载、未签名/SmartScreen 限制、DeepSeek 数据发送与费用、SHA-256 等适用的通用说明；这些说明不能代替“本次更新”。
- [ ] Release notes 包含 GPL-3.0-only 及精确 tag 对应源码下载入口；免费源码包含应用、配置、Prompt、测试、构建脚本及说明。
- [ ] 发布 Draft 前重新读取其 Release notes，确认“本次更新”存在且非空，再发布。
- [ ] 发布后重新读取公开 Release，从未登录视角核对正文、“本次更新”、公开可见性、下载链接、Tag 和资产摘要。
- [ ] 未登录下载源码并逐文件与发行冻结提交核对，同时重新计算公开 ZIP SHA-256，不能仅凭页面或静态测试宣称源码和成品一致。

## 发布后文档收口

- [ ] 公开 Release 及未登录核验成功后，继续在同一公开发布任务中按文档路由更新 `docs/CHANGELOG.md`；只有首次发布或稳定下载入口变化时才同时更新根目录 `README.md`。
- [ ] 发布后文档收口发生在冻结成品之后，不重建或替换已发布资产，不改写既有 immutable Release，也不拆成新的独立任务。
- [ ] Git commit/push 必须获得相应授权；尚未取得授权时，先完成本地文档修改和验证，明确报告待提交状态，不得因此省略文档或宣称整个公开发布任务已经完成。
- [ ] 对文档变更执行适用的离线测试和 `git diff --check`，并确认 ZIP、`.sha256`、测试或审计产物没有进入 Git。
- [ ] 分别记录最终 main 与发行 tag SHA；两者的差异只包含发布后文档收口及其必要治理测试，已发布资产保持不变。

## 本地历史 portable 收口

- [ ] 本节的不得清理要求限定旧发行物和历史材料；本次登记工作文件的收尾按生命周期执行，不改变本节授权及正式发布后远端核验门槛。
- [ ] 已按 [运行手册](../OPERATIONS.md#本地历史-portable-收口) 将精确旧文件删除纳入当次授权，分别列明旧 ZIP、校验文件和旧版本 `dist/` 成品目录；仅授权清理 `release/` 不包含 `dist/`。构建、验证和失败阶段不得提前清理，已授权时不重复确认。
- [ ] 正式新版本 Release 成功发布，新资产、tag、BUILD_INFO、公开源码及远端资产核验全部通过。
- [ ] 从未登录视角下载全部待清理版本的正式 ZIP 和 `.sha256`，核对名称、字节数、GitHub digest、实际 SHA-256、校验内容及 tag/BUILD_INFO，确认历史资产完整且历史发行保持不变。
- [ ] 明确列出准确文件名，核验绝对路径、目录链接与项目边界，逐文件删除，不使用通配符或递归删除；未知版本、非 portable 文件和未明确确认的本地技术构建均保留并停止清理。
- [ ] 本地 `release/` 只保留当前最新正式版本的 ZIP 和 `.sha256`；旧正式版本由 GitHub Releases 作为历史存档。记录最终文件列表。
- [ ] 旧版本 `dist/` 仅在新旧远端核验全部通过后收口，不由构建器或临时产物轮换自动删除；确认是早于当前正式版本的已发布成品、不再用于构建、验证或待审，所属进程、线程及数据库连接退出。
- [ ] 检查旧目录完整路径和文件名；含 `runtime/`、Secret、数据库、PDF 或日志的目录保留，不读取这些用户数据。未知版本、技术候选、链接、越界或归属不明均保留并停止清理，技术构建例外不适用于 `dist/`。
- [ ] 旧目录名、BUILD_INFO 的版本与提交和对应历史 tag 一致；完整文件集和逐文件 SHA-256 与已核验的对应远端正式 ZIP 一致，遗漏文件、额外文件或目录、哈希及身份不符均停止。
- [ ] 授权清单保存准确绝对路径、完整文件与目录清单、逐文件哈希、对应资产摘要和保留理由，执行前重新核验，任何变化都停止；只逐文件删除，再由深到浅移除已核验的空目录，禁止删除 `dist/` 父目录或使用通配符、`Remove-Item -Recurse`。删除失败立即停止，保存已删除与残留清单，不自动重试。
- [ ] `dist/` 只保留当前最新正式版本及仍有明确必要的成品；逐项记录额外保留目录的用途和待完成事项，后续发布再次复核，并记录最终文件及目录列表。
- [ ] 不删除或修改 GitHub 历史 Release、tag、源码或资产，不清理 `.desktop-build/`、源码材料、审计材料、运行数据；`preserved/previous-*`、vendor、许可输入与来源归档均不在本步骤范围。
- [ ] 任一测试、构建、发行身份、远端核验或历史资产完整性异常时停止后续发布或删除，不绕过。

## 失败处理

- [ ] 任一哈希、版本、提交、文件集合或扫描结果不一致时停止发布。
- [ ] 不替换已发布的 ZIP、标签或校验值；需要修复时发布新版本。
- [ ] 失败后不自动重跑网络验证、不上传部分成品、不调用 DeepSeek。
- [ ] 使用受管构建和验证入口；受保护 vendor、稳定许可 manifest、来源归档和旧成品哈希已登记，许可输入缺失或不匹配即停止。
- [ ] 按 [临时产物生命周期](../OPERATIONS.md#临时产物生命周期) 保存摘要、清单、哈希和截图后，所属进程退出再收尾本次自产工作副本；人工验收证据保留到明确结案，清理失败单独报告并停止后续生成。
- [ ] 普通证据按生命周期自动轮换每类最近两次成功和最近一次普通失败诊断；历史、未结案故障及待审材料继续保护，普通失败不授予自动重跑网络或业务的权限。

## 匿名只读核验入口

`scripts/verify_public_release.py` 参数指定 `--repo`、`--tag`、`--commit`、`--zip`、`--checksum` 和 `--history-baseline`。只在当次授权的公开核验范围内访问 GitHub；不使用登录凭据、不上传、不发布、不删除远端资产、不自动重试。运行登记并保护本地资产和基线，保存 `evidence/release.json` 与发行身份摘要，收尾本次下载副本。该入口不授予本地历史 portable 删除权限，也不代替上述人工检查。

基线为 `history_snapshot(repo, tag)` 的 JSON：`releases` 保存排除目标 tag 后各 Release 的 id、tag_name、name、body、draft、prerelease、immutable 及全部 assets 的 id/name/size/digest；`tags` 保存排除目标 tag 的 refs/tags/ 到对象 SHA 映射。发布前取得并保存在项目内忽略目录；核验前后都必须与该基线一致。旧审计基线格式不自动转换，原历史脚本和记录保持原样。

```powershell
& $desktopPython -X utf8 -B scripts/verify_public_release.py --repo 'yyyang20/arXivKaleid-Desktop' --tag 'v<version>' --commit '<冻结提交40位SHA>' --zip 'release/arXivKaleid-<version>-windows-x64.zip' --checksum 'release/arXivKaleid-<version>-windows-x64.zip.sha256' --history-baseline '.desktop-build/<本次审计>/history-baseline.json'
```
