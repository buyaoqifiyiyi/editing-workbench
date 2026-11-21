# OpenEdit 纯开源剪辑执行模块

本模块用于直接调用本机 **OpenEdit Engine**（ArtCraft **FilmCraft** + ArtCraft **EffectCraft** + **FFmpeg / FFprobe** + 本地离线 AI 工具），将构思编译为明文 `.fcproj` / `.ecproj` 工程、ProRes 4444 透明叠加层、OTIO/FCPXML 交换工程与严格全解码验证的成品视频。

## 准备与参考阅读

- 有 `openedit-engine`（或兼容别名 `jianying-engine`）MCP 时依次调用：
  1. `describe_operations(include_backends=True)` 确认 FilmCraft / EffectCraft / FFmpeg 全部就绪；
  2. `read_guide("editing")`、`read_guide("capabilities")`、`read_guide("plan-format")`、`read_guide("reference/openedit")` 与 `read_guide("reference/recipes")`；
  3. 需要时用 `search_knowledge("<具体剪辑或创作问题>")` 检索技法。
- 无 MCP 时在终端调用：

```powershell
$enginePython = if ($env:OPENEDIT_ENGINE_PYTHON) { $env:OPENEDIT_ENGINE_PYTHON } elseif ($env:JIANYING_ENGINE_PYTHON) { $env:JIANYING_ENGINE_PYTHON } else { Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv\Scripts\python.exe' }
& $enginePython -m openedit_engine doctor
& $enginePython -m openedit_engine guide editing
& $enginePython -m openedit_engine guide capabilities
& $enginePython -m openedit_engine guide plan-format
& $enginePython -m openedit_engine guide reference/openedit
& $enginePython -m openedit_engine guide reference/recipes
```

## 标准制作与验证流

1. **探测与标准化**：先对陌生素材运行 `inspect`（或参考片分析 `reference`）；若存在 VFR 警告、**10-bit H.264（`yuv420p10le` / Hi10P，FilmCraft 会渲染为 `MEDIA UNREADABLE`）**或非常规编码，先调用 `normalize-media` 转码为 8-bit `yuv420p` 恒定帧率 CFR。
2. **可选 AI / 音视频白名单工具**：按需调用 `beats` / `beat-apply`（节拍分析与切点）、`transcribe`（通过 `OPENEDIT_WHISPER_MODEL` 或 `--whisper-model` 指定离线 Whisper 权重）、`separate`（通过 `OPENEDIT_DEMUCS_MODEL` 或 `--demucs-model` 指定离线 Demucs ONNX 权重）、`ducking`、`denoise-audio`、`captions`、`tracking` / `tracking-apply` 或多模态 `music-edit`。
3. **验证、编译与叠加**：写出 `edit-plan.json`（高阶透明动效写入 `overlays[]`：推荐优先使用 `overlays[].recipe` 的 6 个分辨率自适应内置配方 `title_card` / `lower_third` / `keyword_callout` / `beat_flash` / `cinematic_bars` / `progress_banner`，或结合 `layers` / `script` 自定义，EffectCraft 将自动产出锁定格式的 `ProRes 4444 RGBA .mov`），执行 `validate` 后再执行 `build --plan <计划> --out <新目录> --preview-times <时间列表> --export <输出.mp4> --export-otio <timeline.otio> --export-fcpxml <timeline.fcpxml>`。
4. **交付与实话实说**：使用 MCP 时经 `start_job` -> 轮询 `job_status` -> `read_job_output` 获取完整结构化报告；对生成的 `previews/frame-*.png` 抽帧目视核查文字/Alpha/构图，并确认报告中的 `strict_decode == "passed"`。不启动任何 GUI 界面，并告知使用者可随时打开 `.fcproj` 做主观精修与完整视听审片。
