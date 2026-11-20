# 开源剪辑引擎能力说明

OpenEdit Engine 采用纯开源工具链（ArtCraft **FilmCraft** + ArtCraft **EffectCraft** + **FFmpeg / FFprobe** + 离线 Python 音视频/跟踪组件），全面移除商业剪辑宿主曾存在的分辨率锁（640×360/720p）、时长上限（2s/4s）、固定离散参数白名单、密文草稿和跨版本逐项重新准入门槛。

> 证据口径：技术能力与实机测试结果只证明 CLI/MCP 构筑、回读、静帧渲染及导出全解码通过；完整成片的连续视听连贯性、艺术审美与 GUI 打开精修效果，仍由使用者最终观片确认。

<a id="filmcraft-editing"></a>
## 主剪辑与多轨时间轴

- **开源后端**：ArtCraft **FilmCraft**（Rust 内核，MIT/Apache-2.0，明文 `.fcproj` JSON 工程）。
- **已验证能力**：
  - 任意安全分辨率与帧率：支持宽度 `64..7680`、高度 `64..4320`、帧率 `1..120fps`、时长最高 `86400s`（24 小时），无商业宿主人工分辨率或时长上限。
  - 完整多轨 V/A 时间轴编排：支持 `layer_order` 自定义视频轨、叠加轨、文字轨及独立音轨顺序，自动按轨号顺序与 `0.000001s`（1 微秒）精度排布；同轨重叠及跨媒体类型同名轨道在 `validate` 阶段直接拒绝。
  - 转场、变速倒放与音频混音：`transition`（如 `Cross Dissolve`、擦除等，支持时长与入出边缘 `in`/`out`），片段速度 `0.05..32.0` 与 `reverse`；音频支持 `fade_in` / `fade_out`（FilmCraft `constant_power`）、`loudness`（`essentialSound.setType` + `essentialSound.autoMatch` 目标 LUFS 自动响度匹配）与 `beat_marks`（程序时间轴节拍标记）。
  - 行业标准工程互操作：支持同步导出 **OTIO** (`.otio`) 与 **FCPXML** (`.fcpxml`)，并经 FilmCraft MCP `file.exportOtio` / `file.exportFcpxml` 写出。
  - 无头成片导出与严格全解码：支持 H.264、HEVC、ProRes、WAV、AAC，支持关闭硬件编码（CRF 软件编码）或 `auto`（含本机 NVENC GPU 加速），每次导出均经 `ffprobe` 流存在检查与 `ffmpeg -v error -xerror -f null -` 全流无错解码验证。
- **使用边界**：手机 VFR 可变帧率或异形封装进入纯 Rust 解码器前应先执行 `normalize-media`；用户可使用 FilmCraft GUI 直接打开生成的明文 `.fcproj` 进行主观微调与最终视听确认。

<a id="effectcraft-overlays"></a>
## 动效合成与透明叠层（ProRes 4444）

- **开源后端**：ArtCraft **EffectCraft**（Rust 内核，MIT/Apache-2.0，明文 `.ecproj` JSON 工程）。
- **已验证能力**：
  - 覆盖 308 种内置特效、9 种图层样式、形状层/纯色层/文字层（含 `highlights` 区间高亮与 `text_preset` 动画器预设）/外部素材层、67 种内置入退场与行为预设、表达式、蒙版、3D 相机/灯光及 AE 风格 JSX 脚本（`effectcraft-exec` 与 `overlays[]` 声明式规范）。
  - **声明式可复用配方库（`overlays[].recipe`）**：内置 6 个分辨率自适应配方（`title_card`、`lower_third`、`keyword_callout`、`beat_flash`、`cinematic_bars`、`progress_banner`），支持通过少量业务参数一键展开为标准 EffectCraft 多层工程，详见 [纯开源手法映射与配方库](recipes.md)。
  - **透明通道中间件锁定**：在 1080p 实机验证中，CLI 默认 WebM VP9 导出会丢失 Alpha 通道；**`ProRes 4444 RGBA (.mov, yuva444p12le)`** 已实测保留完整可用 Alpha，并与 RGBA PNG 首尾协同。`render-overlay` 与 `build` 中的 `overlays` 均固定渲染 ProRes 4444 `.mov` 并自动挂载到 FilmCraft 上层视频轨合成。

