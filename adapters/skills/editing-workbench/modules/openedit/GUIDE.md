# OpenEdit 纯开源剪辑执行模块

本模块用于直接调用本机 **OpenEdit Engine**（ArtCraft **FilmCraft** + ArtCraft **EffectCraft** + **FFmpeg / FFprobe** + 本地离线 AI 工具），将构思编译为明文 `.fcproj` / `.ecproj` 工程、ProRes 4444 透明叠加层、OTIO/FCPXML 交换工程与严格全解码验证的成品视频。

## 准备与参考阅读

- 有 `openedit-engine`（或兼容别名 `jianying-engine`）MCP 时依次调用：
  1. `describe_operations(include_backends=True)` 确认 FilmCraft / EffectCraft / FFmpeg 全部就绪；
  2. `read_guide("editing")`、`read_guide("capabilities")`、`read_guide("plan-format")`、`read_guide("reference/openedit")` 与 `read_guide("reference/recipes")`；
  3. 需要时用 `search_knowledge("<具体剪辑或创作问题>")` 检索技法。
- 无 MCP 时在终端调用：

```powershell
$enginePython = if ($env:OPENEDIT_ENGINE_PYTHON) { $env:OPENEDIT_ENGINE_PYTHON } elseif ($env:JIANYING_ENGINE_PYTHON) { $env:JIANYING_ENGINE_PYTHON } else { Join-Path $env:LOCALAPPDATA 'OpenEditEngine\venv\Scripts\python.exe' }
& $enginePython -m openedit_engine doctor
& $enginePython -m openedit_engine guide editing
& $enginePython -m openedit_engine guide capabilities
& $enginePython -m openedit_engine guide plan-format
& $enginePython -m openedit_engine guide reference/openedit
& $enginePython -m openedit_engine guide reference/recipes
```

## 标准制作与验证流

1. **探测与批量标准化**：先对陌生素材运行 `inspect`（或参考片分析 `reference`）；若存在 VFR 警告、**10-bit H.264（`yuv420p10le` / Hi10P，FilmCraft 会渲染为 `MEDIA UNREADABLE`）**或非常规编码，先调用 `normalize-media`（支持传入单文件或素材目录批量跳过合规文件、仅转码 VFR/Hi10P 并生成 `path_map`）转为 8-bit `yuv420p` 恒定帧率 CFR。
2. **可选 AI / 音视频白名单工具**：本机已内置自动发现 `%LOCALAPPDATA%\OpenEditTools\models\{demucs,whisper,vad,matting,face,beats,clap,depth,flow,siglip2,allin1}` 离线权重与模型（可通过 `doctor` 查看 `local_models` 与 `gpu_acceleration`）。按创作场景组合调用（路线见 `reference/creative-knowledge-workflows`）：
   - **节奏与声音**：`music-structure`（Beat This! ONNX / allin1 下行拍与乐段 Drop 分析）、`sfx --from-structure`（程序化音效床 + 闪避包络）、`sfx-search`（CLAP 零样本搜音效）、`audio-master`（双遍 EBU R128 母带 + `afir` 混响 + `--sidechain` 闪避）、`beats` / `beat-apply [--use-downbeats]`、`separate`、`ducking`、`denoise-audio`；
   - **搜镜、动势与视效**：`shot-search [--with-motion]`（SigLIP 2 双语搜镜 + 光流出入场动势）、`motion-analyze`（NeuFlow v2/Farneback 光流 & OpenColorIO 2.6 AgX/ACES `.cube` LUT）、`matting`（MatAnyone 2 / MIT BiRefNet / RVM ONNX 非绿幕抠像导出 ProRes 4444 RGBA）、`interpolate`（Vulkan RIFE 补帧/慢放）、`depth`（Video Depth Anything / Depth Anything V2 景深与 2.5D 视差）、`shader-fx`（ModernGL GL-Transitions & ISF Shader）、`tracking` / `tracking-apply`；
   - **口播与重构图**：`auto-reframe`（横竖屏跟拍重构图）、`rough-cut`（去静音+字幕一步出粗剪工程）、`auto-cut`（Silero VAD / FFmpeg 气口粗剪）、`transcribe`、`captions` 或多模态 `music-edit`（AI 推理操作支持加 `--gpu` / `--device cuda` 启用 CUDA 加速）。
3. **验证、轨道整理、草稿预演、编译与叠加**：写出 `edit-plan.json`（高阶透明动效与 Hero 场景写入 `overlays[]`：常规影视包装推荐 EffectCraft 的 **13 个** 分辨率自适应配方 `title_card` / `lower_third` / `keyword_callout` / `beat_flash` / `cinematic_bars` / `progress_banner` / `chapter_opener` / `pip_card` / `split_screen_frame` / `wipe_transition` 及 3 套素材 Hero 配方 `whip_transition` / `impact_punch` / `parallax_3d_scene` 或 `layers`/`script`；商业广告数据图表、KPI 跳数、参数对比表与进度卡可设 `backend: "hyperframes"` 选用 **4 套** 纯离线数据动画配方 `kpi_bar_chart` / `kpi_counter_banner` / `comparison_table` / `milestone_progress` 或离线 `html`，均自动产出锁定格式的 `ProRes 4444 RGBA .mov`），执行 `validate` 后，对多轨显式 `layer_order` 计划可按需运行 `layout-tracks` 安全整理复用叠层轨；长片或重特效工程建议先跑 `build --draft` 或仅带 `--preview-times` 的 `build` 看图核帧（确认无字幕遮挡、双影重影与着色器接缝），确认后再不带 `--draft` 全分辨率 `build --plan <计划> --out <新目录> --preview-times <时间列表> --export <输出.mp4> --export-otio <timeline.otio> --export-fcpxml <timeline.fcpxml>`。
4. **交付前质检与两类验收分离汇报**：使用 MCP 时经 `start_job` -> 轮询 `job_status` -> `read_job_output` 获取完整结构化报告；运行 `audit --plan <计划> --build-dir <build目录> --export <输出.mp4> [--beats <structure目录>] --contact-sheet --out <audit目录>` 检查源区间重叠、全资产严格解码、响度/0削波、节拍对齐误差与 `qc_contact_sheet.jpg`，并对 `previews/frame-*.png` 与联系表抽帧目视核查文字/Alpha/构图（若在授权前提下调用 `music-edit review`，须核对画面与音轨版本及覆盖范围一致）。严禁启动任何 GUI 界面；交付时按 `planning` 分开汇报内容/视听表达取舍与技术验证层级，并告知使用者可随时打开 `.fcproj` 做主观精修与完整连续视听审片。
