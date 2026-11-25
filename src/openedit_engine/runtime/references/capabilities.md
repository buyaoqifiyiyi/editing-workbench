# 开源剪辑引擎能力说明

OpenEdit Engine 采用纯开源工具链（ArtCraft **FilmCraft** + ArtCraft **EffectCraft** + **FFmpeg / FFprobe** + 离线 Python 音视频/跟踪组件），全面移除商业剪辑宿主曾存在的分辨率锁（640×360/720p）、时长上限（2s/4s）、固定离散参数白名单、密文草稿和跨版本逐项重新准入门槛。

> 证据口径：技术能力与实机测试结果只证明 CLI/MCP 构筑、回读、静帧渲染及导出全解码通过；完整成片的连续视听连贯性、艺术审美与 GUI 打开精修效果，仍由使用者最终观片确认。

<a id="filmcraft-editing"></a>
## 主剪辑与多轨时间轴

- **开源后端**：ArtCraft **FilmCraft**（Rust 内核，MIT/Apache-2.0，明文 `.fcproj` JSON 工程）。
- **已验证能力**：
  - 任意安全分辨率与帧率：支持宽度 `64..7680`、高度 `64..4320`、帧率 `1..120fps`、时长最高 `86400s`（24 小时），无商业宿主人工分辨率或时长上限。
  - 完整多轨 V/A 时间轴编排：支持 `layer_order` 自定义视频轨、叠加轨、文字轨及独立音轨顺序，自动按轨号顺序与 `0.000001s`（1 微秒）精度排布；同轨重叠及跨媒体类型同名轨道在 `validate` 阶段直接拒绝。
  - 转场、变速倒放、AE 曲线运镜与音频混音：`transition`（如 `Cross Dissolve`、擦除等，支持时长与入出边缘 `in`/`out`）、`clips[].camera_motion`（高密度贝塞尔/弹簧入出速率曲线 + 跨切点动势接力 + 自动防黑边预放大 + 永不死停微漂移）、片段速度 `0.05..32.0` 与 `reverse`；音频支持 `fade_in` / `fade_out`（FilmCraft `constant_power`）、`loudness`（`essentialSound.setType` + `essentialSound.autoMatch` 目标 LUFS 自动响度匹配）与 `beat_marks`（程序时间轴节拍标记）。
  - 行业标准工程互操作：支持同步导出 **OTIO** (`.otio`) 与 **FCPXML** (`.fcpxml`)，并经 FilmCraft MCP `file.exportOtio` / `file.exportFcpxml` 写出。
  - 无头成片导出与严格全解码：支持 H.264、HEVC、ProRes、WAV、AAC，支持关闭硬件编码（CRF 软件编码）或 `auto`（含本机 NVENC GPU 加速），每次导出均经 `ffprobe` 流存在检查与 `ffmpeg -v error -xerror -f null -` 全流无错解码验证。
- **使用边界**：手机 VFR 可变帧率或异形封装进入纯 Rust 解码器前应先执行 `normalize-media`；用户可使用 FilmCraft GUI 直接打开生成的明文 `.fcproj` 进行主观微调与最终视听确认。

<a id="effectcraft-overlays"></a>
## 动效合成与透明叠层（ProRes 4444）

