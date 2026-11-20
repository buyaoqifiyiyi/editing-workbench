# OpenEdit Engine（纯开源 Agent 剪辑引擎 v1.0.0）

**OpenEdit Engine** 是一个面向 AI Agent（Codex、Antigravity、DSH、OpenCode 等）与命令行的**纯开源视频剪辑、动效合成与媒体处理引擎**，由 **ArtCraft FilmCraft**、**ArtCraft EffectCraft**、**FFmpeg / FFprobe** 以及可插拔本地开源 AI 音频/视觉工具组成。

> **架构声明**：自 `1.0.0` 起，引擎已全面移除商业剪辑宿主（剪映、DaVinci Resolve、Adobe Premiere / After Effects）依赖与历史独立桌面/Web 工作台（商业闭源适配基线已冻结并保存在 Git 标签 `archive/commercial-v0.6.3`）。原 `jianying-engine` 包名、CLI 与 MCP 入口仅保留为向后兼容的薄委托垫片。

## 纯开源核心能力

| 组件 | 开源协议 | 负责职责 | 核心产出的工程/产物 |
|---|---|---|---|
| **ArtCraft FilmCraft** (`filmcraft-cli`) | MIT OR Apache-2.0 | 多轨 V/A 时间轴、硬切/转场、变速倒放、Lumetri 曲线/色轮/HSL/LUT 调色、26 种混合模式、CJK 图形文字/多色重点词高亮、Subtitle 字幕轨、OTIO/FCPXML 互操作与成片导出 | 明文 `.fcproj`、`.otio`、`.fcpxml`、PNG 预览帧、H.264/HEVC/ProRes/NVENC 成片 |
| **ArtCraft EffectCraft** (`effectcraft-cli`) | MIT OR Apache-2.0 | 308 种特效、9 种图层样式、形状/纯色/文字/素材层、文字动画器、蒙版、表达式、3D 相机/灯光、AE 风格 JSX 脚本 | 明文 `.ecproj`、RGBA 预览 PNG、锁定 **`ProRes 4444 RGBA (.mov)`** 透明叠层 |
| **FFmpeg / FFprobe** | LGPL / GPL | 媒体流探测、手机 VFR 可变帧率转码标准化（`normalize-media`）、参考抽帧与导出后严格全解码核验 | 标准 CFR 中间件、严格全解码回执（`ffmpeg -v error -xerror`） |
| **离线开源 AI 工具** | MIT / Apache-2.0 / BSD | 按拍切镜（`librosa`）、语音转写（`faster-whisper`）、人声分离（`onnxruntime` Demucs）、音频闪避（`ducking`）、FFT 降噪、CSRT 单目标跟踪（OpenCV + PyAV） | `edit-plan.json` 增补轨迹/关键帧/字幕及独立音频文件 |
| **创作知识库与全模态选配** | — | BM25 节级知识检索引擎（`search_knowledge`）；可选调用用户自配 OpenAI 兼容或 Gemini 原生多模态服务的 `music-edit` | 创作方法引用、`analysis.json` / `review.json` |

### 已解除的商业边界锁
- **自由画布与帧率**：支持 `64..7680` × `64..4320` 分辨率与 `1..120fps`，移除曾经的 `640×360` / `720p`、2s/4s/20s 时长上限与离散数值白名单。
- **全明文可审阅工程**：所有 `.fcproj` / `.ecproj` 均为清晰明文 JSON，用户随时可用 FilmCraft / EffectCraft 桌面客户端打开微调，无草稿加密与版本号漂移。
- **自动全链路严格校验**：每次构建校验 `.provenance.json` 来源 SHA-256、导出后自动运行 `ffprobe` 与 `ffmpeg -v error -xerror` 全解码验证，并写出 `剪辑交付.md` 与 `素材来源.md`。

## 安装与常用命令

建议使用 Windows 与 64 位 Python 3.12+，确保本机可用 `ffmpeg`/`ffprobe` 及 ArtCraft `filmcraft-cli` / `effectcraft-cli`：

```powershell
$engineVenv = Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv'
py -3.12 -m venv $engineVenv
$enginePython = Join-Path $engineVenv 'Scripts\python.exe'

# 开发维护安装（editable，含 MCP 与常用开源扩展）
& $enginePython -m pip install -e '.[mcp]'
# 按需可选 extras: [rhythm] [audio] [tracking] [exchange] [all]
```

日常诊断与剪辑指令（也可直接调用安装后的 `openedit` / 兼容别名 `jianying-engine`）：

```powershell
& $enginePython -m openedit_engine doctor
& $enginePython -m openedit_engine backends
& $enginePython -m openedit_engine capabilities --query blur --live
& $enginePython -m openedit_engine validate --plan edit-plan.json
& $enginePython -m openedit_engine build --plan edit-plan.json --out build-out --preview-times 0.5 2.0 --export final.mp4 --export-otio timeline.otio --export-fcpxml timeline.fcpxml
& $enginePython -m openedit_engine checks
& $enginePython -m openedit_engine guide editing
& $enginePython -m openedit_engine knowledge "EffectCraft"
```

## Agent 与 MCP 集成

- **Agent 技能模板**：位于 [adapters/skills/editing-workbench](adapters/skills/editing-workbench)，提供统一的 `editing-workbench` Skill 入口（含 `openedit`、`planning`、`maintenance` 子模块）。
- **本机 MCP Server**：见 [docs/mcp.md](docs/mcp.md) 与 [docs/agents.md](docs/agents.md)，提供官方 `mcp==2.3.0` stdio 服务（`openedit_engine.mcp_server`，兼容别名 `jianying_engine.mcp_server`），对外暴露 7 个只读/受控工具：`describe_operations`、`read_guide`、`search_knowledge`、`start_job`、`job_status`、`read_job_output`、`cancel_job`。

## 许可证与回溯

- 本仓库 Python/JS 代码及捆绑文案遵循仓库许可证及开源下游组件各自的 MIT / Apache-2.0 / LGPL / BSD 协议。
- 商业宿主适配与桌面端工作台历史代码完整冻结在 Git annotated tag `archive/commercial-v0.6.3`；所有运行生成数据、素材、模型与私人密钥均严禁放入源码或包内。
