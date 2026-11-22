# Edit Plan 规范（edit-plan.json）

`openedit validate` 与 `openedit build` 接受纯开源开放剪辑计划 JSON（`schema_version` 为整数 `1` 或 `2`），统一由 `runtime_core.validate_plan` 进行结构、媒体流探测、微秒级时间量化、轨道冲突与素材边界校验。

## 顶层结构

```json
{
  "schema_version": 2,
  "name": "sample-project",
  "canvas": {"width": 1920, "height": 1080, "fps": 30},
  "clips": [],
  "audio": [],
  "captions": [],
  "overlays": [],
  "layer_order": ["video", "overlay", "captions", "C1", "A1", "voice"],
  "export": {"format": "h264", "crf": 20, "hardware_encoding": "off", "audio": true, "burn_captions": true},
  "notes": [],
  "missing_assets": []
}
```

- **必填字段**：`schema_version`（`1` 或 `2`）、`name`（1..200 字符）、`canvas`（`width` 整数 `64..7680`、`height` 整数 `64..4320`、`fps` 有限数值 `1..120`）、`clips`（非空数组）。
- **可选顶层字段**：`mode`、`backend`、`audio`、`captions`、`overlays`（兼容别名 `motion_graphics`，两者自动归一化为 `overlays`）、`effects`、`filters`、`composites`、`layer_order`、`notes`、`missing_assets`、`export`、`allow_vip_effects`。
- **路径解析规则**：所有相对路径基于 `edit-plan.json` 所在目录解析，拒绝 `NUL` 字节与缺失文件。构建时同时校对同目录 `*.provenance.json` sidecar。

## 视频 / 图片片段：`clips[]`

- **必填**：`path`（支持 `.mp4/.mov/.mkv/.avi/.webm/.png/.jpg` 等媒体；若为 VFR 或 10-bit H.264 `yuv420p10le`/Hi10P 编码，FilmCraft 会静默渲染为 `MEDIA UNREADABLE` 红屏，须先用 `normalize-media` 转为 8-bit `yuv420p` CFR）、`start`（`0..86400` 秒）、`duration`（`>0..86400` 秒）。`start` 与 `duration` 均自动量化到整微秒边界。
- **可选核心属性**：
  - `source_in`（默认 `0.0`）、`speed`（默认 `1.0`，允许范围 `0.05..32.0`）：视频要求 `source_in + duration * speed <= source_duration + 0.05s`。
  - `reverse`（布尔，倒放）、`volume`（`0..4.0`；自带音轨默认 `1.0`，纯图或静音默认 `0.0`，`>0` 时在 FilmCraft 联动放置到 `A1` 音轨）。
  - `track`（默认 `"video"`）：同一轨道名称不能混用不同媒体类型，同一轨道内任意两个片段不能时间重叠。
  - `fit`：`contain`（等比完整入框，默认）/ `cover`（等比铺满裁边）/ `fill`（非等比拉伸铺满画布）。当素材尺寸与画布不同时，`transform.scale_x`/`scale_y` 与缩放关键帧会自动在 `fit` 基础倍率上累乘。
- **空间变换与透明度：`transform`**：
  - 支持两种坐标系统：直接画布像素 `position: [px_x, px_y]`，或归一化坐标 `transform_x` / `transform_y`（以画布中心 `(0, 0)` 为原点，按 `x_px = w/2 + vx*(w/2)`、`y_px = h/2 - vy*(h/2)` 映射，推荐范围 `[-1, 1]`，引擎校验范围 `[-10000, 10000]`）。
  - 支持 `scale_x` / `scale_y`（`>0`，若两者不等自动关闭 `uniform_scale` 并独立设置 `scale_width`）、`rotation`（度）、`alpha`（`0..1`）、`flip_horizontal` / `flip_vertical`（布尔，编译为 FilmCraft `horizontal_flip` / `vertical_flip`）。
