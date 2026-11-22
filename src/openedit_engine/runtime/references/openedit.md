# FilmCraft 与 EffectCraft 开源工作流

OpenEdit Engine 将 **ArtCraft FilmCraft**（非线性主剪辑、调色、字幕与导出）与 **ArtCraft EffectCraft**（合成动效与透明叠加）直接整合在统一白名单 CLI（`openedit`）和本机 MCP 服务中，全程以明文工程（`.fcproj` / `.ecproj`）驱动，无需逆向商业草稿，也不启动任何图形界面弹窗。

## EffectCraft

- **定位**：MIT/Apache-2.0 纯 Rust 动效合成内核，提供 **308 种内置特效**、9 种图层样式、形状/纯色/文字/外部片段层、文字动画器、蒙版、表达式、3D 相机/灯光、时间重映射/运动模糊以及 AE 风格 JSX 脚本能力。
- **ProRes 透明通道动效锁定**：当需要把高级文字动画、发光粒子、装饰卡片或表达式合成层叠加到主时间轴时，统一使用 EffectCraft 编译为 **`ProRes 4444 RGBA (.mov, yuva444p12le)`** 中间件（已实测完整保留 Alpha 透明度并无缝进入 FilmCraft V2+ 轨道；默认 CLI WebM VP9 导出会丢 Alpha，严禁作为透明叠层交付）。
- **两种使用方式**：
  1. 在 `edit-plan.json` 的 `overlays[]` 字段中声明图层或 `script`，执行 `openedit build` 时自动完成全部 EffectCraft 渲染、RGBA 校验与 FilmCraft 轨道合入（声明式图层与 `recipe` 自动启用基于内容哈希的外部叠层缓存 `%LOCALAPPDATA%\OpenEditEngine\overlay-cache`，可用 `OPENEDIT_OVERLAY_CACHE_DIR` 覆盖或加 `--no-overlay-cache` 禁用）。
  2. 独立制作 ProRes 透明通道动效或直接调用底层引擎：
     - `openedit render-overlay --spec <spec.json|inline_json> --out <新目录> [--no-video] [--gpu] [--no-cache]`
     - `openedit effectcraft-exec --commands <json> [--script <jsx文件或源码>] [--project <in.ecproj>] [--save-as <out.ecproj>]`

## FilmCraft

- **定位**：MIT/Apache-2.0 纯 Rust 非线性剪辑与调色混音内核，工程为明文 JSON 格式 `.fcproj`，支持自由画布（`64..7680` × `64..4320`，`1..120fps`）、多轨 V/A 排布、Lumetri 曲线/色轮/HSL/3D LUT、26 种混合模式、富文本多色高亮与烧录字幕。
- **核心操作与 CLI 入口**：
  - 环境与能力探测：`openedit doctor`、`openedit backends`、`openedit capabilities --query blur --live`
  - 媒体准备与 AI 工具：`openedit inspect`、`openedit reference [--detector ffmpeg|pyscenedetect]`、`openedit normalize-media`（单文件或目录批量 VFR/Hi10P → 8-bit CFR）、`openedit auto-cut`、`openedit rough-cut`（去静音+字幕一步出工程）、`openedit auto-reframe`（横竖屏自动跟拍重构图）、`openedit matting`（非绿幕 AI 抠像，含 MatAnyone 2/BiRefNet/RVM/RMBG → ProRes 4444 RGBA）、`openedit audio-master`（双遍 EBU R128 响度母带与质检）、`openedit sfx`（8 类程序化立体声效合成与 BGM 闪避曲线）、`openedit motion-analyze`（光流运镜衔接分析与 33³ .cube LUT 生成）；`transcribe` / `separate` / `matting` / `auto-cut` 均支持可选 `--gpu` CUDA 加速。
  - 校验与编译：`openedit validate --plan <edit-plan.json>`、`openedit build --plan <edit-plan.json> --out <新目录> [--draft] [--export-scale 0.5] [--preview-times 0.5 2.0] [--export <out.mp4>] [--export-otio <timeline.otio>] [--export-fcpxml <timeline.fcpxml>]`
  - 二次审片与导出：`openedit preview --project <in.fcproj> --out <目录> --times 0.5 2.0`、`openedit export --project <in.fcproj> --out <out.mp4> --format h264|hevc|prores|wav|aac [--draft] [--scale 0.5] [--gpu-rendering auto|off] [--hardware-encoding off|auto]`
  - 高级直通逃生舱：`openedit filmcraft-exec --commands '[{"id":"...", "params":{...}}]' [--project <in.fcproj>] [--save-as <out.fcproj>]`（直通全部 718 条 FilmCraft MCP 命令）

## 质量检查与导出验收

- `build` 与 `export` 在写出最终 `.mp4` / `.mov` 后自动调用 `ffprobe` 校验音视频流完整存在，并执行 `ffmpeg -v error -xerror -i <导出文件> -f null -` 进行严格全流解码检查。
- 成片输出目录自动生成 `edit-plan.json`、`<name>.fcproj`、`overlays/`（如有）、`build-report.json`、`剪辑交付.md`、`素材来源.md` 与 `素材来源.json`。用户如需主观精调，可随时在 FilmCraft / EffectCraft 桌面客户端中直接打开这些明文工程继续编辑。
