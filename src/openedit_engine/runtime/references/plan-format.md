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

- **必填**：`path`（支持 `.mp4/.mov/.mkv/.avi/.webm/.png/.jpg` 等媒体）、`start`（`0..86400` 秒）、`duration`（`>0..86400` 秒）。`start` 与 `duration` 均自动量化到整微秒边界。
- **可选核心属性**：
  - `source_in`（默认 `0.0`）、`speed`（默认 `1.0`，允许范围 `0.05..32.0`）：视频要求 `source_in + duration * speed <= source_duration + 0.05s`。
  - `reverse`（布尔，倒放）、`volume`（`0..4.0`；自带音轨默认 `1.0`，纯图或静音默认 `0.0`，`>0` 时在 FilmCraft 联动放置到 `A1` 音轨）。
  - `track`（默认 `"video"`）：同一轨道名称不能混用不同媒体类型，同一轨道内任意两个片段不能时间重叠。
  - `fit`：`contain`（默认）/ `cover` / `fill`。
- **空间变换与透明度：`transform`**：
  - 支持两种坐标系统：直接画布像素 `position: [px_x, px_y]`，或归一化坐标 `transform_x` / `transform_y`（以画布中心 `(0, 0)` 为原点，按 `x_px = w/2 + vx*(w/2)`、`y_px = h/2 - vy*(h/2)` 映射，推荐范围 `[-1, 1]`，引擎校验范围 `[-10000, 10000]`）。
  - 支持 `scale_x` / `scale_y`（`>0`，若两者不等自动关闭 `uniform_scale` 并独立设置 `scale_width`）、`rotation`（度）、`alpha`（`0..1`）、`flip_horizontal` / `flip_vertical`。
- **动态关键帧：`keyframes[]` 与 `motion_easing`**：
  - 每项包含 `property`、`time`（相对片段起点的本地秒数，`0..duration`）、`value`、可选 `interpolation`（`linear`、`smoothstep`、`easeIn`、`easeOut`、`bezier`、`hold`、`autoBezier`、`continuousBezier`）。
  - 支持属性：`position_x`、`position_y`、`uniform_scale`、`scale_x`、`scale_y`、`rotation`、`alpha`。同时间点的 `position_x` / `position_y` 自动合并写入 FilmCraft 二维 `motion/position` 关键帧。同一属性与微秒时刻禁止重复。
- **混合模式与转场**：
  - `blend_mode`（字符串，如 `screen`、`multiply`、`overlay` 等）或 `mix_mode`（对象或字符串，支持 `opacity` 设定不透明度）。
  - `transition`：字符串名称（如 `"Cross Dissolve"`）或对象 `{"name": "Cross Dissolve", "duration": 0.4, "frames": 12, "edge": "in"|"out"}`。引擎在所有视频片段全部入轨后统一挂接边界转场，避免首片段无前邻边界失败。
- **调色与特效：`grading` / `effects`**：
  - `grading`：支持 Lumetri 字段 `exposure`、`contrast`、`saturation`、`creative_sat`、`highlights`、`shadows`、`whites`、`blacks`、`temperature`、`tint`、`vibrance`、`vignette`（`[-200, 300]`）、`luma_curve`（至少两个 `x` 严格递增 `[x, y]`, `x,y in [0,1]`）、`rgb_curves`（`red`/`green`/`blue`）、`lut` / `look`。
  - `effects`：字符串或 `{"name": "...", "params": {...}}` 数组，映射为 FilmCraft `effects.apply` + `effects.setParam`。

## 独立音频轨：`audio[]`

- **必填**：`path`（必须含有效音频流）、`start`、`duration`。
- **可选**：`source_in`、`speed`（`0.25..4.0`）、`volume`（`0..4.0`）、`track`（**v1/v2 默认值为 `"voice"`**）、`fade_in`、`fade_out`、`loudness`、`denoise`、`beat_marks`。
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

- 每项必须唯一命名 `name`（1..64 安全字符），指定 `start`、`duration`（`0.04..3600`s），可覆盖 `width`/`height`/`fps`（否则继承画布设置），默认进入 `track: "overlay"`。
- 必须提供 `layers`（支持 `solid`、`shape`、`footage`、`text`，含 `keyframes`/`expressions`/`effects`）或 `script`（AE 风格 `.jsx` / JS 源码）。
- 构建时自动由 EffectCraft 无头渲染为 `ProRes 4444 RGBA (.mov, yuva444p12le)`，经完整 Alpha 与严格解码校验后置于 FilmCraft 对应视频轨。

## 轨道顺序与来源 Sidecar 约束

- **轨道类型与重叠校验**：同一 `track` 名称不能同时承载不同类型的项目（`video` / `text` / `audio` / `effect` / `filter`），且同一轨道内不可发生区间重叠。
- **`layer_order`**：若显式提供，必须不重复、无空白、且与工程中**实际出现的所有非空轨道集合完全一致**（包括视频、图形字幕、Subtitle 字幕轨与音频轨名称，若连带音轨 `A1` 会由 `clips[].volume > 0` 自动激活，也可先省略或显式列出所有活跃轨名）。
- **`.provenance.json` Sidecar**：若引用媒体同目录存在 `<文件名>.provenance.json`，则必须同时包含 `"license_verification": "agent_review_recorded"`、非空 `review_note` 与匹配的实时 `sha256` 摘要，否则 `build` 在建目录前即中止并报错。
