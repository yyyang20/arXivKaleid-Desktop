# 公开发布检查清单

本清单只用于将私人开发仓库 `yyyang20/arXivKaleid` 中已验收的 Windows portable 成品发布到公开下载仓库 [yyyang20/arXivKaleid-Desktop](https://github.com/yyyang20/arXivKaleid-Desktop)。ZIP 和 `.sha256` 不得进入任何 Git 历史。

## 冻结开发版本

- [ ] 本地分支为 `main`，HEAD 与最新 `origin/main` 一致，工作树干净。
- [ ] Desktop 版本号、README、Desktop 文档和 CHANGELOG 一致。
- [ ] 完整离线测试、Desktop 测试、Python AST 检查和 `git diff --check` 通过。
- [ ] 本次构建不读取真实 Secret、不调用 DeepSeek、不产生模型费用。

## 构建与验证

- [ ] 仅在已授权的 `arxivkaleid-desktop` 专用 Conda 环境中执行构建。
- [ ] 重新生成 `release/arXivKaleid-<version>-windows-x64.zip` 和对应 `.zip.sha256`。
- [ ] `BUILD_INFO.json` 记录预期版本、冻结提交、x64 架构且 `working_tree_clean` 为 `true`。
- [ ] ZIP 根目录包含 `README.md`、`EULA.txt`、`PRIVACY.md`、`SECURITY.md` 和 `THIRD_PARTY_NOTICES.txt`，完整第三方许可仍在 `licenses/`。
- [ ] 发行扫描未发现 `runtime/`、API Key、`secret.dat`、SQLite、PDF、日志或本机私有路径。
- [ ] 发行扫描显式拒绝诊断 JSONL；ZIP 与 `_internal/` 均不含运行日志或可写日志目录。
- [ ] source `.desktop-runtime/logs/` 由 Git ignore 覆盖，`git status --short --ignored` 只把它显示为 ignored。
- [ ] 执行 portable 零模型验证；只在另行授权后才执行真实 arXiv 网络验证。
- [ ] 本地重新计算 ZIP SHA-256，与 `.zip.sha256` 完全一致。

## 同步公开文档

- [ ] 公开仓库只同步 `README.md`、`EULA.txt`、`PRIVACY.md`、`SECURITY.md` 和 `THIRD_PARTY_NOTICES.txt`等明确 allowlist 文件。
- [ ] 前四个文件来自 `docs/public_release/`；第三方声明唯一来源是 `packaging/windows/THIRD_PARTY_NOTICES.txt`。
- [ ] 公开仓库不同步源码、prompt、profile、私有项目文档、workflow 或运行数据。
- [ ] 不在本地长期保留第二份仓库；如使用临时检出，必须限定在项目内被忽略的审计目录。

## 发布 GitHub Release

- [ ] 已获得当次 GitHub 写操作授权，并重新只读核对目标仓库、标签和现有 Release。
- [ ] 目标公开仓库已启用 immutable releases 和 private vulnerability reporting。
- [ ] 先创建 Draft Release，alpha/beta 版本标记为 prerelease。
- [ ] 只上传带版本号的 ZIP 和对应 `.zip.sha256`，不上传使用过的 portable 目录。
- [ ] 发布前核对 GitHub 返回的 asset 文件名、字节数和 SHA-256 digest。
- [ ] Release notes 包含非空的 `## 本次更新`：首个公开版本概括首次提供的主要功能，后续版本依据冻结提交、Git 差异和 `CHANGELOG` 用 1～5 条概括相对上一公开版本的主要新增、修改或修复。
- [ ] Release notes 另行保留平台、下载、未签名/SmartScreen 限制、DeepSeek 数据发送与费用、SHA-256 等适用的通用说明；这些说明不能代替“本次更新”。
- [ ] 发布 Draft 前重新读取其 Release notes，确认“本次更新”存在且非空，再发布。
- [ ] 发布后重新读取公开 Release，从未登录视角核对正文、“本次更新”、公开可见性、下载链接、Tag 和资产摘要。

## 发布后私人仓库收口

- [ ] 公开 Release 及未登录核验成功后，继续在同一公开发布任务中按文档路由更新私人仓库 `docs/CHANGELOG.md`；只有首次发布或稳定下载入口变化时才同时更新根目录 `README.md`。
- [ ] 发布后文档收口发生在冻结成品之后，不重建或替换已发布资产，不改写既有 immutable Release，也不拆成新的独立任务。
- [ ] 公开仓库写操作授权不自动覆盖私人仓库的 Git commit/push；尚未取得后者授权时，先完成本地文档修改和验证，明确报告待提交状态，不得因此省略文档或宣称整个公开发布任务已经完成。
- [ ] 对文档变更执行适用的离线测试和 `git diff --check`，并确认 ZIP、`.sha256`、测试或审计产物没有进入 Git。

## 失败处理

- [ ] 任一哈希、版本、提交、文件集合或扫描结果不一致时停止发布。
- [ ] 不替换已发布的 ZIP、标签或校验值；需要修复时发布新版本。
- [ ] 失败后不自动重跑网络验证、不上传部分成品、不调用 DeepSeek。
