# edit-plan.json 格式

静态右上角字段：perspective:{"static_up_right":[0.5,-0.5]}，与keyframes互斥，仅[1,-1]或[0.5,-0.5]；准入范围见[能力主表](capabilities.md#perspective)。

本页只写字段、单位与校验规则。某个字段能用到什么子集、可与什么组合、验证到哪一层，统一查 [能力状态](capabilities.md)；子集外的组合按未准入处理。

CLI 校验未知字段、非法数值、缺失素材、越界截取、同轨重叠和主画面空洞。所有时间单位为秒，可带小数。校验时先把每段的起点与终点（start、start+duration）各自舍入到整数微秒，duration 改为两者之差，source_in 也舍入到微秒；写回的 `edit-plan.json` 即为舍入后的值，重复校验结果不变。同轨相邻片段直接写 `下一段 start = 上一段 start + duration` 即可首尾相接；舍入后仍重叠 1 微秒以上会被拒绝。相对素材路径从计划文件所在目录解析；路径只接受本地文件，不接受 URL。不添加未知字段，不猜资源 ID 绕过校验。

## 顶层

| 字段 | 说明 |
|---|---|
| `schema_version` | 固定为 `1` |
| `name` | 项目名称；可含中文，不能含路径字符或 Windows 保留名 |
| `mode` | `script`、`reference`、`hybrid` 之一 |
| `canvas` | `{ "width": 1080, "height": 1920, "fps": 30 }`，按用户要求确定 |
| `clips` | 必填，非空画面片段数组；视频、图片均支持 |
| `audio` | 可选，配音、BGM、音效数组 |
| `captions` | 可选，字幕或标题数组 |
| `notes` | 可选，取舍和依据的字符串数组 |
| `missing_assets` | 可选，素材缺口字符串数组；真实缺口写在这里，不藏进 notes |
| `effects` / `filters` | 可选，独立原生特效/滤镜轨道数组，见 [原生效果](native-effects.md#独立特效滤镜轨道) 与下文 [独立效果轨](#独立效果轨) |
| `allow_vip_effects` | 可选布尔值，默认 true（用户已声明会员并授权），允许会员效果及抠像参数。当前任务只用免费效果时显式设为 false。此字段不开通权限 |
| `jianying_tasks` | 兼容保留的未执行状态字段，只接受既有 `smart_cutout`/`tracking` 记录，不提交算法；普通生产不添加。含该记录的交付标记 partial |
| `layer_order` | 可选，全部实际轨道名的数组，从背景到前景排序，每轨恰好一次；可让文字位于视频面板后。主视频 `video` 必须是最底部视觉轨；音频轨不参与视觉遮挡 |
| `composites` | 可选，把连续的视频/文字/音频轨组成可编辑子草稿，见 [composites](#composites复合) |

输出时长取主视频轨道 `video` 的结束位置。必须有该轨道，从 0 开始连续排列；黑场用明确的黑色素材。叠加轨道、音频和字幕不能延伸到主画面结束之后。不同轨道可重叠，同轨不可重叠。

## clips：画面

必填 `path`、`start`、`duration`。`start` 是时间轴起点，`duration` 是时间轴时长；`source_in` 默认 0，`speed` 默认 1，源截取长度 = duration × speed。图片的 source_in 固定为 0、speed 固定为 1，duration 自由设置。speed 只接受正数。

音频轨道的片段同样受这条源截取规则约束。源截取长度不能越过素材的可测时长，而同一文件的帧数换算值、ffprobe 读数与后端 mediainfo 会相差几十微秒（实测 WAV：帧数 267.9640816、ffprobe 267.964082、校验上界 267.964000），写成帧数换算值即被拒绝为 `source range exceeds measured media duration`。取几者中最小者再留至少 1 微秒余量；用整段素材时宁短勿长，末尾少几毫秒不影响听感。

可选字段：

- `track`：默认 `video`；如 `overlay` 为额外画面轨道。不同轨道按第一次出现顺序从背景到前景创建。
- `volume`：默认 0，原声的线性增益，1 表示保持原增益。需要同步声时显式写 1；不主动消除有效原声。
- `fit`：默认 `contain`，完整显示；`cover` 填满画布，可能裁掉主体，需视觉确认。
- `transform`：ClipSettings 参数 `scale_x`、`scale_y`、`transform_x`、`transform_y`、`rotation`、`alpha`、`flip_horizontal`、`flip_vertical`。scale 是 fit 后的相对缩放，必须大于 0；位移单位为半个画布宽/高，字幕常用 y=-0.8；alpha 为 0–1。
- `transition`：`{"name":"叠化","duration":0.25}`。转场位于前一片段末尾，必须有同轨紧接的下一片段；只填 `capabilities` 列出的精确枚举名；时长不超过相邻片段较短者的一半。真实效果与资源仍需剪映内验证。
- `reason`：选择素材的依据，例如画面内容或参考镜头对应关系。
- `filters`、`effects`、`animations`、`mix_mode`、`chroma`、`background`：原生参数，格式与枚举查询见 [原生效果](native-effects.md)。不接受任意资源 ID。旧字符串mix_mode与未选模式的chroma仍属 [参数兼容未准入](capabilities.md#param-only)；混合仅按 [安装内混合子集](capabilities.md#installed-mix) 使用对象，色键按 [安装内Chroma2子集](capabilities.md#installed-chroma2) 显式选择rendering。
- `keyframes`、`motion_easing`：见 [画面关键帧与缓动](#画面关键帧与缓动)。
- `speed_curve`：见 [曲线变速](#曲线变速)。
- `mask`：见 [蒙版](#蒙版)。
- `grading`：见 [调色](#调色)。
- `matting_cache`：见 [已有人物遮罩](#已有人物遮罩)。

### 画面关键帧与缓动

`keyframes`：`[{"property":"uniform_scale","time":0,"value":1},{"property":"uniform_scale","time":2,"value":1.08}]`。

- time 相对片段起点，单位秒。视频/图片运动在草稿内写为源时间 `source_in + time * speed`，无需预换算；局部时间与映射后的微秒时间都不得重复。
- 属性限 `uniform_scale`、`scale_x`、`scale_y`、`position_x`、`position_y`、`rotation`、`alpha`；不能混用 uniform_scale 与 scale_x/scale_y。
- 缩放关键帧是相对 fit 基准的绝对目标值，不再乘以静态 `transform.scale_x/scale_y`。纯色细线已有宽度 w、高度 h 时，随组缩放 s 分别写 `scale_x=w*s`、`scale_y=h*s`；只写 `uniform_scale=s` 会丢失细线比例。共同中心的位移与旋转另行计算，见 [同源图层与共同中心](technique-combinations.md#同源图层与共同中心)。这是当前后台写入语义，渲染仍按实际结果确认。
- 蒙版中心、效果旋转、音量和调色数值不能混入画面 keyframes。
- 省略 `motion_easing` 时为线性。`motion_easing` 只接受 `"smoothstep"`，由生成器写入可编辑原生曲线；可用的对象、点数与组合见 [原生缓动](capabilities.md#easing)。

### 曲线变速

`speed_curve`：`[{"x":0,"y":0.5},{"x":1,"y":2}]`。

- x 是选定源区间的归一化进度（不是时间轴进度），首 0 末 1、严格递增；y 为倍速；两或三个点。
- duration 仍是时间轴时长。生成器由曲线推导平均 speed 与源长度，通常省略 speed；显式 speed 与推导值冲突即拒绝。
- 片段上的蒙版键 time 是目标局部时刻，生成器按真实曲线映射写源坐标，不乘平均 speed。
- 取值范围、可用素材、窗口与组的组合见 [曲线变速](capabilities.md#speed-curve)。volume=0 不能让含声音的视频自动准入；生成器不自动剥除原声或补帧。

### 蒙版

`mask` 每片段最多一个，需 `experimental: true`；不能与同片段 `grading.local_circle` 共存。其他形状校验拒绝。

```json
{"mask":{"name":"矩形","experimental":true,"center_x":-150,"size":0.5,"rect_width":0.28,"feather":0,
  "keyframes":[{"property":"center_x","time":0,"value":-150},{"property":"center_x","time":4.8,"value":150},
               {"property":"rotation","time":0,"value":0},{"property":"rotation","time":4.8,"value":30}]}}
```

- 静态参数：size、center_x/center_y、rotation、invert。center 为原素材像素偏移（正 X 向右、正 Y 向上）；size 为原素材高度比例，>0 至 4（圆形直径 = size × 原素材高度）；invert 为布尔值。rect_width（素材宽比例）与 round_corner（0–100）只用于矩形。裁切蒙版默认模式仅准入feather=0；显式矩形rendering="red_coverage_v1"可用0–20静态值及同范围feather关键帧，范围见下方链接。调色 local_circle 的羽化是另一契约。
- `mask.keyframes`：属性 center_x、center_y、rotation、width、height、feather（圆形无 feather，矩形默认feather关键帧只可保持0；red_coverage_v1例外见下）。width/height 是原素材像素尺寸，不是画布像素或 transform 缩放值；后台按原素材宽/高与 100 归一化。无关键帧的尺寸沿用 size/rect_width。示例尺寸 1075.2×1080 对应 3840×2160 素材，不是其他素材的默认值。
- 时间：每属性首点 0，严格递增，微秒舍入后不重复，终点小于片段 duration（落在可见帧）。计划 time 始终是片段局部秒数；生成器写入 `(source_in + time * speed) * 1000000` 并取整，不要在计划里自行加源入点或时间轴 start。只支持线性插值；值范围沿用静态中心/旋转范围。
- 单属性只写一个 0 秒点可保留静态值。蒙版关键帧不能混入片段画面 keyframes。
- 各形状与羽化的准入见 [矩形](capabilities.md#mask-rect) 与 [圆形与椭圆](capabilities.md#mask-circle)。

### 调色

`grading` 只接受以下键，至少含一种 HSL 或曲线：

```json
{"grading":{"red_hsl":{"hue":-35,"saturation":15,"lightness":-10},
 "luma_curve":[[0,0],[0.5,0.4],[1,1]],
 "rgb_curves":{"blue":[[0,0],[0.4,0.65],[1,1]]},
 "local_circle":{"width":1075.2,"height":1080,"feather":20}}}
```

- `red_hsl`：`{hue,saturation,lightness}` 各 −100 至 100，省略分量为 0，仅红色通道。
- `blue_hsl`、`green_hsl`：单一分量对象，如 `{"saturation":-60}`、`{"hue":-30}`；允许的分量与取值见 [调色](capabilities.md#grading)。
- `luma_curve`：三个归一化锚点 `[[0,0],[x,y],[1,1]]`，中点 0<x<1、0≤y≤1；控制柄按相邻点三分之一构建折线，不是任意贝塞尔接口。
- `rgb_curves`：非空对象，只接受 red/green/blue 的任意非空子集，各通道同三点规则。未请求的 RGB 通道与未请求的 L 曲线写入恒等两点；同一 color_curves 材料保存 L/R/G/B，select_channel=0。
- `local_circle`：`{width,height,feather}`，静态、居中、无旋转；尺寸为原素材像素，羽化 0–100；必须与 HSL 或曲线配合，不能与视频 mask 共存。写了它，同片段曲线/HSL 的**作用范围就限于这个区域，区域外保持原样**；不写则这些调节作用于全画面。羽化不是「边缘略软」：实测羽化 60 会把调节摊到近乎整帧且中心强度只剩约 35%，常用值是 0。区域几何随源尺寸归一化（1280×720 画布填 560×440 与 640×360 画布填 280×220 同形），`transform_x/transform_y` 会带着区域一起平移，旋转未在区域轮廓上生效。实测见 [调色](capabilities.md#grading)。
- 所有坐标必须是有限数值；不接受布尔、额外控制柄或动画字段。资源缓存缺失直接拒绝。

### 已有人物遮罩

`matting_cache`：`{"manifest":"外部准入.json","sha256":"该文件的SHA256"}`。manifest 相对计划目录解析，由 `editor.py admit-matting` 生成；只绑定已审阅的已有原生自定义遮罩，不产生新遮罩。画布、运动、配组限制与 manifest 字段见 [人物遮罩绑定](capabilities.md#matting)。

## composites：复合

```json
{"name":"局部组件","tracks":["panel","title","music"],"start":1,"duration":2,
 "keyframes":[{"property":"rotation","time":0,"value":0},
              {"property":"rotation","time":1.966667,"value":12}]}
```

- `tracks`：在实际 layer_order 中连续，至少两条且第一条为视频，可含文字/音频，互不重用。未指定 layer_order 时按默认轨序。
- `start`/`duration`：省略即覆盖完整主时间轴；局部区间需成对指定，所选轨全部片段须完整位于区间内，否则先切段。
- `keyframes`：组局部时间，首点 0，末点早于组 duration。属性为 rotation，或恰好两点的单属性 position_x/position_y（−1 到 1，垂直正值向上、按半画布高度换算）/uniform_scale（0.6 到 1.2，原生 ScaleX/ScaleY 相等，围绕画布中心）。属性之间不混用，也不与 perspective 混用。
- `motion_easing`：可选 `"smoothstep"`，只用于受限两点水平组。
- 生成：内层 target 减去组 start，媒体源范围与关键帧保留；外层 source 从 0 开始，target 使用组区间。pending 算法轨须先完成；发布校验嵌入数据与 sidecar 的范围和路径。
- 组内成员、可组合的运动与外部组合见 [局部复合](capabilities.md#composite)。

### 外层角点透视

`composites[].perspective.keyframes` 保留`up_left`；另可按能力主表使用`up_right`、`down_left`或`down_right`受限两点子集。每点 `values` 为归一化 `[x,y]`：原始左上角为 `[-1,-1]`，X 向右、Y 向下，原up_left各项限 −10 到 10；新三种仅原角点与半幅内收两个预设坐标，不能沿用此任意范围。这与画面位移或蒙版像素单位不同，不能用面板像素读数直接填写。首点为 0，按组局部秒数严格递增且小于组 duration，线性插值；外层 source_in=0、speed=1。

```json
{"name":"视觉合成","tracks":["video","panel"],"perspective":{"keyframes":[
  {"property":"up_left","time":0,"values":[-0.6840579710144927,-0.7139175257731959]},
  {"property":"up_left","time":0.833333,"values":[-1,-1]}
]}}
```

这组数值只展示字段结构，不是默认构图。状态见 [左上角透视](capabilities.md#perspective)。

## 独立效果轨

顶层 `effects`/`filters` 的通用字段见 [原生效果](native-effects.md#独立特效滤镜轨道)。独立模糊可加参数动画：

```json
{"name":"模糊","kind":"scene","start":1,"duration":3,"params":{"effects_adjust_blur":0},
 "keyframes":[{"time":0,"value":0},{"time":1.5,"value":45},{"time":2.966667,"value":0}]}
```

value 为 0–100。时间相对效果片段，不加视频源入点或时间轴 start；首点 0，严格递增且小于效果 duration。准入范围见 [独立模糊动画](capabilities.md#blur-anim)。

## audio：音频

必填 `path`、`start`、`duration`。默认 `track="voice"`、`source_in=0`、`speed=1`、`volume=1`。可选 `fade_in`、`fade_out`，单位秒，默认 0，两者之和不能超过片段时长。BGM 通常单独放 `music` 轨，增益按试听调整，不把固定数值当成自动混音。

音频轨只用不含视频流的音频文件。配音来自视频时，先在授权任务目录中用 FFmpeg 提取音频副本，保留源文件。选择性保留招式名、碰撞等原声时，同样用 audio 片段记录来源与源范围，独立于 music 轨，并静音视频内的同一路声音，避免重复播放。

- `keyframes`：`[{"time":0,"value":0.5},{"time":1,"value":0.12}]`。value 与 volume 相同，是线性增益 0–10，不是 dB；换算用 `10 ** (dB / 20)`，插值发生在增益上。首点 0，时间严格递增且小于片段 duration；局部时间与 `source_in + time * speed` 换算后的微秒都必须唯一。支持固定 speed。BGM 局部避让也可拆连续源段分别设 volume。见 [音量关键帧](capabilities.md#audio-keyframes)。
- `denoise`：`{"model_path":"当前安装的原生模型绝对路径"}`。Agent 定位当前本机模型并核实字节身份，用户不需填写技术路径；省略不启用。见 [实时降噪](capabilities.md#denoise)。
- `loudness`：`{"target_lufs":-23}`；省略不启用。见 [受限响度](capabilities.md#loudness)。

自动闪避的包络不在 build 内生成：用 `scripts/ducking.py` 从分离后的人声 stem（或显式语音区间）算出音量关键帧写回目标轨的 `keyframes` 后再 build；人声 stem 可用 `scripts/separate.py` 离线拆出（要自备 htdemucs ONNX 权重与 onnxruntime，模型不随 Skill 分发），也可以来自外部工具或用户手工分离。具体包络和声音事件按试听核实。见 [离线自动闪避](capabilities.md#ducking) 与 [离线人声分离](capabilities.md#separation)。

## captions：字幕和标题

已有本工具的转写结果时，可用 `scripts/captions.py 计划.json --transcript 转写目录/transcript.json --index 0 --out 新计划.json` 按所选画面片段的截取与线性速度导入完整语句；选独立音频片段加 `--group audio`。保留原文和已有字幕，生成新计划，不覆盖输入。多片段可同次传 `--index 0 2 3`，按各自截取和固定速度生成一份新计划；选段范围与批量拒绝见 [多片段字幕映射](capabilities.md#多片段字幕映射)。来源检查、半句拒绝及支持范围见 [转写结果导入剪辑计划](capabilities.md#转写结果导入剪辑计划)。

用户提供或人工校正的纯文本 SRT 可用 `scripts/captions.py 计划.json --srt 字幕.srt --timebase source --index 0 --out 新计划.json`；源媒体时间选 `source`，最终计划时间选 `timeline`，不可省略或猜测。只导入所选片段区间内的完整字幕，保留多行文字；编码、来源标记与其它边界见 [外部 SRT 导入](capabilities.md#外部或人工校正-srt-导入)。统一 CLI/MCP 操作名为 `captions`。

必填 `text`、`start`、`duration`。默认 `track="captions"`、`size=8`、`color="#FFFFFF"`、`y=-0.8`、`x=0`、`bold=false`、`border=true`。标题可放 `titles` 轨，指定 y=0 或其他位置。size 是剪映字体单位而非像素，不能原样套参考视频的像素字号。字幕默认字体、居中、自动换行；描边为基础黑色描边；不偷偷下载或替换字体。不填 `font` 时，生成器仍会写入剪映自己对该版本使用的系统默认中文字体路径（本机 11.6 实测为 `<安装目录>\Resources\Font\SystemFont\zh-hans.ttf`，与剪映保存草稿时自己写入的值一致）——没有这个路径时剪映界面把文字画成占位字形；本机找不到安装目录就**不写**并给出一条警告，不猜路径。

生成的 `字幕.srt` 只包含 captions 轨道，其他文字轨道保留在工程中。SRT 不能表达字体、颜色、位置或动画。逐段文字与时间可以来自 [离线转写](capabilities.md#transcription)（`scripts/transcribe.py`，要自备 faster-whisper 模型）：它的 `字幕.srt` 与本节的 `text`/`start`/`duration` 同形，可直接落进计划；但默认时间只有段级；显式模型词级入口见 [模型词级时间](capabilities.md#模型词级时间与独立文字段)，不得人工改写模型原文或编造更细的时间。没有转写结果、也没有用户文稿时，字幕时间标为暂定。

- `animations`：原生 intro/outro/loop，名称从对应文字枚举查询，字段见 [原生效果](native-effects.md#captions原生字幕动画)。loop 不填 duration，自动放在入场结束与出场开始之间。
- `transform`、`keyframes`：缩放、旋转、水平/垂直位移沿用画面格式；文字关键帧用局部时间，不加 start。静态位置继续用 x/y，不在 transform 里重复写 transform_x/transform_y。文字alpha仅按 [原生GlobalAlpha子集](capabilities.md#text-global-alpha) 准入；计划仍写property=alpha，文字专用映射由引擎完成。
- `motion_easing`：可选 `"smoothstep"`，省略为线性。见 [原生缓动](capabilities.md#easing)。
- `style`：只含 alpha（0–1）、italic/vertical/auto_wrapping（布尔）、letter_spacing/line_spacing（−100 到 100 的整数）、border_color（#RRGGBB）、border_width（0–100）与 shadow 对象。shadow 可写 color（#RRGGBB）、alpha（0–1）、diffuse/distance（0–100）、angle（−180 到 180）。border=false 时描边设置不显示。这些参数保留为原生文字，不生成字幕图片。
- `font`：`capabilities --kind fonts` 返回的已准入枚举名，目前仅 `励字志向黑简_特粗`；缺省保持默认字体。不接收资源 ID、任意文件路径或系统字体名。见 [缓存字体](capabilities.md#font)。
- `font_path`：单个静态 `.ttf` / `.otf` 的本地路径，相对路径按计划文件目录解析，也可用绝对路径。与 `font` 互斥；build 会解析字体表、拒绝 TTC/可变字体/损坏文件，按 SHA256 复制到草稿 `Resources/local-fonts/`，并把顶层 `font_path` 与富文本 `styles[0].font.path` 同时绑定到草稿内副本。发布或离线归档时重绑定到新草稿目录。首版仅支持静态横排 BMP 文字，不允许动画、画面运动、重点词、透明度、弯曲、发光、竖排或原生复合。见 [本地静态字体](capabilities.md#local-font)。
- `text_curve`：`{"angle":180}`，angle 为有限数值 0–360 度，0 为启用弯曲后的直线对照；省略保持普通文字。兼容旧 experimental 布尔字段。见 [静态弯曲](capabilities.md#text-curve)。
- `glow`：`{"name":"放射发光","color":"#4080FF","strength":0.2,"range":0.2}`。strength/range 各 0.01–1，颜色为不透明 RGB，方向固定 0.5/0.5。文字条件与运动限制见 [放射发光](capabilities.md#glow)。

## 最小计划示例

```json
{
  "schema_version": 1,
  "name": "脚本样片-v1",
  "mode": "script",
  "canvas": {"width": 1080, "height": 1920, "fps": 30},
  "clips": [
    {"path": "素材/镜头1.mp4", "start": 0, "duration": 3, "source_in": 1, "fit": "cover", "reason": "开场细节"},
    {"path": "素材/镜头2.mp4", "start": 3, "duration": 4, "source_in": 0, "volume": 1}
  ],
  "audio": [],
  "captions": [
    {"text": "开场文案", "start": 0.2, "duration": 2.5},
    {"text": "主体文案", "start": 3.2, "duration": 3.5}
  ],
  "notes": ["字幕时间按脚本暂定，待声音同步"],
  "missing_assets": []
}
```

计划中的素材应实际存在。效果保留剪映原生可编辑，不用外部预渲染替代。动态矩形蒙版、视频复合与已测左上角透视都直接后台生成；日常交付如实保留“预览未验证”，单独兼容测试可按 [高级原生 UI 操作](native-ui-advanced.md) 局部核对。暂存能力不写入计划或 `jianying_tasks`；必要且无法替代时说明剪辑缺口，尚未执行的必要操作在交付中明确列出。

蓝绿HSL组合例外：`grading:{"blue_hsl":{"saturation":-60},"green_hsl":{"saturation":-60}}`，仅此精确配对；输入与证据边界查[调色能力](capabilities.md#grading)。

blue_hsl.saturation视频例外的输入字段为无音轨640×360/30fps源，独立单段source_in=0.5/start0/duration2/speed1/volume0，匹配画布、contain无其他变换或轨道；仅−60/0/60，准入边界见[调色能力](capabilities.md#grading)。

green_hsl.saturation另准入同蓝色视频的无音轨640×360/30fps单段限制，值仅−60/0/60、source_in0.5/start0/duration2/speed1/volume0，匹配画布contain无其他轨道/运动效果；组合与其他参数拒绝，见[调色](capabilities.md#grading)。


## 倒放素材输入

先用统一 `reverse` 操作生成新媒体，再把 `reverse-report.json` 的 `output_media` 填入普通clips[].path，source_in=0、duration采用报告时长；需声音时显式volume=1。没有reverse或负speed计划字段，其他轨道与字幕/效果不会自动倒排。限制与来源报告见 [离线短片段倒放](capabilities.md#离线短片段倒放)。保留新素材目录和原片。


## 模型词级字幕导入

已有同次transcribe --word-timestamps结果时，用captions的 `--level word` 导入，每词为普通caption文字片段，可与多索引固定速度映射共用；不是行内高亮或逐字拆分。源SHA256、原词文本、区间和拒绝边界见 [模型词级时间](capabilities.md#模型词级时间与独立文字段)。单词过短不自动拉长或借用相邻时间，后续按实际阅读重新设计。


## 离线轨迹应用到文字

已有成功CSRT报告时，用tracking-apply --caption-index选择现有caption；与图片target-index互斥。目标与源片段同start/duration，显式x=0、y=0、style.auto_wrapping=false，水平单行默认字体且无已有运动/transform/动画/曲线/glow。offset-x/y为画布像素，正右/正上；源身份、覆盖范围、固定速度与拒绝规则见 [离线单目标跟踪](capabilities.md#offline-tracking)。输出仍是普通caption与可编辑position_x/y局部关键帧，无新增计划字段，不烘焙文字。仅检查锚点在画内，实际字形、遮挡与阅读需单独检查。


## 离线降噪音频素材

`denoise-audio`在新目录生成dry.wav、denoised.wav及身份/样本/延迟报告；作为普通audio素材使用output_media，source_in=0，视频原声替换时明确将视频volume=0并保持源入点一致。不添加audio[].denoise，避免与原生实时降噪重复。PCM16转换、时间量化、输入限制及听感边界见 [离线FFT降噪](capabilities.md#offline-denoise)。输出目录是后续草稿的素材依赖，原片与dry对照保留。


## 矩形羽化渲染变体

示例：`{"mask":{"name":"矩形","experimental":true,"rendering":"red_coverage_v1","size":0.6,"rect_width":0.3375,"feather":20}}`。参数仍是原生可修改蒙版；build生成草稿内已核资源副本，publication随草稿复制/重定位。明确限制、运行时/源缓存要求及UI边界以 [能力主表](capabilities.md#mask-red-coverage) 为准；不得给默认模式或其他形状直接添加非零羽化。迁移计划后在目标环境重建，保留派生资源目录。

## 安装内色键渲染

示例：`{"chroma":{"rendering":"installed_chroma2_v1","color":"#00FF00FF","intensity":35}}`。shadow/edge_smooth/spill默认0；不接受path、resource_id或任意资源注入。正式build写入已核安装资源路径，参数保持原生可编辑；明确输入/组合/版本与发布迁移依赖见 [能力主表](capabilities.md#installed-chroma2)。

## 安装内混合模式

示例：`{"mix_mode":{"name":"滤色","rendering":"installed_mix_v1"}}`；name也可为`正片叠底`、`叠加`、`柔光`、`强光`、`线性加深`、`颜色加深`、`颜色减淡`、`变亮`或`变暗`。后六种只接受PNG叠层，前四种保留已准入视频子集。不接受path、resource_id、value或任意资源注入，旧字符串仍拒绝。轨道/素材/组合/运行版本与发布迁移依赖见 [能力主表](capabilities.md#installed-mix)。

## 同句重点词颜色

`captions[].highlights`示例：`[{"start":2,"end":5,"color":"#FF0000"}]`，按原始text从0起、end不包含；保留同一文字材料。必须`style={"auto_wrapping":false}`，只接受start/end/color，不接时间或任意native style。输入/字体/组合/发布迁移依赖以 [能力主表](capabilities.md#text-highlights) 为准；它不替代词级转写或逐词时间。

## 文字透明度关键帧

默认字体白字、单行、bold=false、`style={"auto_wrapping":false}`：`keyframes=[{"property":"alpha","time":0,"value":0},{"property":"alpha","time":1.5,"value":1}]`。两点纯alpha保留原支持；另可用四点0→1→1→0完成淡入—保持—淡出。首time=0，键按微秒严格递增，末点严格早于duration；time不加字幕start。范围/运行依赖/拒绝组合统一见 [能力主表](capabilities.md#text-global-alpha)。不直接写原生property_type或手改JSON。

## 多色字幕淡入淡出

同一字幕可写`highlights=[{"start":2,"end":5,"color":"#FF0000"}]`，`style={"auto_wrapping":false}`，并写两点或受限四点`keyframes`的property=alpha；或移除keyframes改用`style={"auto_wrapping":false,"alpha":0.5}`。不能混用静态/动态alpha，基字白色/bold=false，其他子集与拒绝规则见 [多色透明度组合](capabilities.md#text-color-fade)。范围和颜色是静态的，不是逐词卡拉OK。


### 字幕淡入—保持—淡出

四点仅准入以下0→1→1→0值序列，时间可按阅读需要调整，必须保留正的淡入、保持与淡出区间；最后键后保持透明。可同句增加已准入highlights；其他条件见 [文字整体透明度](capabilities.md#text-global-alpha)。

```json
{"text":"出现后清楚停留，再淡出","start":0.5,"duration":3,"style":{"auto_wrapping":false},
 "keyframes":[{"property":"alpha","time":0,"value":0},
              {"property":"alpha","time":0.5,"value":1},
              {"property":"alpha","time":1.5,"value":1},
              {"property":"alpha","time":2.5,"value":0}]}
```


### 受限右上/左下/右下角点运动

示例为两个完整两秒轨道组成的640×360/30fps PNG与默认文字组：

```json
{"name":"透视组","tracks":["video","title"],"perspective":{"keyframes":[
 {"property":"up_right","time":0,"values":[1,-1]},
 {"property":"up_right","time":1.5,"values":[0.5,-0.5]}]}}
```

左下改down_left及[-1,1]→[-0.5,0.5]；右下改down_right及[1,1]→[0.5,0.5]。可倒置两个位置形成复位，或保持同值。一次只准一种新角点，不能同组混入up_left或组运动；其他输入、运行依赖与拒绝规则见 [角点透视主表](capabilities.md#perspective)。


## 节拍候选映射

先用beats明确输入源选区，beat-apply显式指定每N个候选，生成新计划；沿用clips.start/duration/source_in和audio，不新增beats/auto_beat字段。音乐源时间由样本时钟换算，精确量化帧后再写微秒。新片长止于所选最后边界，原计划和媒体保留；准入与拒绝边界见 [离线节拍与切镜](capabilities.md#beat-editing)。


## 两行重点词着色

静态默认字体白基色、非粗体、style仅auto_wrapping=false。示例text="甲乙丙丁\n戊己庚辛"的LF下标为4；highlights=[{"start":0,"end":4,"color":"#FF0000"},{"start":5,"end":9,"color":"#00FF00"}]分别覆盖两行。下标包含LF，范围不能包含LF；两行各自的局部词也可分别指定。无静态/动态透明度、其他运动或效果组合；完整边界见 [重点词着色](capabilities.md#text-highlights)。


## 绿色HSL明度

`grading={"green_hsl":{"lightness":30}}`，只含lightness且仅−30/0/30。沿绿色HSL单静图640×360/30fps、start0/duration2/contain/speed1及无其他轨道/效果/变换限制，不用于绿色饱和度的视频例外。原生hsl_color_type=4，hue/saturation固定0；缓存与运行身份、发布/迁移边界见 [调色主表](capabilities.md#grading)。


## 其他五色HSL

示例`grading={"cyan_hsl":{"saturation":-60}}`；字段可选orange_hsl/yellow_hsl/cyan_hsl/purple_hsl/magenta_hsl之一，一次只含saturation（仅−60/0/+60）或lightness/hue（各仅−30/0/+30），例如`grading={"cyan_hsl":{"lightness":30}}`。仅独立640×360/30fps单PNG两秒静态计划，无其他调色、效果或轨道，另允许同一通道同时含saturation和lightness，分别保持上述离散值，例如`grading={"cyan_hsl":{"saturation":-60,"lightness":30}}`。hue仍单独，不与任一其他参数组合；不接受三参数、关键帧或视频。原生通道与边界见 [调色主表](capabilities.md#grading)。

色相示例：`grading={"orange_hsl":{"hue":30}}`。数值是原生HSL参数，不作为角度单位；每次一个通道的其余静图限制保持，色相独立使用。


## 逐词高亮字幕生成

先提供真实的词时间，再调用captions的`--karaoke-mode active|cumulative`。生成结果仍是普通captions数组：例如同组`甲乙`在第一个时间状态的highlights=[{start:0,end:1,color:#FFD54F}]，第二个状态active为[1,2]，cumulative为[0,2]；每个状态text都为完整`甲乙`，style.auto_wrapping=false。内部停顿的active状态无highlights，cumulative保持前一个范围。字段使用标准JSON字符串/键，不能把本说明伪代码当成JSON直接写入。

无需新增计划schema或原生参数。时间按完整输入单元映射，不按字数平摊，现有字幕也不会被自动删除；轨道冲突先显式整理输入计划。输入、CLI选项、编辑和时间可信度边界见 [动态逐词高亮](capabilities.md#karaoke)。