- **动态关键帧：`keyframes[]` 与 `motion_easing`**：
  - 每项包含 `property`、`time`（相对片段起点的本地秒数，`0..duration`）、`value`、可选 `interpolation`（`linear`、`smoothstep`、`easeIn`、`easeOut`、`bezier`、`hold`、`autoBezier`、`continuousBezier`）。
  - 支持属性：`position_x`、`position_y`、`uniform_scale`、`scale_x`、`scale_y`、`rotation`、`alpha`。同时间点的 `position_x` / `position_y` 自动合并写入 FilmCraft 二维 `motion/position` 关键帧。同一属性与微秒时刻禁止重复。
- **遮罩：`mask`（原生静态 + 关键帧遮罩）**：
  - 形状 `shape`（兼容 `type`）：`rectangle`（`rect`）、`ellipse`（`circle`）、`polygon`、`bezier`；可附带目标 `effect`（默认 `"opacity"`）与 `corner_radius`（px）。
  - 坐标与尺寸：像素坐标 `center: [x, y]` 与 `size: [w, h]`，或归一化坐标 `center_x` / `center_y` 与画布比例 `width` / `height`（`0.0001..10.0`），或多边形/贝塞尔顶点 `path`（或 `points`，至少 3 个 `[x, y]` 点）。
  - 参数：`feather`（`0..1000`px；显式传入 `0` 会覆盖 FilmCraft 默认 `10px` 羽化）、`opacity`（`0..1`）、`expansion`（`-1000..1000`px）、`inverted`（或 `invert`）、`mode`（`none`/`add`/`subtract`/`intersect`/`lighten`/`darken`/`difference`）。
  - 遮罩关键帧 `keyframes[]`：按片段本地 `time`（`0..duration`）动态更新 `feather`、`opacity`、`expansion`、`path`、`center`、`size`（对 `rectangle`/`ellipse` 自动重算四顶点/贝塞尔路径）。
- **色键抠像：`chroma`**：
  - `method`：`"ultra_key"`（默认，专业绿幕/蓝幕抠像，支持 `key_color`/`color` `#RRGGBB`、`setting`、`output`、`transparency`、`highlight`、`shadow`、`tolerance`、`pedestal`、`choke`、`soften`、`spill`、`contrast`、`mid_point`、`desaturate`、`range`、`spill_luma`，别名 `strength`/`feather`）或 `"color_key"`（支持 `color`/`key_color`、`tolerance` `0..255`、`thin` `-5..5`、`feather` `0..50`）。
- **混合模式与转场**：
  - `blend_mode`（字符串，如 `screen`、`multiply`、`overlay` 等）或 `mix_mode`（对象或字符串，支持 `opacity` 设定不透明度）。
  - `transition`：字符串名称（如 `"Cross Dissolve"`）或对象 `{"name": "whip", "duration": 0.14, "frames": 8, "edge": "in"|"out", "align": "center"|"start"|"end", "reverse": false, "params": {"direction": 1, "blur": 90}}`。引擎在所有视频片段全部入轨后统一挂接边界转场，避免首片段无前邻边界失败；除常规溶解外，FilmCraft 原生支持方向动感转场 `whip`、`cross_zoom`、`zoom_blur`、`travel_motion`、`pull_motion`、`motion_camera`、`directional_blur_transition`、`dip_to_black`、`blur_dissolve`、`flash`。