<a id="filmcraft-grading"></a>
## 调色与 Lumetri

- 通过 `clips[].grading`（及 `composites[].grading`）接入 FilmCraft 原生 Lumetri 引擎，完整支持连续参数与二级控制：
  - 基础与创意调色：`exposure`、`contrast`、`saturation`（独立映射 Lumetri 基础饱和度）、`highlights`、`shadows`、`whites`、`blacks`、`temperature`、`tint`、`creative_sat`、`vibrance`、`sharpen` / `sharpness`、`faded_film`、`look_intensity`、`vignette`（`-5..5`） / `vignette_midpoint` / `vignette_roundness` / `vignette_feather`。
  - 全曲线与三色轮：亮度/RGB 曲线（`luma_curve`、`rgb_curves`）、5 组 HSL/Hue 曲线（`hue_vs_sat`、`hue_vs_hue`、`hue_vs_luma`、`luma_vs_sat`、`sat_vs_sat`）以及三区色轮（`wheels` / `primary_wheel` 的 `shadows` / `midtones` / `highlights` `[x, y]` 与 `*_l`）。
  - 二级 HSL、LUT 与局部遮罩：完整 `hsl` 二级抠像（或单色简写 `red_hsl` / `green_hsl` / `blue_hsl`）、前置 `input_lut` 与创意 `lut` / `look`，以及基于 Lumetri 椭圆遮罩的 `local_circle`。

<a id="filmcraft-blend"></a>
## 混合模式与合成

- `clips[]` 与 `composites[]` 原生支持 26 种混合模式（`blend_mode` / `mix_mode`）、静态与关键帧不透明度 `alpha`（`0..1`）、`fit`（`contain`/`cover`/`fill`）倍率联动、`flip_horizontal` / `flip_vertical` 水平/垂直镜像翻转。
- 支持 `position`（画布像素）、`transform_x` / `transform_y`（中心原点归一化 `[-1, 1]` 映射）、`scale_x` / `scale_y`（非等比自动展开 `scale_width`）、`rotation`、`keyframes` 插值、`mask`（矩形/椭圆/多边形/贝塞尔静态与关键帧遮罩）、`chroma`（`ultra_key` / `color_key` 绿幕/蓝幕/指定色键抠像）与 `composites[]` 嵌套合成组（`timeline.select` + `clip.nest`）。

<a id="text-highlights"></a>
## 图形字幕、重点词高亮与烧录字幕

- **图形字幕（`kind="graphic"`）**：通过 `graphics.newText` 在指定视频轨生成可编辑富文本图形层，原生支持 CJK 字体（默认优先检索 `Noto Sans SC` / `Microsoft YaHei` / `SimHei`，亦可通过 `font` 或本地静态 TTF/OTF `font_path` 指定）、描边、阴影、背景条，以及时间轴关键帧（位置/缩放/旋转/不透明度）。
- **同句多色重点词高亮（`highlights`）**：在同一段文本内通过 `graphics.setCharStyle` 按非重叠半开字符区间 `0 <= start < end <= len(text)` 施加多种 `#RRGGBB` 着色，兼容英文与中文标点。
- **烧录字幕轨（`kind="subtitle"`）**：通过 `captions.newTrack` + `captions.setStyle` + `captions.add` 写入独立字幕轨（默认 `C1`），并在导出时受 `--no-burn-captions` / `export.burn_captions` 控制烧录或保留软字幕结构。

<a id="media-prep"></a>
## 媒体探测与 VFR 标准化