- **开源后端**：ArtCraft **EffectCraft**（Rust 内核，MIT/Apache-2.0，明文 `.ecproj` JSON 工程）。
- **已验证能力**：
  - 覆盖 308 种内置特效、9 种图层样式、形状层/纯色层/文字层（含 `highlights` 区间高亮与 `text_preset` 动画器预设）/外部素材层（支持 `time`/`in`/`out`/`duration` 与 `time_remap`/`timeRemap` 连续变速曲线）/真实 **3D 相机层（`type="camera"` -> `layer.newCamera` + `three_d` Z 轴景深视差 + `motion_blur` 合成运动模糊）**、67 种内置入退场与行为预设、表达式、蒙版、灯光及 AE 风格 JSX 脚本（`effectcraft-exec` 与 `overlays[]` 声明式规范）。
  - **声明式可复用配方库（`overlays[].recipe`）**：内置 **16 个** 分辨率自适应配方（`title_card`、`lower_third`、`keyword_callout`、`beat_flash`、`cinematic_bars`、`progress_banner`、`chapter_opener`、`pip_card`、`split_screen_frame`、`wipe_transition`、`editorial_card`，以及 5 个素材驱动 Hero 配方 `whip_transition`、`velocity_cut`、`impact_punch`、`parallax_3d_scene`、`hero_ring_cutout`），支持通过少量业务参数一键展开为标准 EffectCraft 多层工程，详见 [纯开源手法映射与配方库](recipes.md)。
  - **透明通道中间件锁定与内容寻址缓存**：在 1080p 实机验证中，CLI 默认 WebM VP9 导出会丢失 Alpha 通道；**`ProRes 4444 RGBA (.mov, yuva444p12le)`** 已实测保留完整可用 Alpha，并与 RGBA PNG 首尾协同。`render-overlay` 与 `build` 中的 `overlays` 均固定渲染 ProRes 4444 `.mov` 并自动挂载到 FilmCraft 上层视频轨合成；声明式/配方叠层默认按图层内容、画布、素材 SHA-256 与 CLI 指纹缓存在 `%LOCALAPPDATA%\OpenEditEngine\overlay-cache`（支持 `--no-cache` / `--no-overlay-cache` 绕过）。

<a id="hyperframes-data-motion"></a>
## 数据动画与 UI 透明叠层（HyperFrames）

- **开源后端**：**HyperFrames**（Apache-2.0，无头 Node.js + Chromium 渲染内核；通过 `overlays[].backend: "hyperframes"` 或 `render-overlay --spec` 启用，不替代主时间轴与 EffectCraft）。
- **已验证能力**：
  - **10 套内置数据、UI 连续画布与技术流 MG 动画配方（全面支持活视频容器）**：商业数据 4 套（`kpi_bar_chart`、`kpi_counter_banner`、`comparison_table`、`milestone_progress`）、UI 丝滑切换 2 套（`ui_flow_stage` 单画布共享元素 FLIP 展开 + RK4 弹簧物理 + 虚拟相机、`kinetic_ui_title` 错峰三联动标题）与技术流平面 MG 4 套（`viewfinder_hud` 取景器 HUD、`polaroid_grid` 拍立得+避让手绘红圈、`filmstrip_reel` 35mm 胶片卷轴、`text_matte_dissolve` 字体遮罩星尘消散）；其中 `ui_flow_stage`、`polaroid_grid`、`filmstrip_reel`、`text_matte_dissolve` 原生支持传入本地活视频 `video` / `fill_video` + `source_in`（构建期自动预裁并同步为静音 H.264 写入 `<video data-start>`，彻底告别 PPT 静图感），并保留 `image` 向后兼容与共享 `theme` 风格令牌（`editorial_scrapbook` / `neon_cyber` / `cinema_clean`）。
  - **离线自定义 HTML 逃生舱**：支持传入本地 `.html` 项目或内联 HTML/CSS/SVG/GSAP 代码与 `--variables` 参数注入；默认启用纯离线静态检查，自动拦截外部 `http(s)://` 脚本、样式或字体拉取（显式 `allow_network: true` 除外），默认注入本机静态 CJK 字体（`@font-face`）与本地 GSAP。
  - **严格 Alpha 锁定与质检**：与 EffectCraft 同构导出 **`ProRes 4444 RGBA (.mov, yuva444p12le)`** 直通 FilmCraft 叠加轨，默认先跑 `hyperframes lint --json`（可选 `check: true` 进行布局/WCAG 对比度/首帧空屏检查），并复用内容寻址 `overlay-cache` 与 `ffmpeg -v error -xerror` 严格解码验收。
- **使用边界**：需本机安装 Node.js (>=22) 与 `@hyperframes/cli`（支持隔离装于 `%LOCALAPPDATA%\OpenEditTools\hyperframes` 或 `OPENEDIT_HYPERFRAMES_CLI`）及本地 Chromium/Chrome；渲染过程强制关闭遥测与自动更新，严禁调用交互预览 GUI。

<a id="filmcraft-grading"></a>
## 调色与 Lumetri