- **调色、滤镜、特效与响度：`grading` / `effects` / `filters` / `loudness`**：
  - `grading`：支持基础调色（`exposure`、`contrast`、`saturation` 映射基础饱和度、`highlights`、`shadows`、`whites`、`blacks`、`temperature`、`tint`）、创意/锐化/暗角（`creative_sat`、`vibrance`、`sharpen`/`sharpness`、`faded_film`、`look_intensity`、`vignette`（`-5..5`）、`vignette_midpoint`、`vignette_roundness`、`vignette_feather`）、曲线（`luma_curve`、`rgb_curves`、`hue_vs_sat`、`hue_vs_hue`、`hue_vs_luma`、`luma_vs_sat`、`sat_vs_sat`）、三色轮（`wheels` / `primary_wheel` 的 `shadows`/`midtones`/`highlights` `[x, y]` 与 `*_l` 亮度）、HSL 二级调色（完整 `hsl` 对象或单组简写 `red_hsl` / `green_hsl` / `blue_hsl`，支持 `lightness` 自动编译至 `hue_vs_luma`）、`input_lut` 与 `lut`/`look`、局部椭圆校色遮罩 `local_circle`。
  - `effects` / `filters`：字符串或 `{"name": "...", "params": {...}}` 数组，映射为 FilmCraft `effects.apply` + `effects.setParam`。
  - `loudness`：应用于带音轨视频片段（`volume > 0`），支持目标响度数字（LUFS `-60..0`）或 `{"target_lufs": -16.0, "type": "dialogue|music|sfx|ambience", "enabled": true}`，通过 FilmCraft `essentialSound.setType` + `essentialSound.autoMatch` 自动匹配响度。
  - `denoise` / `animations` / `background` 为兼容保留字段，不直接编译，验证时会明确输出警告提示改用 `denoise-audio` 或 EffectCraft `overlays[]`。

## 独立音频轨：`audio[]`

- **必填**：`path`（必须含有效音频流）、`start`、`duration`。
- **可选**：`source_in`、`speed`（`0.25..4.0`）、`volume`（`0..4.0`）、`track`（**v1/v2 默认值为 `"voice"`**）、`fade_in` / `fade_out`（秒，`fade_in + fade_out <= duration`，编译为 FilmCraft `constant_power` 入/出淡变转场）、`loudness`（LUFS 数字或 `essentialSound` 配置对象，按 `voice`/`bgm` 轨道默认推断 `dialogue`/`music`）、`denoise`（校验期提示使用离线 `denoise-audio`）、`beat_marks`（严格递增的源时钟秒数列表，按 `source_in`/`speed` 换算为程序时间轴黄色节拍标记 `#FFD54F`）。
- **动态音量关键帧 `keyframes[]`**：每项格式 `{"time": t_sec, "value": linear_gain}`，由 `runtime_core.validate_parameter_keys` 严格校验：非空数组、**必须从 `time == 0` 起步**、本地时间与源时间均严格递增、`value in [0, 10]`；构建时由线性增益转为分贝（dB）写入 FilmCraft 音量关键帧（如 `ducking` 生成的闪避曲线）。

## 字幕与图形标题：`captions[]`

- **必填**：`text`（1..4000 非空字符）、`start`、`duration`。
- **双模式 `kind`**：
  - `kind: "graphic"`（默认）：默认置于 `track: "captions"`，编译为 FilmCraft `graphics.newText` 矢量图形文字层，默认坐标 `(x=0.0, y=-0.7)`（或直接传像素 `position: [px, py]`），支持 `font` / `font_style` / `font_path`（静态 TTF/OTF）、`size`（`1..500`）、`color` / `stroke_color` / `background_color`（`#RRGGBB` 或 `#RRGGBBAA`）、`border`/`stroke`、`stroke_width`、`background`、`shadow`、`bold`、`italic`、`align`、`style.alpha` 与 `transform.alpha`。
  - `kind: "subtitle"`：默认置于 `track: "C1"`，创建独立 Subtitle 字幕轨并可在导出时烧录画面或随时间轴保留。
- **同句多色重点词：`highlights[]`**：
  - 配列元素：`{"start": int, "end": int, "color": "#RRGGBB"}`，遵循半开字符区间 `0 <= start < end <= len(text)`，构建前排序并拒绝任意两段重叠。
- **图形关键帧：`keyframes[]`**：支持 `position_x`/`position_y`/`uniform_scale`/`scale_y`/`rotation`/`alpha`，按时间分组合并通过 `graphics.set` 写入精确关键帧。

