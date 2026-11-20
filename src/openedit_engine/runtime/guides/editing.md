# 开源剪辑步骤

本指南说明如何使用纯开源后端（ArtCraft **FilmCraft** + **EffectCraft** + **FFmpeg** + 本地离线 AI 工具）完成从素材诊断到工程编译、抽帧预览、成片导出与严格全解码验收的标准执行链路。开始制作前先阅读 `planning` 完成构思与选段设计。

## 1. 环境与后端就绪检查

1. 首先核对本机纯开源工具链状态（或经 MCP `describe_operations(include_backends=True)`）：
   - `openedit doctor`（确认 `ready: true`，核实 `filmcraft`、`effectcraft`、`ffmpeg` 与本机 GPU/AI 扩展可用性）。
   - `openedit capabilities --query blur --live`（按需检索 FilmCraft / EffectCraft 实际可用命令与特效名称，所有字段规范以 `capabilities` 与 `plan-format` 为准）。

## 2. 素材探测、预处理与创作检索

1. **媒体检查**：使用 `openedit inspect --input <文件或目录> --out <新探测目录>` 查看分辨率、编码、帧率、音轨与是否存在 VFR（可变帧率）警告；需要观察参考视频切点时使用 `openedit reference`。
2. **VFR / 异形封装标准化**：若检测到手机 VFR 视频或非常规编码，先执行 `openedit normalize-media --input <源视频> --out <cfr视频.mp4>` 转为恒定帧率 CFR H.264/AAC 或 ProRes，防止多轨声画漂移。
3. **可选离线 AI 辅助与创作知识检索**：
   - 使用 `openedit knowledge "<问题>"` 或 MCP `search_knowledge` 检索叙事、声画关系、转场、混合与构图方法，并用 `guide` / `read_guide` 读原文。
   - 按任务白名单按需调用：`beats` / `beat-apply`（节拍检测与按拍切镜）、`transcribe`（Whisper 转写，需自备权重）、`separate`（Demucs 人声分离，需自备权重）、`ducking`（音乐自动闪避）、`denoise-audio`（FFT 降噪）、`captions`（逐段/逐词高亮字幕映射）、`tracking` / `tracking-apply`（CSRT 单目标跟踪）。

## 3. 编写计划、合法性校验与工程编译

1. 在项目独立目录下编制 `edit-plan.json`（规范见 `plan-format`）：
   - 支持自由画布尺寸（`64..7680` × `64..4320`、`1..120fps`），无商业分辨率/时长锁。
   - 主剪辑、转场、Lumetri 调色、运动与不透明度关键帧、26 种混合模式、`highlights` 多色高亮文字与烧录字幕轨写入 `clips`/`audio`/`captions`。
   - 需要 308 种高级特效、图层样式、复杂形状或表达式动效时，声明到 `overlays[]`（编译时将自动由 EffectCraft 无头渲染为 **`ProRes 4444 RGBA (.mov)`** 透明叠加层并挂入 FilmCraft 上层轨道）。
   - 所有联网/第三方授权素材均应附带通过校验的 `.provenance.json` sidecar。
2. 执行严格结构与媒体探测校验：`openedit validate --plan <edit-plan.json>`。
3. 编译工程、透明叠层、预览帧与成品输出（输出目录必须为全新目录，拒绝静默覆盖）：

```powershell
openedit build --plan '<项目>\edit-plan.json' --out '<项目>\build-v1' --preview-times 0.5 2.0 --export '<项目>\build-v1\final.mp4' --export-otio '<项目>\build-v1\timeline.otio' --export-fcpxml '<项目>\build-v1\timeline.fcpxml'
```

## 4. 抽帧核查、严格全解码与交付

1. **视觉抽帧**：对输出 `previews/frame-*.png`（或二次执行 `openedit preview --project <name>.fcproj --out <预览目录> --times ...`）逐帧检查文字位置、抗锯齿、透明叠层边界、遮挡层级与构图重心。
2. **导出与严格解码**：`build --export` 或 `openedit export` 会自动执行 `ffprobe` 流检查及 `ffmpeg -v error -xerror -i <导出文件> -f null -` 严格全流无错解码验证。
3. **如实交付与验证边界**：交付项目目录内的明文 `<name>.fcproj`、`overlays/`（如有）、`build-report.json`、`剪辑交付.md` 与 `素材来源.md` / `素材来源.json`。向用户明确汇报已完成结构校验、静帧检查与严格解码，并提醒使用者按需打开 `.fcproj` 在 GUI 中做最终主观精修与完整连续视听验收。
