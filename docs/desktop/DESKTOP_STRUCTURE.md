# Desktop 文件结构

本文档维护 Desktop 包内文件及 runtime 数据细节；整体目录与核心模块职责见根 [项目结构](../PROJECT_STRUCTURE.md)，分工以 [文档索引](../README.md) 为准。

| 路径 | 当前职责 |
|---|---|
| `desktop/AGENTS.md` | Desktop 局部协作规则 |
| `desktop/__init__.py` | 包入口和 Desktop 版本 |
| `desktop/config.py` | Desktop 只读配置契约、预算和 Prompt 资源哈希校验 |
| `desktop/paths.py` | application/resource/runtime 根、路径安全、时区与 bundled curl 校验 |
| `desktop/errors.py` | 稳定错误和 outcome、GUI 文案及按实际影响范围判定 paper/system 的纯模型 |
| `desktop/diagnostics.py` | session/operation/snapshot/fetch/run 关联、脱敏 JSONL、安全 traceback、计时与内存降级 |
| `desktop/app.py` | QApplication、导航、首次告知、Key 保存、抓取/分析/历史 QThread、成功后独立保存历史与生命周期 |
| `desktop/pages.py` | 首页、设置页与历史页入口；抓取信息卡、密码框、About 和 Markdown/空态布局 |
| `desktop/history.py` | 无 Qt 的独立历史 schema v2、双时间及论文日期、事务保存、元数据游标分页、原始正文读取和删除 |
| `desktop/history_page.py` | 历史列表模型与卡片委托、主体/删除命中区、列表/详情切换与 Markdown 展示 |
| `desktop/date_picker.py` | 首页 Qt 月历弹层、365 天范围、月份导航限制、今天标记和屏幕内定位 |
| `desktop/style.py` | alpha 7 三页共享灰蓝调色板、输入框样式与低对比度卡片绘制 |
| `desktop/task_panel.py` | 可复用任务状态组件；运行展开、成功收缩、失败诊断、真实事件与快照/结果详情 |
| `desktop/analysis.py` | 一次性快照分析、工作库重置和锁、两轮/PDF/全文编排、累计费用预检、结构化进度与安全结果 |
| `desktop/progress.py` | 抓取与分析共用的不可变结构化进度事件及不影响业务流程的安全派发 |
| `desktop/report.py` | 当前 run 的 SQLite 事实与 Desktop Markdown 外壳，复用 Round 2 推荐区块 |
| `desktop/pipeline.py` | 固定候选规则、现有 submittedDate 核心调用、真实日期/分类进度、runtime 路径约束和只读内存快照 |
| `desktop/secrets.py` | ctypes DPAPI、密文原子保存与恢复，独立于 Qt |
| `requirements-desktop.txt` | 固定 PySide6、基础 Fluent、frameless、darkdetect 和 Windows pywin32 依赖 |
| `requirements-build.txt` | 固定 PyInstaller 和仅构建阶段依赖 |
| `packaging/windows/` | 正式 one-folder spec、冻结入口、零模型诊断、curl/source manifest、第三方声明源文件 |
| `scripts/build_windows_portable.ps1` | 显式 Python 解释器的 Windows 构建入口 |
| `docs/public_release/` | portable 打包 README、应用 EULA、隐私与安全说明的维护源及发布检查清单；不保存 ZIP |
| `scripts/build_windows_portable.py` | 资源与公开文档 allowlist、vendor 校验、构建、发行扫描、ZIP 和 SHA-256 |
| `scripts/portable_licenses.py` | 从实际分发组件及固定 gzip/xz 源码收集原许可，保留八项源码归档与哈希 |
| `scripts/validate_windows_portable.py` | 全新副本的脱离开发环境验证和重启 |
| `scripts/visual_qa_desktop.py` | 禁止网络/模型、内存假 Key、合成业务事件驱动的真实 Qt 截图；产物仅在项目忽略目录 |
| `scripts/generate_app_icon.py` | 使用现有 PySide6 从 A0 SVG 母版确定性生成 16～256 px Windows ICO 与可选验收预览 |
| `assets/app-icon.svg`、`assets/app-icon.ico` | 透明画布 A0 矢量母版及 Qt/EXE 共用的正式多尺寸图标 |
| `packaging/windows/portable_visual.py` | 冻结诊断专用合成 QThread 状态、真实截图、DLL 来源和窗口生命周期验证，不进入正式业务流程 |
| `tests/test_portable_diagnostic.py` | Windows 合成 DPAPI 的独立进程完整诊断路径、后置检查失败与已用 runtime 拒绝回归 |
| `tests/test_desktop_portable.py` | source/frozen 路径、链接、可写性、bundled curl 与工作库边界 |
| `tests/test_desktop_build.py` | checksum、x64、发行数据排除、资源及 manifest 契约 |
| `tests/test_desktop_pipeline.py` | 假 Atom 集成、日期边界、计数、排序、冻结及禁止分析入口 |
| `tests/test_desktop_secrets.py` | 密文落盘、错误、路径和 Windows DPAPI 假值往返 |
| `tests/test_desktop_app.py` | 可选 offscreen GUI 状态、首次分析告知拒绝边界、线程及凭据交互 |
| `tests/test_desktop_analysis.py` | mock HTTP 两轮集成、门控、预算、一次性分析、工作数据隔离和日报事实 |
| `tests/test_desktop_history.py` | 原始正文、跨进程、同日多次、游标排序、事务故障、锁、schema 与路径边界 |
| `tests/test_desktop_diagnostics.py` | JSONL schema、关联身份、作用域、线程安全、内存降级和隐私 canary |
| `tests/test_desktop_config.py` | 当前配置、预算、协议和 Prompt 哈希契约 |
| `tests/test_desktop_decoupling.py` | alpha.4 行为基线、schema v1、缓存、预算和独立源码运行 |
| `tests/test_desktop_governance.py` | 根与局部治理、文档路由、当前身份、链接、发行资料副本与发布清单的只读检查 |