## 高级动效透明叠加层：`overlays[]`（别名 `motion_graphics[]`）

```json
{
  "name": "hero_title_fx",
  "track": "overlay",
  "start": 0.4,
  "duration": 3.0,
  "layers": [
    {
      "type": "text",
      "name": "title",
      "text": "开源双核渲染",
      "size": 76,
      "color": "#FFFFFF",
      "stroke_color": "#0F172A",
      "stroke_width": 4,
      "position": [960, 480],
      "keyframes": [
        {"path": "opacity", "keys": [{"time": 0, "value": 0}, {"time": 0.35, "value": 100}], "interpolation": "easeOut"}
      ]
    }
  ]
}
```

- 每项必须唯一命名 `name`（1..64 安全字符），指定 `start`、`duration`（`0.04..3600`s），可覆盖 `width`/`height`/`fps`（否则继承画布设置），可选顶层 `motion_blur`（开启合成运动模糊 `comp.setSwitch`），默认进入 `track: "overlay"`。
- 必须提供 `recipe`（推荐，配合可选 `params`，与 `layers`/`script` 互斥）、`layers`（支持 `solid`、`shape`、`footage`、`text` 与 `camera`；其中 `footage` 支持 `time`/`in`/`out`/`duration`/`anchor` 及 `time_remap`/`timeRemap` S 形曲线时间重映射，可见图元支持 `three_d` Z 轴景深坐标 `[x, y, z]` 与单层 `motion_blur`，`camera` 支持 `camera_type`/`preset`/`position`/`poi` 与 `transform/position`/`cameraOptions/zoom` 关键帧；保留 `keyframes`/`expressions`/`effects`/`highlights`/`text_preset`/`styles`/`masks`/`blend_mode`/`builtin_presets`）或 `script`（AE 风格 `.jsx` / JS 源码）。内置 10 个分辨率自适应配方 `title_card`、`lower_third`、`keyword_callout`、`beat_flash`、`cinematic_bars`、`progress_banner`、`chapter_opener`、`pip_card`、`split_screen_frame`、`wipe_transition` 详见 [动效配方与手法映射](recipes.md)。
- 构建时自动由 EffectCraft 无头渲染为 `ProRes 4444 RGBA (.mov, yuva444p12le)`，经完整 Alpha 与严格解码校验后置于 FilmCraft 对应视频轨。

## 嵌套合成组：`composites[]`

- 将一个或多个已存在轨道上的视频/叠层/图形片段打包为 FilmCraft 嵌套序列（`timeline.select` + `clip.nest`），再对外层嵌套片段统一施加整体变换、关键帧、混合模式、遮罩、调色与滤镜：
  - **必填**：`tracks`（非空已声明轨道名数组）。
  - **可选**：`name`、`start`、`duration`（若指定则按时间区间交集筛选选中片段；省略时选中这些轨道上的全部已入轨片段）、`transform`、`keyframes`、`motion_easing`、`blend_mode`、`mix_mode`、`mask`、`grading`、`effects`、`filters`。

## 轨道顺序与来源 Sidecar 约束

- **轨道类型与重叠校验**：同一 `track` 名称不能同时承载不同类型的项目（`video` / `text` / `audio` / `effect` / `filter`），且同一轨道内不可发生区间重叠。
- **`layer_order`**：若显式提供，必须不重复、无空白、且与工程中**实际出现的所有非空轨道集合完全一致**（包括视频、图形字幕、Subtitle 字幕轨与音频轨名称，若连带音轨 `A1` 会由 `clips[].volume > 0` 自动激活，也可先省略或显式列出所有活跃轨名）。
- **`.provenance.json` Sidecar**：若引用媒体同目录存在 `<文件名>.provenance.json`，则必须同时包含 `"license_verification": "agent_review_recorded"`、非空 `review_note` 与匹配的实时 `sha256` 摘要，否则 `build` 在建目录前即中止并报错。