- 通过 `clips[].grading`（及 `composites[].grading`）接入 FilmCraft 原生 Lumetri 引擎，完整支持连续参数与二级控制：
  - 基础与创意调色：`exposure`、`contrast`、`saturation`（独立映射 Lumetri 基础饱和度）、`highlights`、`shadows`、`whites`、`blacks`、`temperature`、`tint`、`creative_sat`、`vibrance`、`sharpen` / `sharpness`、`faded_film`、`look_intensity`、`vignette`（`-5..5`） / `vignette_midpoint` / `vignette_roundness` / `vignette_feather`。
  - 全曲线与三色轮：亮度/RGB 曲线（`luma_curve`、`rgb_curves`）、5 组 HSL/Hue 曲线（`hue_vs_sat`、`hue_vs_hue`、`hue_vs_luma`、`luma_vs_sat`、`sat_vs_sat`）以及三区色轮（`wheels` / `primary_wheel` 的 `shadows` / `midtones` / `highlights` `[x, y]` 与 `*_l`）。
  - 二级 HSL、3D LUT 与局部遮罩：完整 `hsl` 二级抠像（或单色简写 `red_hsl` / `green_hsl` / `blue_hsl`）、前置 `input_lut` 与创意 `lut` / `look`（支持标准 33×33×33 `.cube` 电影风格 LUT，配合 `look_intensity` 控量），以及基于 Lumetri 椭圆遮罩的 `local_circle`。

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

- `inspect` 与 `reference` 基于 `ffprobe` / `ffmpeg`（已验证至 FFmpeg 9.0.2，使用现代 `-fps_mode vfr` 抽取参考帧与镜头切换候选）探测本地音视频与图片流参数、像素格式 Alpha 通道及 VFR 标志；`inspect` 支持 `--contact-sheet` 一键拼合批量缩略条带总览图，以及 `--analyze-framing` 复用 YuNet 人脸中心/头顶留白与**景别估计**（`shot_scale`: `ECU|CU|MCU|MS|WS`）、清晰度与构图美学综合分（`aesthetic_score`/`sharpness_score`）、三分法裁切推荐（`rule_of_thirds_crop`）、首尾冗余滞留修剪建议（`trim_suggestion`）、帧间硬切检测与底栏硬字幕扫雷；`reference` 同时支持显式 `--detector pyscenedetect`（基于已装 PySceneDetect 0.7.2 `ContentDetector` 提取 `scenes`/`cuts`）并在所有模式额外输出 **`editing_rhythm` 拉片节奏谱**（ASL 平均镜头时长、4 分段速率曲线、Oklab 主色三色板与景别序列）。
- `normalize-media` 将手机录制的可变帧率（VFR）或 10-bit Hi10P 编码转为恒定帧率（CFR）8-bit H.264/AAC 或 ProRes；传入文件夹时自动进入**批量模式**，跳过已合规的 8-bit CFR 素材，仅转换 VFR/Hi10P 异常文件并输出可直接用于 `migrate --map` 的 `path_map` 与 `prepare-report.json`（加 `--force` 强制重编全部文件）。
- **素材来源核查（`.provenance.json`）**：构建前优先于落盘目录强制检查同目录 sidecar `*.provenance.json`，核对 `license_verification == "agent_review_recorded"`、`review_note` 与实测 `sha256`，校验不通过立即中止生成，并在成果目录写出 `素材来源.md` 与 `素材来源.json`。

<a id="transcription"></a>
## 离线语音转写（Whisper）

- 通过 `transcribe` 调用本地 `faster-whisper` 进行离线 ASR 语音识别，输出带段级/词级时间戳的 JSON 或 SRT。
- **边界**：包内不内置大模型权重，调用方需通过 `--model` 传入本机已下载的 Whisper 权重目录或标识；缺本地权重时在 `checks` 中明确跳过，不冒充通过。

<a id="separation"></a>
## 离线人声与伴奏分离（Demucs）