portable 根目录包含直接来自仓库根的 GPLv3 `LICENSE`。应用源码说明按用途绑定 `BUILD_INFO.json`：技术候选引用本地冻结 commit，正式发行引用同提交的版本 tag；第三方源码归档位于 `licenses/sources/`，身份和 SHA-256 由 `packaging/windows/source-manifest.json` 固定，原许可不改写。

## 当前运行数据

以下为源码模式；portable 模式将 `.desktop-runtime/` 替换为 EXE 同级 `runtime/`，其余结构相同。`_internal/` 只保存应用资源和运行依赖。`dist/`、`release/`、`build/`、`.desktop-build/` 为被忽略的本机构建产物，不进入 Git。

```text
.desktop-runtime/
├─ config/
│  └─ secret.dat
├─ cache/
│  └─ arxiv/
│     └─ last_request_time.txt
├─ logs/
│  └─ desktop-<UTC>-<session_id>.jsonl
├─ history/
│  └─ history.sqlite
├─ work/
│  ├─ analysis.lock
│  ├─ arxiv_kaleid.sqlite
│  └─ round2_inputs.sqlite
└─ pdfs/
   └─ <候选日期>/
```

Secret 保存过程中可暂存同目录下的随机命名密文 `.tmp` 文件，成功后原子替换；测试临时数据可以使用 `test-temp/`。整个目录被忽略，不进入 Git，也不得进入 portable ZIP；`logs/` 不写入 `_internal/`、AppData、Documents 或 home。

候选快照只存在当前进程内；分析时复制到当前工作库，不跨运行去重。全文仅存于独立全文库，PDF 保留。成功日报字符串原样保存到独立历史库，重启后仍可查看；不写根 `reports/`。`run_round2.py` 使用显式 Key 注入，主库/全文库通过连接参数传入。根核心模块只保留 Desktop 所需功能，不依赖 Online 模块或资源。完整项目结构见 [PROJECT_STRUCTURE.md](../PROJECT_STRUCTURE.md)。
