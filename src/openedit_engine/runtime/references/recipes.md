# 纯开源剪辑手法映射表与 EffectCraft 动效配方库

本指南将常见“商业剪辑/特效”创作手法直接映射到 **FilmCraft** 时间轴/调色/合成字段、**EffectCraft** `overlays[].recipe` 声明式透明叠层配方（编译为 ProRes 4444 RGBA `.mov` 自动合入主时间轴），以及离线音视频辅助命令。

> 证据边界：配方校验与 ProRes 4444 渲染证明合成结构、Alpha 通道与静帧构图合法；真实片源的阅读停留、色键边缘及连续视听节奏仍须结合 `preview` 与导出结果核查。

## 纯开源剪辑手法映射表

| 创作手法 | 纯开源实现方案 | 关键参数 / 命令入口 | 说明与边界 |
|---|---|---|---|
| 动态标题卡（底框 + 强调线 + 主副标题） | `overlays[].recipe: "title_card"` | `title`, `subtitle`, `highlights`, `text_preset`, `glow` | `Linear Wipe` 揭示底板，`dropShadow` + 可选发光 |
| 人物 / 章节下三分之一条（Lower Third） | `overlays[].recipe: "lower_third"` | `title`, `subtitle`, `align`, `position_y`, `accent_color` | 左/右安全区对齐，竖条伸缩 + 文字滑入 |
| 同句多色重点词弹字强调 | EffectCraft `recipe: "keyword_callout"` 或 FilmCraft `captions[]` | `highlights: [{start, end, color}]`, `text_preset` | 无需复杂入场用 FilmCraft；需弹性文字预设与发光用 `keyword_callout` |
| 卡点闪白 / 切点提亮冲击 | `overlays[].recipe: "beat_flash"` | `peak_time`, `peak_opacity`, `blend_mode: "Add"` | 在切点/重音峰值叠 `Add`/`Screen` 混合模式 |
| 影院黑边遮幅（入画 / 收回） | `overlays[].recipe: "cinematic_bars"` | `bar_ratio`, `animate_in`, `animate_out` | 位于高轨，避免压住关键脸部与底栏字幕 |
| 教程 / 章节进度条 | `overlays[].recipe: "progress_banner"` | `label`, `progress_start`, `progress_end`, `position` | 随段时长线性 `Linear Wipe` 推进 |
| 章节大标题开场（编号徽标 + 分隔线） | `overlays[].recipe: "chapter_opener"` | `title`, `badge`, `subtitle`, `card_style`, `highlights` | 居中卡片或极简悬浮风，带发光/投影与渐退 |
| 透明内框画中画边框（PiP 边框） | `overlays[].recipe: "pip_card"` | `corner`, `title`, `width_ratio`, `aspect`, `border_color` | 内框 100% 透明 Alpha，兼容横屏与竖屏四角安全区 |
| 双栏 / 三栏透明分屏分割线与角标 | `overlays[].recipe: "split_screen_frame"` | `layout: "vertical_2 \| horizontal_2 \| vertical_3"`, `labels` | 面板内保持全透明 Alpha，仅绘制分割线与可选角标 |
| 双色斜切擦除转场 | `overlays[].recipe: "wipe_transition"` | `direction`, `angle_offset`, `feather`, `peak_hold` | 双重 `Linear Wipe` 透明→全遮挡→透明过渡 |
| 渐进分屏与几何窗口揭示 | FilmCraft `clips[].mask` + `keyframes` | `shape`, `center`, `size`, `feather`, `keyframes` | 传 `feather: 0` 覆盖 FilmCraft 默认 10px 羽化 |
| 绿幕 / 蓝幕 / 单色键合成与前后穿插 | FilmCraft `clips[].chroma` + 复制前景同源上轨 + `mask` | `method: "ultra_key" \| "color_key"`, `key_color`, `spill` | 复杂非单色背景人物用 `openedit matting` 转 ProRes 4444 RGBA |
| 多轨成组旋转、缩放与统一调色 | FilmCraft `composites[]` 或 EffectCraft 合成叠层 | `tracks`, `transform`, `keyframes`, `grading`, `mask` | 在 `clip.nest` 嵌套片段外层统一施加运动与校色 |
| 一级/二级调色、色轮与局部校色 | FilmCraft `clips[].grading` | `saturation`, `wheels`, `red_hsl`, `hue_vs_*`, `local_circle` | `local_circle` 自动挂载至 Lumetri 椭圆遮罩 |
| 自动拍点标记、BGM 闪避与响度统一 | `beats` / `beat-apply` / `ducking` + FilmCraft 原生字段 | `audio[].beat_marks`, `fade_in`, `fade_out`, `loudness` | `loudness` 经 `essentialSound.setType` + `autoMatch` |

---

## 10 个内置 EffectCraft 声明式配方（`overlays[].recipe`）

在 `edit-plan.json` 的 `overlays[]`（或 `render-overlay --spec ...`）中直接声明 `recipe` 与 `params`，无需手写长 `layers` 数组或 JSX 脚本；注意**不能与 `layers` 或 `script` 混用**，且画布尺寸/安全区按当前项目 `width` / `height` 自适应缩放。

### 1. `title_card` — 居中主副标题卡片

- **必填**：`title`（`<= 200` 字符）
- **可选**：`subtitle`（`<= 240`）、`highlights`（`[{start, end, color}]` 非重叠字符区间）、`accent_color`（默认 `#38BDF8`）、`text_color`（`#FFFFFF`）、`bg_color`（`#0F172A`）、`bg_opacity`（`0..100`，默认 `82`）、`stroke_color`（`#0B1220`）、`glow`（默认 `true`）、`text_preset`（`none|typewriter|fadeUpCharacters|bounceInWords|trackingIn|scramble|blurIn|jitter|dropInLines`，默认 `fadeUpCharacters`）、`position_y`（`0.1..0.9`，默认 `0.5`）。

