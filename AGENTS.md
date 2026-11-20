# OpenEdit Engine 开发与协作规范

本仓库是纯开源 Agent 剪辑引擎 **`openedit-engine`**（含兼容垫片 **`jianying_engine`**）与 Agent Skill 分发模板的唯一正式源码。

## 目录结构与真值边界

- **正式核心源码**：[src/openedit_engine](src/openedit_engine)（公共白名单与 BM25 知识检索在 `src/openedit_engine/api.py`，后端目录与探测在 `software_backends.py` / `software_detection.py`，脚本/核心校验/后端编译与参考文档/指南在 `src/openedit_engine/runtime/`）。
- **向后兼容薄垫片**：[src/jianying_engine](src/jianying_engine) 仅包含 9 个薄委托文件以保持已有脚本/MCP 配置无缝工作；严禁在该目录下编写或分叉独立业务逻辑。
- **Skill 分发模板**：[adapters/skills/editing-workbench](adapters/skills/editing-workbench) 是同步到外部 Agent Skills 目录的唯一模板来源。
- **本机环境说明**：如果工作区存在 `AGENTS.local.md`，按其中约定定位本机 Python 虚拟环境、外部项目目录及长期维护台账目录；提交历史前请务必阅读 `README.md` 与 `python -I -m openedit_engine paths`。

## 实施与验收约束

1. **全栈纯开源与单一命令面**：不重新引入任何闭源商业剪辑宿主（剪映、PR、AE、DaVinci）适配或静态/加密逆向逻辑。所有新 CLI/MCP 能力均须先通过 `src/openedit_engine/api.py` 注册白名单与 `OPERATION_SCHEMAS` 后暴露。
2. **禁止隐式副作用与越权操作**：
   - 严禁启动任何图形界面客户端（FilmCraft GUI / EffectCraft GUI / 商业软件）；
   - 严禁修改外部商业软件用户数据目录、缓存或 `%LOCALAPPDATA%\JianyingEngine\runtime-admissions.json`；
   - 除非用户明确指令，严禁执行 `git commit` 或 `git push`；
   - 严禁篡改外部已安装 Skill 目录下的 `backends\` 与 `.magpie-library`。
3. **透明通道中间件规则**：EffectCraft 透明叠层在编码链路上一律使用 `ProRes 4444 RGBA (.mov, yuva444p12le)` 并经 `ffprobe` + 严格解码验收；禁止使用丢 Alpha 的 WebM VP9 默认导出替代。
4. **分层验收口径（严禁混为一谈）**：
   - 区分“源码单元测试（`unittest`）”、“运行时自检（`openedit checks`，跳过需外置模型权重的项目须明确标注）”、“实时 stdio MCP 通信”、“实际编译导出 + `ffmpeg -v error -xerror` 严格全解码 + PNG 预览图检查”以及“人工 GUI 微调与连续视听审美确认”。
   - 任何外部测试/模型/渲染跳过都必须如实记录为未验证或跳过，不得申报为全量实机通过。
5. **并行隔离与外部台账**：涉及多方改动时隔离输出临时目录；每次重要维护完成并通过全量验收后，记录真实 SHA-256 与验证读数到工作区外维护台账并清理临时编译产物。
