# 开源剪辑步骤

本指南说明如何使用纯开源后端（ArtCraft **FilmCraft** + **EffectCraft** + **FFmpeg** + 本地离线 AI 工具）完成从素材诊断到工程编译、抽帧预览、成片导出与严格全解码验收的标准开源剪辑步骤与执行链路。开始制作或修订前，先按 [创作决策层](planning.md) 完成构思、选段与声画设计。

## 创作衔接与成片类型分区

- **分流、同款参考与双线判断**：需求分流以 [分流（唯一维护位置）](planning.md#分流唯一维护位置) 为准，不在此重复分流规则；每版按照 [每版同时设计剧情与剪辑](planning.md#每版同时设计剧情与剪辑) 与 [创作判断流程](planning.md#创作判断流程) 展开 6 步创作判断，仅在核心目标冲突、不可替代素材缺失或需外部付费/外发授权时询问用户。
- **整版重剪 vs 局部修改**：整体优化默认从原素材建立新版本目录与 `edit-plan.json`（复用分析与反馈，旧版时间轴只作对照）；用户指定局部修改时只重审受影响镜头、阅读与相邻连接，不重剪全片也不覆盖用户手工调整后的工程。
- **片型与专项方法路由**（先定表达目的再选片源，混合片按段落切换）：

| 类型或专项问题 | 按需读取 |
|---|---|
| 漫剪、节奏混剪、情绪 MV | [漫剪与节奏混剪](../references/styles/anime-mix.md) |
| Vlog、旅行、生活记录 | [Vlog](../references/styles/vlog.md) |
| 纪实、纪录片、访谈 | [纪录片与纪实](../references/styles/documentary.md) |
| 剧情解说、知识、教程 | [讲解与解说](../references/styles/explainer.md) |
| 产品、商业、品牌展示 | [商业展示](../references/styles/commercial.md) |
| 剧情叙事、相册、活动、口播与通用连接 | [通用剪辑方法](../references/general-editing.md) |
| 绿幕/蓝幕去底、分层揭示、镜头去重与修订 | [绿幕](../references/green-screen.md)、[分层揭示](../references/layered-reveal.md)、[镜头使用与去重](../references/general-editing.md#镜头使用与去重)、[创作修订](../references/creative-review.md) |

## 1. 环境与后端就绪检查

1. 首先核对本机纯开源工具链状态（或经 MCP `describe_operations(include_backends=True)`）：
   - `openedit doctor`（确认 `ready: true`，核实 `filmcraft`、`effectcraft`、`ffmpeg` 与本机 GPU/AI 扩展可用性）。
   - `openedit capabilities --query blur --live`（按需检索 FilmCraft / EffectCraft 实际可用命令与特效名称，所有字段规范以 [能力状态](../references/capabilities.md) 与 [计划规范](../references/plan-format.md) 为准）。

## 2. 素材探测、预处理与创作检索

1. **媒体检查与参考探测**：使用 `openedit inspect --input <文件或目录> --out <新探测目录>` 查看分辨率、编码、帧率、音轨与 VFR/Hi10P 警告；需要观察本地参考视频切点候选时使用 `openedit reference --detector pyscenedetect`。
2. **VFR / Hi10P 选择性标准化**：若检测到手机 VFR 视频或 **10-bit H.264（`yuv420p10le` / Hi10P）**，先执行 `openedit normalize-media --input <源视频或目录> --out <输出目录>` 转为恒定帧率 8-bit `yuv420p` CFR；长片或大体积片源可先用轻量代理与 `shot-search [--with-motion]` 初筛命中区间，再选择性标准化选中片段，节省算力并防止声画漂移或 `MEDIA UNREADABLE`。
3. **可选离线 AI 辅助与创作知识检索**：
   - 使用 `openedit knowledge "<问题>"` 或 MCP `search_knowledge` 检索叙事、声画关系、转场、混合与构图方法，并用 `guide` / `read_guide` 读原文。
   - 按任务白名单按需调用：`music-structure`（Beat This!/allin1 乐段/下行拍/Drop 分析）、`shot-search [--with-motion]`（SigLIP 2 双语搜镜与出入场动势）、`motion-analyze`（NeuFlow v2/Farneback 光流 & AgX/ACES `.cube` LUT）、`sfx [--from-structure]` / `sfx-search`（程序化音效合成 & CLAP 检索）、`interpolate`（Vulkan RIFE 补帧/慢放，自定义慢放倍数用 `--model auto`/`rife-v4.6`）、`depth`（Video Depth Anything/Depth Anything V2 景深视差）、`shader-fx`（ModernGL GL-Transitions & ISF Shader）、`matting`（MatAnyone 2/BiRefNet/RVM 非绿幕抠像）、`auto-reframe`（16:9 ↔ 9:16 竖屏智能跟拍重构图）、`rough-cut`（去气口+字幕一步出粗剪工程）、`audio-master`（双遍 EBU R128 响度母带、`afir` 卷积混响与 `sidechaincompress` 侧链压低）、`beats` / `beat-apply [--use-downbeats]`、`transcribe`、`separate`、`ducking`、`denoise-audio`、`captions`、`tracking` / `tracking-apply`。
   - 前期快速迭代可对 `build` / `export` / `render-overlay` 加 `--draft` 生成半分辨率快速预览草稿（最终交付前去掉 `--draft` 以全分辨率严格全解码导出）。

## 3. 编写计划、校验、轨道整理与工程编译

1. 在项目独立目录下编制 `edit-plan.json`（规范见 [计划格式](../references/plan-format.md) 与 [开源工作流](../references/openedit.md)）：
   - 支持自由画布尺寸（`64..7680` × `64..4320`、`1..120fps`），无商业分辨率/时长锁。
   - 主剪辑、转场、Lumetri 调色、运动与不透明度关键帧、26 种混合模式、`highlights` 多色高亮文字与烧录字幕轨写入 `clips`/`audio`/`captions`（有双语歌词或对白时在 `layer_order` 将字幕轨置于常规特效叠层上方；切点与时长按整微秒边界计算防重叠）。
   - 需要 308 种高级特效、图层样式、复杂形状、3D 视差或表达式动效时，声明到 `overlays[]`（支持 13 套内置声明式与 Hero 配方，见 [配方与映射](../references/recipes.md)；编译时自动由 EffectCraft 无头渲染为 **`ProRes 4444 RGBA (.mov)`** 透明叠加层并挂入 FilmCraft 上层轨道）。
   - 所有联网/第三方授权素材均应附带通过校验的 `.provenance.json` sidecar。
2. 执行严格结构与媒体探测校验：`openedit validate --plan <edit-plan.json>`。
3. **可选人工可编辑轨道整理**：多轨显式 `layer_order` 计划在正式编译前，可按 [安全轨道复用](../references/plan-format.md#安全轨道复用) 运行 `openedit layout-tracks --plan <计划> --out <新计划.json>` 合并不同时段的普通视频叠层，审阅映射报告与画面后再决定是否采用。
4. **先编排看图再全量导出**：长片或重特效工程建议先执行 `openedit build --plan <计划> --out <新目录> --preview-times ...`（或 `--draft`）快速产出 `.fcproj` 与关键帧 PNG 并目视核查遮挡/字幕/重影/着色素接缝；确认无误后再执行全分辨率导出与交换文件生成：

```powershell
openedit build --plan '<项目>\edit-plan.json' --out '<项目>\build-v1' --preview-times 0.5 2.0 --export '<项目>\build-v1\final.mp4' --export-otio '<项目>\build-v1\timeline.otio' --export-fcpxml '<项目>\build-v1\timeline.fcpxml'
```

## 4. 抽帧核查、严格全解码与交付

1. **视觉抽帧与端到端质检**：对 `previews/frame-*.png` 逐帧检查文字位置、抗锯齿、透明叠层边界、遮挡层级与构图重心，并运行 `openedit audit --plan '<项目>\edit-plan.json' --build-dir '<项目>\build-v1' --export '<项目>\build-v1\final.mp4' --contact-sheet --out '<项目>\audit-v1'` 检查源区间重叠、全资产严格解码、响度削波、节拍对齐误差与 `qc_contact_sheet.jpg` 联系表拼图。
2. **导出与严格解码**：`build --export` 或 `openedit export` 会自动执行 `ffprobe` 流检查及 `ffmpeg -v error -xerror -i <导出文件> -f null -` 严格全流无错解码验证。
3. **两类验收分离交付**：交付项目目录内的明文 `<name>.fcproj`、`overlays/`（如有）、`build-report.json`、`audit-report.json`（如已运行）、`剪辑交付.md` 与 `素材来源.md` / `素材来源.json`。向用户汇报时须严格按照 [交付与两类验收分离](planning.md#交付与两类验收分离) 分开列出内容/视听表达取舍与技术验证层级，并提醒使用者按需打开 `.fcproj` 在 GUI 中做主观精修与完整连续视听验收。