```json
{
  "name": "hero_title",
  "track": "overlay",
  "start": 0.2,
  "duration": 2.4,
  "recipe": "title_card",
  "params": {
    "title": "开源双核剪辑引擎",
    "subtitle": "FilmCraft + EffectCraft 声明式工作流",
    "highlights": [{"start": 0, "end": 4, "color": "#FACC15"}],
    "accent_color": "#38BDF8"
  }
}
```

### 2. `lower_third` — 人物 / 章节下三分之一条

- **必填**：`title`（`<= 120` 字符）
- **可选**：`subtitle`（`<= 180`）、`accent_color`（`#38BDF8`）、`bg_color`（`#0B1220`）、`bg_opacity`（`84`）、`text_color`（`#FFFFFF`）、`align`（`left` | `right`，默认 `left`）、`position_y`（`0.15..0.92`，默认 `0.83`）。

```json
{
  "name": "speaker_lt",
  "track": "lower",
  "start": 1.0,
  "duration": 2.6,
  "recipe": "lower_third",
  "params": {
    "title": "主讲人 · 张工",
    "subtitle": "架构设计 / 无头批量合成演示",
    "align": "left"
  }
}
```

### 3. `keyword_callout` — 重点词动效弹字

- **必填**：`text`（`<= 160` 字符）
- **可选**：`highlights`、`color`（`#FFFFFF`）、`stroke_color`（`#0B1220`）、`stroke_width`（`0..24`，默认 `5`）、`size`（`16..300`，默认随分辨率自适应）、`glow`（默认 `true`）、`text_preset`（默认 `bounceInWords`）、`position_y`（`0.1..0.9`，默认 `0.72`）。

### 4. `beat_flash` — 节拍闪白冲击

- **可选**：`color`（默认 `#FFFFFF`）、`peak_time`（相对叠加层起点的本地秒数，默认 `0.35 * duration`）、`peak_opacity`（`1..100`，默认 `85`）、`blend_mode`（`Add` | `Screen` | `Overlay` | `Normal`，默认 `Add`）、`glow`（默认 `true`）。

### 5. `cinematic_bars` — 影院黑边遮幅

- **可选**：`bar_ratio`（上下单边占画高比例 `0.02..0.30`，默认 `0.11`）、`color`（默认 `#000000`）、`animate_in`（入场滑入秒数）、`animate_out`（退场滑出秒数，默认 `0.0`）。

### 6. `progress_banner` — 章节进度条

- **可选**：`label`（`<= 120` 字符）、`progress_start` / `progress_end`（`0..1`，默认 `0.0` → `1.0`）、`bar_color`（`#38BDF8`）、`track_color`（`#1E293B`）、`text_color`（`#FFFFFF`）、`position`（`top` | `bottom`，默认 `bottom`）。

### 7. `chapter_opener` — 章节大标题开场

- **必填**：`title`（`<= 120` 字符）
- **可选**：`badge`（`<= 24`，默认 `"01"`）、`subtitle`（`<= 200`）、`highlights`、`card_style`（`card` | `minimal`，默认 `card`）、`accent_color`、`text_color`、`bg_color`、`bg_opacity`（`78`）、`glow`、`text_preset`、`position_y`（`0.15..0.85`）。

### 8. `pip_card` — 透明内框 PiP 装饰框

- 用于画中画（PiP）悬浮窗边框装饰。**可选**：`title`（角标标题，`<= 80`）、`corner`（`top_left` | `top_right` | `bottom_left` | `bottom_right`，默认 `bottom_right`）、`width_ratio`（`0.18..0.65`，默认 `0.32`）、`aspect`（`0.4..2.5`，默认 `1.7778`）、`border_color`、`border_width`（`1..24`，默认 `6`）、`bg_color`、`text_color`、`glow`（默认 `false`）。框内保持 `alpha=0`，不遮挡下方视频轨。

### 9. `split_screen_frame` — 多栏分屏分割框

- **可选**：`layout`（`vertical_2` | `horizontal_2` | `vertical_3`，默认 `vertical_2`）、`divider_color`（`#FFFFFF`）、`divider_width`（`2..32`，默认 `6`）、`labels`（与栏数匹配的 `2` 或 `3` 项文字数组，每项 `<= 40` 字符）、`badge_color`、`text_color`、`animate_in`。

### 10. `wipe_transition` — 双色斜切擦除转场叠层

- **可选**：`direction`（`left` | `right` | `up` | `down`，默认 `left`）、`color`（主色 `#0F172A`）、`accent_color`（先导条色 `#38BDF8`）、`angle_offset`（`-35..35` 度，默认 `12`）、`feather`（`0..100`，默认 `16`）、`peak_hold`（中点满屏覆盖比例 `0..0.5`，默认 `0.12`）。

```json
{
  "name": "chap_01",
  "track": "overlay",
  "start": 0.0,
  "duration": 2.2,
  "recipe": "chapter_opener",
  "params": {"badge": "PART 01", "title": "开源剪辑工作流", "subtitle": "支持 10 套透明配方"}
}
```

---

## 独立预览与快速调试

```powershell
# 仅验证并生成 EffectCraft 工程及单帧透明 RGBA PNG（不写 MOV，速度最快）
openedit render-overlay --spec hero_title.json --out .\test_overlay --no-video
```