- 通过 `separate` 调用本地 `onnxruntime` 运行 Demucs ONNX 权重，分离人声（vocals）与伴奏（accompaniment）。
- **边界**：需调用方自备并通过 `--model`、`OPENEDIT_DEMUCS_MODEL` 或默认 `%LOCALAPPDATA%\OpenEditTools\models\demucs\` 自动发现传入 Demucs ONNX 权重（`doctor` 会在 `local_models` 中报告已发现的本地模型）。

<a id="auto-cut"></a>
## 离线气口 / 静音粗剪（auto-cut）

- 通过 `auto-cut` 自动移除停顿与静音，按 `silero_vad.onnx`（512 采样点 + 64 上下文 Silero VAD v5 推理）或内置 **FFmpeg `silencedetect` 回退**识别语音段，生成带安全首尾 padding、可选无咔嗒声 `voice` 音频淡入淡出的已校验 `edit-plan.json` 与 `cuts.json`。
- **边界**：纯静音输入不会写出空片段无效计划（返回 `kept_count=0, plan_path=null`）；若需自然呼吸停顿节奏，应结合 `cuts.json` 边界时间戳审阅微调 `--threshold` / `--min-silence`。

<a id="ai-matting"></a>
## 非绿幕离线 AI 抠像（matting -> ProRes 4444）

- 通过 `matting` 支持 **MatAnyone 2**（`matanyone2.pth` 跨帧记忆视频抠像，经隔离 PyTorch CUDA 环境调用；支持 `--mask` 首帧蒙版或本地 BiRefNet 自动起模、`--max-size`、`--warmup` 与 `--decontaminate` 边缘去底色溢色；注意采用 **NTU S-Lab License 1.0 默认仅限非商用**）、**RVM**（`rvm_*.onnx` 时域循环状态，GPL-3.0）、**BiRefNet**（`birefnet*.onnx`，MIT）与 **RMBG**（`rmbg*.onnx`，仅非商业用途）生成带直通道 Alpha 的 `ProRes 4444 RGBA (.mov, yuva444p12le)` 与 `preview_rgba.png` / `preview_alpha.png`；新增动漫精修开关 `--target-fps`（如 `30`fps 智能抽帧提速）、`--defringe`（边缘去白边）、`--clean-color-holes`（如 `blue_sky` 清理发缝/丝带闭合蓝天残留洞）与 `--bottom-feather`（半身像底部渐隐羽化）。
- **边界**：ONNX 模式 CPU 默认可控边界（`--max-frames` 默认 600）；报告新增逐帧 `min_fg_ratio`、`top_band_min_fg_ratio`、`max_frame_fg_drop` 与 `dropout_warning` 指标辅助排查削顶或帧间突变（仍不替代实景合入后的发丝/边缘人工检视）；支持 `--gpu` / `--device cuda|auto` 启用 CUDA 加速。

<a id="auto-reframe"></a>
## 竖屏智能重构图（auto-reframe：16:9 ↔ 9:16）

- 通过 `auto-reframe` 先用 PySceneDetect / FFmpeg 切出硬切镜头边界（保证摇镜关键帧不跨切点滑动），再按帧提取 **OpenCV YuNet 人脸检测**（MIT，自动发现 `%LOCALAPPDATA%\OpenEditTools\models\face\`）与梯度/运动显著性fallback，做镜头内曲线平滑与死区抑制。
- 自动换算 FilmCraft `fit="cover"` 下的防黑边约束坐标，写出可直接在 FilmCraft GUI 手动精修的 `edit-plan.json` 与 `reframe.json`。

<a id="rough-cut"></a>
## 一键粗剪联动流水线（rough-cut）与快速草稿模式

- `rough-cut` 将 `auto-cut`（Silero VAD / FFmpeg 去静音气口）→ `transcribe`（Whisper 词级时间戳，支持 `--gpu` CUDA 加速）→ 片段严格限域字幕 / `--karaoke-mode` 逐词高亮映射串联为一条指令，直接产出校验通过的粗剪 `edit-plan.json`。
- `build --draft`、`export --draft` 与 `render-overlay --draft` 打通 FilmCraft `--scale 0.5`、`--gpu-rendering` 与 EffectCraft `--resolution half --quality draft`（草稿与正式版叠层缓存哈希严格隔离），显著加速前期粗剪审片。

<a id="audio-master"></a>
## 音频响度母带、卷积混响与侧链闪避（audio-master）

- `audio-master` 基于 FFmpeg 双遍线性 EBU R128 `loudnorm` 与 PCM16 统计，内置 `streaming`（`-14 LUFS / -1.0 dBTP`）、`podcast`（`-16 LUFS / -1.5 dBTP`）与 `broadcast`（`-23 LUFS / -1.0 dBTP`）预设，输出 `mastered.wav` 与 `loudness-report.json`（含前后积分响度、真峰值、削波采样数与 `--measure-only` 只测不写模式）。
- **卷积空间混响与侧链压低**：支持 `--reverb-ir`（内置程序脉冲 `room` / `hall` / `cathedral` / `tunnel` / `plate` 或本地脉冲响应 `.wav` via FFmpeg `afir`）与 `--reverb-wet`，以及 `--sidechain`（经 FFmpeg `sidechaincompress` 由人声/音效触发自动闪避压低背景音，可配 `--sidechain-threshold`/`--sidechain-ratio`/`--sidechain-attack`/`--sidechain-release`）。若自行接入 OpenAIR 等第三方高校声学脉冲文件，需单文件复核其商用授权（默认仅依赖 MIT/CC0/内置算法合成）。

<a id="music-structure"></a>
## 离线音乐结构、下行拍与副歌高潮分析（music-structure）

- 通过 `music-structure` 对音乐或视频音轨进行纯离线节拍与乐段结构切分：自动优先采用 **Beat This! ONNX**（CP-JKU `beat-this`，MIT 协议，CUDA/CPU ONNX 推理 `50.0fps` 精准节拍网格）、支持显式 `--backend allin1 --gpu` 调用隔离 CUDA 环境运行 **allin1**（`harmonix-all`，MIT 协议，识别 `start` / `verse` / `chorus` / `bridge` / `outro` 语义乐段、下行拍与副歌 Drop 点），或无模型时使用 `librosa` / 频谱通量 `spectral` 回退，输出 `structure.json`。

<a id="sfx-design"></a>
## 程序化立体声电影音效合成（sfx）

- 通过 `sfx`（纯 NumPy + SciPy DSP）按剪辑点/节拍偏移或直接传 `--from-structure <music-structure目录或structure.json>` 自动读取下行拍/Drop/乐段边界，直接离线生成 48kHz 立体声 PCM16 `sfx_bed.wav`，内置 `whoosh`（立体声左右声像扫频）、`riser`、`sub_drop`、`impact`、`katana_draw`、`laser_sweep`、`glitch_tick` 与 `bass_swell` 8 类音效及平滑软限幅（确保零满幅削波）。
- 同步生成按微秒严格递增的 BGM 闪避包络 `bgm_duck_keyframes.json`，可直接写入主音轨 `audio[].keyframes` 与人声/音效形成呼吸压低配合。

<a id="sfx-search"></a>
## 本地音效库零样本语义搜索与 Freesound CC0（sfx-search / stock）

- 通过 `sfx-search` 对本机拟音/SFX 目录进行零样本自然语言搜索：内置 **LAION CLAP HTSAT ONNX**（Apache-2.0，CUDA/CPU）声学-文本跨模态嵌入检索（外部 `--index-cache` 支持按文件长度与 `mtime_ns` 增量缓存复用）与声学描述符（谱质心/平坦度/起音瞬态）离线回退，精准匹配 `whoosh` / 金属打击 / 环境雨声等素材。
- Online 素材工具 `stock` 同步接入 **Freesound APIv2（CC0-only 硬性过滤）**，在提供 `FREESOUND_API_KEY` 后显式按 CC0 公有领域协议搜素与落盘 `.provenance.json` 溯源伴生卡，绝不混入未授权音效。

<a id="motion-flow"></a>
## 神经/稠密光流分析与 OpenColorIO 2.6 AgX/ACES 3D `.cube` LUT（motion-analyze）

- 通过 `motion-analyze` 支持 **NeuFlow v2 ONNX**（Apache-2.0，CUDA/CPU 神经网络稠密光流）与 OpenCV Farneback 估算每个素材的入场/出场运动方向（`entry_vector` / `exit_vector`）、推拉散度（`zoom_divergence`：`push_in` / `pull_out`）、动作爆发峰值（`action_peaks`）与视觉重心轨迹（`centroid`），辅助确定动势接应转场与音效卡点。
- 基于 **OpenColorIO 2.6**（BSD-3-Clause）与 **Oklab/OpenDRT** 算法导出标准 Adobe 33×33×33 `.cube` LUT：内置 `agx_base`、`agx_punchy`、`aces_cinema`、`aces_filmic_warm`、`opendrt_filmic`（OpenDRT 高光肩部柔顺滚落）、**`match_reference`**（`--match-reference <参考片或图像>`，以 Oklab MKL 协方差色彩迁移对齐目标影调并附带主色板 `target_palette_hex`）及 `steel_violet` / `cinema_warm` / `teal_orange`，独立生成 LUT 时无需传入 `--input` 视频，可直接喂给 FilmCraft `clips[].grading.lut`。

<a id="interpolate"></a>
## Vulkan RIFE 神经网络补帧与丝滑慢动作（interpolate）

- 通过 `interpolate` 调用独立便携二进制 **`rife-ncnn-vulkan`**（MIT 协议，本机 Vulkan GPU，内置 `rife-v4.6` / `rife-anime` 等模型）或内置 FFmpeg `minterpolate` 补帧升格（`--multiplier` / `--target-fps`）与高质慢动作（`--slow-factor` 如 `0.5` 二倍慢放，自动串联标准 `atempo` 音频同步）。
- 支持 `--deband` 挂载 FFmpeg 去色带滤镜消除 8-bit 动漫渐变断层，导出后自动经 `ffmpeg -v error -xerror` 全流严苛解码核验。

<a id="depth-parallax"></a>
## 时域视频深度估计、光学虚化与 2.5D 视差层（depth）

- 通过 `depth` 调用隔离 CUDA PyTorch 工作的 **Video Depth Anything Small**（Apache-2.0，跨帧时域一致深度图）或 **Depth Anything V2 Small ONNX**（Apache-2.0，图像与单帧高速 CUDA/CPU 推理，带轻量梯度回退），导出 `depth.mp4` / `depth.png` 与中帧预览 `preview_depth.png`。
- 支持可选 `--dof-blur` / `--dof-focus` 按深度掩膜生成焦点光学景深散景预览，及 `--parallax-layers` 自动分离前后景并编码带直通道 Alpha 的 **`ProRes 4444` (`yuva444p12le`)** 与 EffectCraft 3D 视差合成规范。

<a id="shot-search"></a>
## 离线双语自然语言以文搜镜（shot-search）

- 通过 `shot-search` 基于 **Google SigLIP 2 ONNX**（Apache-2.0，`CUDAExecutionProvider`/CPU）与 `PySceneDetect` / 等距抽帧，支持纯离线**中英文双语**自然语言搜寻长素材库中的特定镜头（如 `"bright red fire explosion flash"` 或 `"蓝色大海波浪"`），输出带时间码区间、得分排名与缩略图的 `shot-search-report.json`；加 `--with-motion` 时同步对 top-K 命中镜头计算 `motion-analyze` 入出场光流方向、推拉趋势与动作峰值；无权重时提供颜色直方图离线回退，并支持外部 `--index-cache` 增量索引复用。

<a id="shader-fx"></a>
## 无头 ModernGL GPU Shader 转场与 ISF 滤镜（shader-fx）

- 通过 `shader-fx` 在纯无头 **ModernGL OpenGL 3.3 Core**（`create_standalone_context`，绝不弹窗或启动 GUI 桌面）中实时编译与执行行业标准着色器：
  - `transition`：兼容 MIT **`GL-Transitions`** 协议，内置 10 种常用炫酷转场预设（`directionalwarp`、`crosszoom`、`glitch_memories`、`swirl`、`radial`、`zoom_in_circles`、`wind`、`dreamy_zoom`、`burn`、`chromatic_split`）及自定义 `--shader <custom.glsl>` 与 `--params` 参数覆盖。
  - `effect`：内置 **ISF**（Interactive Shader Format，MIT）JSON 头与 `prevImage` / `nextImage` / `bgImage` 采样器，提供 **14 套** 滤镜预设：`halation_bloom`、`prism_chromatic`、`film_grain_vignette`、`glitch_scanline`、`radial_zoom_blur`、技术流 5 件套（`duotone_grade`、`color_pop`、`neon_strobe`、`dot_grid_backdrop`、`echo_chromatic_snap`）、**5 级 Mipmap Bloom `deep_glow`**、**时域快门角运动模糊 `shutter_motion_blur`**，以及透明立绘/抠像动势与光学融合两件套：**`layer_motion_blur`**（出入场预乘 Alpha 安全方向模糊）与 **`light_wrap`**（按 `--bg` 底层高亮漫射包裹边缘），并支持自定义 `.fs`；加 `--alpha` 直接输出保留透明通道的 **`ProRes 4444` (`yuva444p12le`)** `.mov`（完整支持从 `yuva444p12le` / RGBA 视频直读透明通道）供时间轴叠层使用。若需人物立绘真 Live2D 眨眼/发丝飘动/呼吸动画，请直接调用内置 [`mesh-avatar`](#mesh-avatar)，绝不用整图正弦扭曲替代。

<a id="ducking"></a>
## 自动音频闪避（Ducking）

- 通过 `ducking` 读取人声音频（`--vocals`）或语音时间戳 JSON（`--speech-json`），基于实时响度包络门限（`--duck-db`、`--attack`、`--release`、`--floor-margin-db` 等）自动生成 `audio[].keyframes`（要求 `t=0` 起步且源/本地时间严格递增），交由 FilmCraft 编译为 A 轨音频增益关键帧实现背景音乐自动压低。

<a id="offline-denoise"></a>
## 离线音频降噪（FFT）

- 通过 `denoise-audio` 对本地音频或音轨片段执行频谱 FFT 降噪，输出无损 PCM WAV 供后续剪辑计划引用，显式保留原始噪声基准与增益设置边界。

<a id="beat-editing"></a>
## 离线节拍检测与按拍切镜

- 通过 `beats`（基于 `librosa`）或 `music-structure`（Beat This! ONNX / allin1 `structure.json`）提取节拍候选；通过 `beat-apply`（支持消费 `music-structure` 并加 `--use-downbeats` 仅对齐下行拍）按 `--every N` 生成对齐项目帧率的剪切计划或音频标记，并与 `music-edit compile` 的 80ms 吸附窗口协同工作。

<a id="offline-tracking"></a>
## 离线单目标跟踪

- 通过 `tracking`（基于 OpenCV CSRT 与 PyAV 真实 PTS 解码）追踪选定视频区间内单目标 ROI 轨迹；通过 `tracking-apply` 将轨迹映射为目标层的 `position_x` / `position_y` 关键帧。

<a id="project-audit"></a>
## 端到端项目质检与联系表（audit）

- 通过 `audit --plan <edit-plan.json> --out <新目录> [--export <成片.mp4>] [--build-dir <build目录>] [--beats <music-structure|beats目录>] [--preset streaming|podcast|broadcast] [--contact-sheet] [--visual-qc] [--motion-review] [--font-check <ttf/ttc>]` 对成品交付执行一键自动化审计：
  - 主视频轨重复源区间重叠检查（`source_overlaps`）；
  - 成片及 `build-dir/overlays/*/*.mov` 的全流严格解码核验（`ffmpeg -v error -xerror`）；
  - 双轨响度、真峰值与 0 削波采样体检（复用 `audio-master` EBU R128 指标）；
  - V1 切点与节拍/Drop 目标的时间偏差统计（中位数/最大值毫秒误差）；
  - **视觉卫生、动态帧差与字体门禁（`visual_qc` / `pacing_qc` / `motion_qc` / `--motion-review`）**：在生成 `qc_contact_sheet.jpg` 或传入 `--visual-qc` 时顺带抽检高亮过曝占比（`>= 35%` 亮斑告警）、全黑死帧（`>= 95%` 黑帧告警）、相邻采样帧 Oklab 跳变（`max_adjacent_oklab_delta`），并用本地 TTF/TTC `cmap` 校验 `captions`/`overlays` 全字符覆盖率拦截 `[?]` 豆腐块；只要提供 `--export`，自动采样计算 `motion_qc`（`dynamic_ratio` 活跃运动占比、连续 `>= 0.8s` 静态冻屏 `frozen_segments` 告警、V1 切点前后运动能量 `cut_continuity`）；加 `--motion-review` 可额外输出带时间码 HUD 的轻量 **`motion_review.mp4`**（360p/15fps）与 V1 切点前后 `±120ms` 四联爆裂条带 **`cut_strips.jpg`** 用于高速动态审片；
  - 优先覆盖 V1 切点的 `qc_contact_sheet.jpg` 拼图联系表。
- **边界**：`audit` 的 `status == "passed"` 仅证明技术数据与指标达标，`continuous_audiovisual_review` 始终标为 `"unverified"`，不替代人类连续看片审美确认。

<a id="mesh-avatar"></a>
## 真 2D 网格 Live2D 透明人物动画（mesh-avatar）

- 通过 `openedit mesh-avatar status`（或 `openedit doctor` 的 `mesh_avatar` 字段）自动探测本机配置的 **Mesh Avatar Studio**（MIT WebGL2 2D 网格立绘运行时与 Playwright 无头浏览器管线）。
- **核心原理与真实表现**：严格按插画分层补底与骨骼物理驱动——eye 区域按多边形拆出独立眼球层、反混合上下睫毛层并对眼眶补肤色底图实现清晰不涂抹的自然眨眼；头发按“发根锁死（`weight=0`）→ 发梢 4 节点高阻尼弹簧链”独立驱动飘动而不扭曲帽檐、脸部或衣领；头部绕颈椎支点做 `2.5D` 微转头与 `3.2–4.6s` 慢呼吸曲线。
- **双路径交付与 1080p 直出**：支持**单张正面透明立绘零外发短路径**（`new-project.py` → 网格精准标点 → `build-layers.py` 拆层视觉验收 → `pack-avatar` / `validate-avatar` → `openedit mesh-avatar render --width 1920 --height 1080 --fps 30 --codec prores_4444` 直出锁定 `ProRes 4444 RGBA .mov`，自动发现本机 Chrome/Edge 并通过严格解码核验）以及视频情境变体流程，详情见 [Mesh Avatar 流程与规范](mesh-avatar.md)。
- **适用边界**：面向正面或微侧半身透明立绘；**90° 纯侧脸或大遮挡转身镜**应保持刚体锐利剪影，改用 `layer_motion_blur` + `light_wrap --bg` 与缓慢单向漂移。

## 其他扩展能力与验收边界

- **Mesh Avatar 透明形象**：完整流程、坐标规范与变体授权边界详见 [`mesh-avatar`](#mesh-avatar) 与 [Mesh Avatar 流程与规范](mesh-avatar.md)。
- **10-bit H.264 (Hi10P) 兼容守卫**：实测 FilmCraft 对 `h264` + `yuv420p10le` 等 10-bit H.264 编码会静默渲染为 `MEDIA UNREADABLE` 警告画面（12-bit ProRes 4444 与 8-bit `yuv420p` 不受影响）；`inspect`、`validate` 与 `build` 已内置自动识别与前置拦截，提示先执行 `normalize-media` 转为 8-bit `yuv420p` CFR。
- **多模态音乐驱动编排（`music-edit`）**：通过 `prepare` -> `analyze` / `review` -> `compile` 对接使用者自配的 OpenAI 兼容或 Gemini 原生视听模型（仅在显式 `analyze` / `review` 时产生请求与 API 用量；支持将 `beats` 或 `music-structure` 节拍直接编译为纯开源 `openedit`/`filmcraft` 计划）。详见 [原生视听与音乐编排](music-edit.md)。
- **验证分级声明**：`validate` 通过 ≠ `build` 通过 ≠ `export` 全解码通过 ≠ 人类主观视听认可；关键视觉效果应同时结合 `preview` PNG 帧、`audit` 联系表及使用者最终看片反馈。
