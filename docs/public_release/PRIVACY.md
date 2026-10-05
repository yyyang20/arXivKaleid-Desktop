# 隐私说明

更新日期：2026-10-05

arXivKaleid Desktop 是在用户 Windows 电脑上运行的 portable 工具。应用没有维护者服务器中转、用户账户系统、广告或遥测。

## 网络请求

- 应用从 arXiv 读取公开论文元数据和用户选择分析的 PDF。
- 用户同意开始分析后，论文标题、摘要、分类、固定筛选协议、已保存生效的默认或自定义研究要求，以及通过门控后的 PDF 提取全文会发送给 DeepSeek API；未保存草稿不会发送，应用不会另行发送 Research Profile 文件内容。
- DeepSeek API Key 仅用于用户自己的 API 请求，不会发送给维护者。DeepSeek 对请求数据的处理受其当前服务条款和隐私政策约束。

应用没有其他网络遥测或维护者统计上报。

## 分析前同意

每次启动应用后，第一次开始分析前会显示简短告知。拒绝告知不会消费当前候选快照，也不会调用 DeepSeek 模型。候选元数据抓取不需要 API Key。

## 本地数据

portable 模式的数据位于 `arXivKaleid.exe` 同级 `runtime/` 目录，包括：

- 经 Windows DPAPI 绑定当前 Windows 用户和电脑加密的 `runtime/config/secret.dat`；
- 两轮独立 UTF-8 明文研究要求记录 `runtime/config/round1_research_requirements.json` 和 `round2_research_requirements.json`；不加密、不跨目录迁移，确认恢复默认替换对应覆盖记录；
- 候选请求间隔缓存；
- 当前分析使用的 SQLite 工作库；
- 独立 `runtime/history/history.sqlite` 中以明文保存的成功日报原始 Markdown、日报完成时间、实际抓取完成时间、论文日期及候选/推荐数量；正文可能包含论文标题、作者、摘要、推荐理由、费用与本地 PDF 相对路径，但不保存 API Key 或模型原始响应；
- 已下载的论文 PDF 及提取后的全文工作数据。
- `runtime/logs/` 下按应用会话生成的脱敏 JSONL 诊断，包括阶段、稳定代码、耗时、计数、关联 ID 和安全网络字段。

诊断日志不保存 API Key、Authorization、Cookie、DPAPI 密文、完整 Prompt、研究要求正文、标题、摘要、论文全文、逐页文本、模型请求或原始响应、response ID、curl 原始 stderr、HTTP 原始正文或未知响应头、环境变量全集、命令行全集、traceback locals 或用户绝对路径。未预见异常的 traceback 只保留模块、函数和行号。工作库审计只保存请求哈希等安全字段，不存原始研究要求；模型生成的推荐理由可能复述用户输入，仍会随日报保存。

DPAPI 密文通常不能在另一台电脑或另一个 Windows 用户下解密。不要转发已使用过的 portable 目录或 `runtime/` 内容。

## 删除数据

应用没有云端账户或维护者端数据。当前版本不自动删除历史诊断日志；可在应用关闭后删除 `runtime/logs/` 中不再需要的文件。删除整个 portable 目录会删除本地程序、API Key 密文、PDF、SQLite、缓存和日志。DeepSeek 端数据的查询或删除请求需按 DeepSeek 账户和当前政策处理。

日报历史不自动清理、不设置数量上限。用户可在历史列表确认后删除指定记录，无回收站或恢复功能；只删除该条历史元数据和正文，不删除 PDF、工作库、日志或 Key。SQLite 删除不是安全擦除，不承诺不可恢复的物理销毁。历史保存失败不改变已经完成的分析，本次日报仅在当前会话可查看；没有人工重试保存或崩溃恢复。

## 用户责任

用户应确保对发送给 DeepSeek 的内容拥有适用的使用权利，并遵守 arXiv、DeepSeek 及所在地区的适用规则。不应将密码、未公开稿件、受限数据或其他不应交由外部 API 处理的内容导入应用。

## 外部政策

- DeepSeek Open Platform Terms of Service: https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html
- DeepSeek Privacy Policy: https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html
- arXiv API Terms of Use: https://info.arxiv.org/help/api/tou.html