- `inspect` 与 `reference` 基于 `ffprobe` / `ffmpeg`（已验证至 FFmpeg 9.0.2，使用现代 `-fps_mode vfr` 抽取参考帧与镜头切换候选）探测本地音视频与图片流参数、像素格式 Alpha 通道及 VFR 标志。
- `normalize-media` 将手机录制的可变帧率（VFR）或非标准封装转为恒定帧率（CFR）H.264/AAC 或 ProRes，保障多轨剪辑声画对齐与帧同步。
- **素材来源核查（`.provenance.json`）**：构建前优先于落盘目录强制检查同目录 sidecar `*.provenance.json`，核对 `license_verification == "agent_review_recorded"`、`review_note` 与实测 `sha256`，校验不通过立即中止生成，并在成果目录写出 `素材来源.md` 与 `素材来源.json`。

<a id="transcription"></a>
## 离线语音转写（Whisper）

- 通过 `transcribe` 调用本地 `faster-whisper` 进行离线 ASR 语音识别，输出带段级/词级时间戳的 JSON 或 SRT。
- **边界**：包内不内置大模型权重，调用方需通过 `--model` 传入本机已下载的 Whisper 权重目录或标识；缺本地权重时在 `checks` 中明确跳过，不冒充通过。

<a id="separation"></a>
## 离线人声与伴奏分离（Demucs）

- 通过 `separate` 调用本地 `onnxruntime` 运行 Demucs ONNX 权重，分离人声（vocals）与伴奏（accompaniment）。
- **边界**：需调用方自备并通过 `--model` 传入 Demucs ONNX 模型文件；缺权重时跳过对应自检。

<a id="ducking"></a>
## 自动音频闪避（Ducking）

- 通过 `ducking` 读取人声音频（`--vocals`）或语音时间戳 JSON（`--speech-json`），基于实时响度包络门限（`--duck-db`、`--attack`、`--release`、`--floor-margin-db` 等）自动生成 `audio[].keyframes`（要求 `t=0` 起步且源/本地时间严格递增），交由 FilmCraft 编译为 A 轨音频增益关键帧实现背景音乐自动压低。

<a id="offline-denoise"></a>
## 离线音频降噪（FFT）

- 通过 `denoise-audio` 对本地音频或音轨片段执行频谱 FFT 降噪，输出无损 PCM WAV 供后续剪辑计划引用，显式保留原始噪声基准与增益设置边界。

<a id="beat-editing"></a>
## 离线节拍检测与按拍切镜

- 通过 `beats`（基于 `librosa`）从指定音频区间离线提取节拍候选及能量信息；通过 `beat-apply` 按 `--every N` 映射生成对齐项目帧率的剪切计划或音频标记，并与 `music-edit compile` 的 80ms 吸附窗口协同工作。

<a id="offline-tracking"></a>
## 离线单目标跟踪

- 通过 `tracking`（基于 OpenCV CSRT 与 PyAV 真实 PTS 解码）追踪选定视频区间内单目标 ROI 轨迹；通过 `tracking-apply` 将轨迹映射为目标层的 `position_x` / `position_y` 关键帧。

## 其他扩展能力与验收边界

- **Mesh Avatar 透明形象**：`mesh-avatar` 支持无头构建并打包带完整 Alpha 通道的身份保持 2D 虚拟形象透明视频供画中画或叠加使用。
- **10-bit H.264 (Hi10P) 兼容守卫**：实测 FilmCraft 对 `h264` + `yuv420p10le` 等 10-bit H.264 编码会静默渲染为 `MEDIA UNREADABLE` 警告画面（12-bit ProRes 4444 与 8-bit `yuv420p` 不受影响）；`inspect`、`validate` 与 `build` 已内置自动识别与前置拦截，提示先执行 `normalize-media` 转为 8-bit `yuv420p` CFR。
- **多模态音乐驱动编排（`music-edit`）**：通过 `prepare` -> `analyze` / `review` -> `compile` 对接使用者自配的 OpenAI 兼容或 Gemini 原生视听模型（仅在显式 `analyze` / `review` 时产生请求与 API 用量；支持将分析结构直接编译为纯开源 `openedit`/`filmcraft` 计划）。详见 [原生视听与音乐编排](music-edit.md)。
- **验证分级声明**：`validate` 通过 ≠ `build` 通过 ≠ `export` 全解码通过 ≠ 人类主观视听认可；关键视觉效果应同时结合 `preview` PNG 帧检查与使用者最终看片反馈。
