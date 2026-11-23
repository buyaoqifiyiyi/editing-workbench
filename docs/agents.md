# 多 Agent 接入指南（OpenEdit Engine 1.0）

`openedit-engine` 将全部本地剪辑、特效合成与离线分析能力集中暴露在纯开源白名单 CLI 与 `stdio` MCP 接口上，任何支持标准 MCP 客户端或终端执行的编码/多模态 Agent（Codex、Antigravity、DeepSeek Harness、OpenCode 等）均可直接接入。

> 注：历史版本中的独立桌面客户端（Electron）与 Web 工作台已于 `1.0.0` 下线移除，对应源码冻结在 Git 标签 `archive/commercial-v0.6.3`；当前推荐通过对话中的 `editing-workbench` Skill 协同 MCP/CLI 执行全部制作任务。

## 两种标准接入方式

1. **推荐：通过 stdio MCP 注册接入**
   - 参考 [docs/mcp.md](mcp.md)，在宿主配置文件的 `mcpServers` 中登记 `openedit-engine`（指向虚拟环境的 `python -m openedit_engine.mcp_server` 或 `openedit-mcp`）。
   - Agent 开工顺序：`describe_operations(include_backends=True)` -> `read_guide("planning")` / `read_guide("editing")` -> 按需 `search_knowledge(...)` -> 通过 `start_job` / `job_status` / `read_job_output` 执行 `inspect` / `normalize-media` / `validate` / `build` / `preview` / `export`。
2. **命令行直接执行**
   - 直接调用安装环境中的 `openedit` 或 `& <venv>\Scripts\python.exe -m openedit_engine <子命令>`，均经由同一份 `api.command_for` 白名单与参数校验执行，无特权捷径。

## 可选视听多模态服务（`music-edit`）

- 当任务指定背景音乐（BGM）并需要基于真实画面内容、情绪起伏与动作卡点进行选段或成片审片时，可使用 `music-edit prepare` / `analyze` / `review` / `compile`。
- 协议支持 `openai-compatible`（`{base_url}/chat/completions`）与 `gemini`（原生 `generateContent`），服务地址、模型 ID 与 API 密钥全部由使用者在外部配置文件或环境变量显式配置（默认专用配置位于 `%LOCALAPPDATA%\OpenEditEngine\omni.json`，支持 `OPENEDIT_OMNI_CONFIG` / `--config` 覆盖及已保存的 Windows DPAPI `omni.key`）。
- 完整说明与媒体大小限制见 [原生视听与音乐编排](../src/openedit_engine/runtime/references/music-edit.md)。

## 接入端到端验证核对

接入新宿主 Agent 后建议依次验证：
1. 客户端成功列出 7 个 MCP 工具，`describe_operations(include_backends=True)` 只返回 `filmcraft`、`effectcraft`、`ffmpeg` 三个开源后端且全部 `selectable: true`。
2. `read_guide("capabilities")` 返回完整 27 项开源能力锚点无截断，`search_knowledge("EffectCraft", 1)` 命中 `reference/openedit`。
3. 以独享临时目录跑通 `openedit validate` -> `openedit build --preview-times ... --export ...` -> `openedit audit --contact-sheet` 并经 FFmpeg 严格全解码验收，最后交由使用者在 GUI 打开明文 `.fcproj` 做主观视听确认。
