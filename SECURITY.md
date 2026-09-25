# 安全政策

## 支持范围

只对 GitHub Releases 中最新发布的 Windows x64 portable 版本提供安全修复。旧版本和第三方转发、修改的压缩包不在支持范围内。

## 报告漏洞

请优先使用本公开仓库 Security 页面的 `Report a vulnerability` 私密报告功能。如果该入口不可用，只在不包含利用细节或敏感数据的前提下创建公开 Issue，请求维护者开启私密联系方式。

报告应包含受影响版本、Windows 版本、风险影响和最小复现步骤。请先移除用户名、本机路径、论文全文和其他不必要内容。

## 不得公开的内容

不要在 Issue、讨论、截图或日志中发布：

- DeepSeek API Key 或其他凭据；
- `runtime/config/secret.dat`；
- SQLite、PDF、提取全文或整个 `runtime/` 目录；
- 包含本机路径或个人数据的完整错误现场。

若怀疑 API Key 已泄漏，请立即在 DeepSeek 账户中撤销或轮换，不要等待软件修复。

## 完整性校验

仅从本仓库 GitHub Releases 下载带版本号的 portable ZIP，并在运行前核对同一 Release 中的 SHA-256。SHA-256 用于校验下载完整性，不等同于代码签名。
