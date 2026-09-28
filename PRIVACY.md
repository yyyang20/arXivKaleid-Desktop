# 隐私说明

更新日期：2026-09-28

arXivKaleid Desktop 是在用户 Windows 电脑上运行的 portable 工具。应用没有维护者服务器中转、用户账户系统、广告或遥测。

## 网络请求

- 应用从 arXiv 读取公开论文元数据和用户选择分析的 PDF。
- 用户同意开始分析后，论文标题、摘要、分类、含内置研究边界的筛选 Prompt，以及通过门控后的 PDF 提取全文会发送给 DeepSeek API；应用不会另行发送 Research Profile 文件内容。
- DeepSeek API Key 仅用于用户自己的 API 请求，不会发送给维护者。DeepSeek 对请求数据的处理受其当前服务条款和隐私政策约束。

应用没有其他网络遥测或维护者统计上报。

## 分析前同意

每次启动应用后，第一次开始分析前会显示简短告知。拒绝告知不会消费当前候选快照，也不会调用 DeepSeek 模型。候选元数据抓取不需要 API Key。

## 本地数据

portable 模式的数据位于 `arXivKaleid.exe` 同级 `runtime/` 目录，包括：

- 经 Windows DPAPI 绑定当前 Windows 用户和电脑加密的 `runtime/config/secret.dat`；
- 候选请求间隔缓存；
- 当前分析使用的 SQLite 工作库；
- 已下载的论文 PDF 及提取后的全文工作数据。

DPAPI 密文通常不能在另一台电脑或另一个 Windows 用户下解密。不要转发已使用过的 portable 目录或 `runtime/` 内容。

## 删除数据

应用没有云端账户或维护者端数据。删除整个 portable 目录会删除本地程序、API Key 密文、PDF、SQLite 和缓存。DeepSeek 端数据的查询或删除请求需按 DeepSeek 账户和当前政策处理。

## 用户责任

用户应确保对发送给 DeepSeek 的内容拥有适用的使用权利，并遵守 arXiv、DeepSeek 及所在地区的适用规则。不应将密码、未公开稿件、受限数据或其他不应交由外部 API 处理的内容导入应用。

## 外部政策

- DeepSeek Open Platform Terms of Service: https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html
- DeepSeek Privacy Policy: https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html
- arXiv API Terms of Use: https://info.arxiv.org/help/api/tou.html
