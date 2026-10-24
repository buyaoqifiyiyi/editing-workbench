# 剪映原生效果

## 用户要求与实现边界

用户希望全程通过剪映实现效果，能在剪映里自行调整。因此本版本只引用原素材并写入剪映原生效果、滤镜、动画、混合、色度抠像、背景和蒙版参数，不运行 FFmpeg/OpenCV/AI 模型预渲染效果。素材分析仍可抽帧和探测。离线倒放属于显式时间逆序素材处理例外：会重新编码新媒体且保留原片，不是烘焙空间效果或原生reverse开关。

补帧（含智能补帧、光流插帧及用于平滑慢放的帧融合）、超分/超清及画质修复等技术性画质增强由用户自行完成，不属于本 Skill 的执行、研究接入或剪辑待办范围。普通变速和倒放属于剪辑；用于视觉表达的调色、滤镜与特效仍按当前接口和验证状态选用，不能借资源枚举将画质增强重新纳入任务。

后台生成使用固定后端 `pyJianYingDraft==0.3.0`，再按本机 11.5 实测结构扩展矩形蒙版、复合、音量、独立模糊与有限调色。视频/音频/蒙版关键帧映射到源时间，效果与复合外层使用局部时间；结构和参数回读与实际预览分别记录。日常剪辑默认后台，UI 仅用于单独兼容测试或用户明确要求的操作；导出由用户完成。资源枚举不证明已下载或能渲染。

## 教学操作与当前接口

下表描述接口，不构成调用暂存效果的授权；状态与子集以 [能力状态](capabilities.md) 为准。圆形只准入当前原生视频用途缓存的无羽化子集，其他非矩形旧结构仍暂存；智能抠像/跟踪待办字段不表示后台执行，普通生产不主动列入这些待办。

本表是教学库的共同能力入口，当前字段以editor.py校验与生成路径为准，来源卡的“未接入”保留当时历史。2026-10-08新增音量关键帧、矩形尺寸/羽化、独立模糊动画、有限调色与局部视频/文字/音频复合，详见 [能力状态](capabilities.md)。其他角点、钢笔、跟踪及任意效果复合仍未接入；资源配置与当前呈现分别记录。

| 操作 | 当前可写内容 | 必须区分的缺口 |
|---|---|---|
| 剪切、常速、多轨、静图与错时 | clips 的 source_in/start/duration/speed/track；受限静音speed_curve见正式扩展 | 无生产reverse或自动踩点字段；speed只用正数，负值不替代倒放；同源拼合检查源时码；补帧属于用户负责的范围外操作 |
| 画面运动与透明度 | transform；位置、缩放、平面旋转、alpha 共7种关键帧 | 缩放相对fit；图片/速度1视频两点水平smoothstep按正式扩展及组合限制准入，其他原生缓动/父子约束/任意四角未接入，已测左上角另在composites.perspective；蒙版和独立模糊关键帧分开写 |
| 原生文字样式与运动 | captions.style/transform/keyframes/animations及局部复合；静态text_curve；受限captions.font见正式扩展 | text_curve支持0–360度静态弯曲，普通轨/复合轨编辑重开已测；字体只准入缓存身份已测子集，与其他运动/原生动画的组合拒绝；文字alpha只能静态style或入出动画，其他字体/其他发光包/弯曲角度动画/字形蒙版未准入；静态单行已测缓存字体的放射发光见正式扩展 |
| 片段与独立轨特效/滤镜/动画/转场 | 查询精确枚举和参数后写入 | clip.effects 覆盖整段；独立效果不能指定任意图层集合；group 动画独占片段；近似名称不等同同款 |
| 黑白材质、光效与色键 | 色键按 [安装内Chroma2](capabilities.md#installed-chroma2)，混合按 [安装内混合模式](capabilities.md#installed-mix) 受限接入 | 未选显式模式的色键与旧混合字符串不准入。复杂字形/水墨仍依赖层序 |
| 几何蒙版 | mask+experimental；矩形零羽化位置/旋转/宽高及羽化参数回读，圆形feather=0及位置/旋转/宽高形成椭圆 | common_mask与原素材像素尺寸；圆形当前缓存缺失拒绝；矩形非零羽化另有[RGB覆盖率子集](capabilities.md#mask-red-coverage)，默认非零/其他形状/跟踪未准入，UI编辑重开另验 |
| 主体分离/跟随与描边 | 已有自定义遮罩按clips.matting_cache受限绑定；色键仍按当前呈现边界；新抠像计算/自动跟踪暂存 | 不计算新遮罩或后台编辑笔刷；旧smart_cutout/tracking只表达未执行状态；视频描边未接入，区域裁切不产生人物透明轮廓 |
| 调色、透视、组内合成 | 滤镜；红色HSL/静态三点L及RGB曲线/静态圆形局部调色；全时长/局部video-text-audio复合、rotation与up_left透视 | 其他HSL、动态曲线/色轮/调节层、其他角点、效果轨与多级嵌套未接入 |
| 声音与标记 | 独立audio固定速度、增益、淡化与音量关键帧；44.1kHz双声道speed=1的原生实时降噪及受限-23LUFS响度参数，详见扩展格式 | 离线降噪任务、受限子集之外的响度/混音、**原生**声部分离及节拍标记未接入（离线替代见 [人声分离](capabilities.md#separation)、[自动闪避](capabilities.md#ducking) 与 [节拍候选/切镜](capabilities.md#beat-editing)）；候选不证明精准卡点 |
| 图形库、预设和工作流 | 有权本地图片/视频可用 clips 引用 | 无贴纸/预设/文字模板自动调用、渐变填充、多时间线/代理/联动/备份/批量导出接口；不修改用户全局设置来照抄教程 |

视频与字幕的运动写到各自条目的 keyframes。不能把蒙版中心、效果旋转、音量或任意调色数值混入画面 keyframes；文字 alpha 会被校验拒绝。蒙版区域内调色也不因存在 clip.filters 就自动等于原生调节蒙版。

倒放须区分新逆序计算与reverse_path绑定；开关、accepted或已有路径不证明逆序产物。XML matting.reverse是保留区反转，不能拿来实现时间倒放。剪映宿主原生倒放仍暂存；有界的离线新素材路线已接入，见 [离线短片段倒放](capabilities.md#离线短片段倒放)。

## 查询原生效果

```powershell
$skillRoot = '当前加载Skill的绝对路径'
$skillPython = 'E:\引擎环境\Scripts\python.exe' # 外部虚拟环境，替换为实际路径
& $skillPython "$skillRoot\scripts\editor.py" capabilities --kind scene_effects --search '模糊' --details
& $skillPython "$skillRoot\scripts\editor.py" capabilities --kind filters --search '电影' --details
& $skillPython "$skillRoot\scripts\editor.py" capabilities --kind video_intro --details
& $skillPython "$skillRoot\scripts\editor.py" capabilities --kind text_intro --details
& $skillPython "$skillRoot\scripts\editor.py" capabilities --kind mix_modes --details
```

`kind` 支持 `filters`、`scene_effects`、`character_effects`、`video_intro`、`video_outro`、`video_group`、`text_intro`、`text_outro`、`text_loop`、`mix_modes`、`masks`、`transitions`、`keyframes`、`fonts`。fonts只返回已准入子集，其他枚举仍须结合后台状态选型。默认最多 50 条，`total_matches/truncated` 表明是否截断；需要时用关键词缩小或 `--limit`（最多 2000），不要把前 50 条当全部。

计划填查询返回的 `name`（枚举名），不要按界面印象猜名称。`details.parameters` 给出参数名与原生范围；计划输入统一 0–100，后端映射到原生区间。参数用命名对象避免误用数组位置。缺少或 `null` 使用该效果默认值。效果参数里同名的“速度”“旋转”只是效果自身参数，不能映射到片段的 `speed` 或画面 keyframes。

用户已声明剪映账号拥有会员并授权使用。`allow_vip_effects` 默认 true，允许选用枚举中标记 VIP 的资源及非零 `edge_smooth/spill` 抠像参数，无需再次确认会员权限。保留 `is_vip` 信息用于记录资源性质；仅在用户要求当前任务只用免费效果时设 `allow_vip_effects=false`，查询时可加 `--free-only`。此字段只控制计划策略，实际资源下载和渲染仍需剪映验收。

## clips：附加原生配置

下列字段均可选；组合仍受计划校验限制，不能假定与所有变速、关键帧或字体同时兼容。组合后的真实效果仍须分别验收。

| 字段 | 格式与作用 |
|---|---|
| `filters` | `[{"name":"KE1","intensity":35}]`；作用于整个片段，强度 0–100 |
| `effects` | `[{"kind":"scene","name":"_1998","params":{"effects_adjust_texture":70}}]`；kind 可为 scene/character，作用于整个片段 |
| `animations` | `[{"stage":"intro","name":"动感放大","duration":0.5}]`；stage 可为 intro/outro/group |
| `mix_mode` | `{"name":"滤色","rendering":"installed_mix_v1"}`；仅 [安装内十种混合已核子集](capabilities.md#installed-mix)，下方保留主视频轨道 |
| `chroma` | `{"rendering":"installed_chroma2_v1","color":"#00FF00FF","intensity":35}`；仅 [已核子集](capabilities.md#installed-chroma2)，不是智能人物抠像 |
| `background` | 主画面专用：`{"type":"blur","blur":0.375}` 或 `{"type":"color","color":"#000000FF"}` |
| `mask` | 原生蒙版实验接口，见下文；不自动采用外部渲染替代 |

色度抠除的素材选择、取色、边缘处理和跨类型用途见 [绿幕与色度抠除](green-screen.md)。本地接口的 `edge_smooth` 是边缘羽化，`spill` 是边缘清晰；不要凭英文名把后者解释成通用去绿边。示例中的绿色和强度 35 只展示字段格式，实际颜色与强度按片源判断。

一片段可组合不同效果/滤镜，但不能重复相同效果。局部时段效果可把画面片段切开再添加，或使用独立轨道。动画 duration 单位秒；未填入/出场时长采用枚举默认并限制在片段长度内。入/出场时长之和不能超过片段；group 独占该片段的动画配置。普通关键帧和原生动画都保留在草稿，不能用“写入成功”替代实际动画呈现验证。生成器还会把每条动画绑定到剪映自身的特效缓存（`…/User Data/Cache/effect/<动画 id>/<hash>`，本机 11.6 实测与剪映保存后写入的路径一致），回读时核验路径仍然存在。**本机缓存里找不到可核验包（缺失、多个候选、内容不可解析）的动画，生成器直接不写进草稿**：写一条指向不存在资源的动画，剪映会把它标成「动画丢失」，并在**首次打开时联网下载**该包（2026-10-16 实测：本机 `Cache\effect\` 下 431662 / 624737 / 1696412 / 1696454 / 922958 / 77851352 / 1631524 七个目录都是在用户第一次打开草稿那一刻生成的），下载期间界面同时出现角标、面板里的「资源加载中…」和文字的占位字形（发糊），下完才恢复——「打开慢 + 标丢失 + 字糊」是同一件事的三个表现。丢弃只在**构建**时发生，计划文件仍保留该条目；构建结果给出警告并列出被丢弃的素材、动画和原因（`build_result` 的 `animation_paths.dropped` / `dropped_materials`，另有 `missing`/`ambiguous`/`unverified`/`bound` 计数）。要在界面里一次就干净，先用 `capabilities --kind <动画族> --user-data <User Data>` 拿本机缓存清单：返回 `available_here`（本机已有包、可直接选）与 `missing_here_count`，未传 `--user-data` 时仍是 `unverified`；`resource_availability` 除 `local_cache_checked` 外还可能是 `local_cache_absent`——这台机器还没建过 `Cache\effect\` 目录（新装、没下过任何特效包）时给的就是这个值，此时 `missing_here_count` 是枚举总数、不是逐名判定，不要读成「这 145 个名字都不可用」。不要凭枚举名猜可用性。机器实测口径（2026-10-17，11.6）：`Cache\effect\` 下 331 个目录，六个动画枚举族共 737 个名字里本机有包的只有 20 个——文字入场：渐显、打字机_II、色散拖影、向上露出、水墨晕开、描边填充；文字出场：渐隐、溶解、向右擦除、右上弹出、羽化向右擦除、消散；文字循环：VHS；视频入场：渐显、动感放大、九宫格、砸出波纹；视频出场：渐隐、向上滑动；组合动画：三分割。这 20 个是「一次就干净」的可用集合，其余名字要等剪映联网下载；本机目录数量会随用户操作增长，选名前重新查一次比记住这张表更可靠。保存存活须递归核实际片段引用，不能只数顶层 `material_animations`。2026-10-19 对既有11.6锐利复合稿保存前、首次保存和再次保存的已解码研究副本复核：顶层视频动画材料被去重后，镜头入出/组合动画 `431662`、`624737`、`922958` 仍保留在嵌套复合草稿并由原片段引用；字幕入出场也保留。此前“镜头动画被应用删掉”的结论来自只扫描根层的比较器，现予更正，不能据此要求用户重新挂动画。逐片段动画ID/类型在这两次历史保存均无丢失；这不是本轮新UI保存实验，也不证明所有资源与版本的画面/编辑性。宿主仍会归一化路径，例如字幕outro的缓存目录可能改写；保持当前工程自己的视觉验收。修正证据见工作区 `Skill维护层/变更/2026-10-19-实际问题修复-v1/animation/recursive-readback.json`。

## captions：原生字幕动画

字幕支持 `animations`，stage 为 intro/outro/loop，名称分别从对应文字枚举查询。另支持原生 style（透明度、竖排、字距、描边颜色/宽度、阴影）、transform 与位置/缩放/旋转关键帧，以及顶层 layer_order 的跨文字/视频层序；具体字段见计划格式。连续层序且首轨为视频的全时长/局部复合可以包含文字和音频，外层运动见 [局部复合](capabilities.md#composite)。字体只准入正式扩展的已测缓存子集；文字 alpha 关键帧、纯文字/多级复合、文字/字形蒙版及自定义抠像仍未接入；视频矩形动态蒙版的验证不证明文字组遮罩兼容。示例：

```json
{"text":"示例字幕","start":0,"duration":4,"animations":[
  {"stage":"intro","name":"渐显","duration":0.4},
  {"stage":"outro","name":"渐隐","duration":0.5},
  {"stage":"loop","name":"VHS"}
]}
```

loop 不填 duration，自动覆盖入场结束到出场开始的剩余区间。配置顺序可任意，生成器会正确排序，并修正 0.3.0 的循环起点问题。SRT 不包含动画，动画只存在于剪映工程。文字动画与视频动画一样绑定本机特效缓存，本机无缓存条目的处理同上：构建时丢弃并在结果中报告，不写成丢失引用。枚举名与本机缓存是否齐备是两件事——名称存在不代表这台机器有包。选名后先在 `validate` 阶段看警告：计划里用了本机没有包的动画时，`validate` 会点名该动画（含 id）并列出本机当前可用的替代名，不用先构建再回退；构建结果的 `animation_paths` 计数是第二道核对。`validate` 需要知道去哪找缓存：不传 `--user-data` 时用 `doctor` 配置的那份，配置缺失或这台机器根本没有 `Cache\effect\` 目录时警告改说「还没缓存动画包」（新装机第一次构建会把计划里的动画全部丢掉，这也是它有别于「名字不可用」的一种状态——在剪映里对某条动画点一次、让它下载，再重建即通过）。

查缓存的目录按顺序取，任一可取到就用它：`doctor` 配置的草稿位置（与 `native_user_data()` 同源）→ `JIANYING_USER_DATA`（与草稿路径、`doctor` 同一覆盖入口）→ `%LOCALAPPDATA%\JianyingPro\User Data`；三处都没有 `Cache\effect\` 时回到配置那一份，由上面的警告说明「未检查」。这一步是 2026-10-17 修的：此前只在配置镜像存在时才去找缓存，配置缺失（例如换了机器、设置文件没跟过来）而包其实在磁盘上时，构建会把计划里的动画**一条不写**且不报错，读起来像缓存拒绝了每一个名字。判断标准是构建结果的 `animation_paths`：本机有包的应当是 `bound` 非空，为 0 才说明这一环没找到缓存。丢弃、绑定、回读三层必须用同一个目录：同一轮还修了它的第二半——查找入口换了而绑定层仍读配置镜像时，丢弃层「留着」计划里的动画、绑定层一条也写不进，构建退出码仍是 0，症状同前。其它本机资源（调色、文字发光、模糊、蒙版、扩展、字体）现在走同一套查找：入口是 `resource_user_data()`，候选顺序与上面完全一致，判据是「这条资源要用的文件在不在该候选目录里」。这一步是 2026-10-17 修的：此前只有动画走回退，别的族逐个直调严格查找——配置镜像带 `Config` 却没把 `Cache` 一起带过来时，机器上明明有包，构建仍因「资源不可用」失败（实测：调色局部圆计划 rc=1，报 `Native resource 7416258989467374091 unavailable: <镜像>\Cache\effect\…`；同一计划修后 rc=0、`extensions.grading` 回读为 1）。字体族多一道逐文件 sha256 校验，回退到真机缓存后同样通过。**逐文件判据（同一轮的第二半）**：只按「家族缓存目录存不存在」判，会把带着一个空 `Cache\effect` 的镜像当真——那种镜像下构建继续响亮失败，而真机其实有包（实测 rc=1）。改成逐资源文件探测后，这样的镜像不再被采纳，改取真机那份：同一命令 rc=0、`extensions.grading` 回读为 1（验收第 9 条固定这一行为）。**显式根原样生效**：调用方明确给出 `user_data` 时不做探测，就按给定目录查——自检与验收都靠这一点才能在沙箱里判成败；只有传 `None` 才走候选链。因此各族的辅助函数（`native_advanced.cache_root()`、`native_text_glow`）只在拿到 `None` 时解析，editor 侧只传 `None`（或调用方给的显式根），不再把解析后的根转交下去。一处都取不到时它把原配置目录交回调用方，于是仍旧**响亮失败**（`Native resource <id> unavailable: <目录>` 或 `Admitted font cache unavailable or changed: <路径>`），不会静默少写一个参数。回归里的 `animation_path_self_check` 有三组守卫：源码里除三个解析器（`native_user_data()` / `configured_user_data()` / `resource_user_data()`）外任何一行都不允许再出现严格查找；editor 不得把解析出的根交给家族辅助函数（出现即报出那一行）；显式根不得被解析器顶掉。解析器行为本身也在同一检查里断言（镜像优先、环境变量、默认位置、全都没有时保留配置、无缓存时留给调用方报错、显式根原样）。

loop VHS 的观感要预先知道：本机 `text_loop` 枚举 93 个名字里只有 VHS（`77851352`）有本地特效包，而它的本体就是「磁带故障」——包内 `TurbulenceDisplacement.prefab`（湍流位移）、`mosaic_camera.prefab`（马赛克）、`lumi_dilate_erode.prefab`（膨胀/腐蚀）三层叠在文字上，`lua-meta.json` 的作者路径写作 `文字动画_Vhs`。于是 loop 区间里字幕的**重影、偏色和发糊是这个动画本身的效果**，不是渲染缺陷；字越小、画布越小（例如 320×240 上 size 8）越像糊成一团。2026-10-16 用户看到的「中间这段是糊的」即此例（入场渐显与出场右上弹出区间不带它）。要清晰字幕就去掉 loop，或改用不带破坏性的入场/出场动画；要在界面里演示 loop 阶段，本机目前只有 VHS 可放。

## 独立特效/滤镜轨道

顶层 `effects`、`filters` 是可选数组，直接创建原生轨道；默认轨道名分别为 effects/filters，同轨不能重叠，不可延伸到主画面之外。不同轨道可叠加：

```json
{
  "effects":[{"start":1,"duration":1.2,"track":"特效轨","kind":"scene","name":"_70s","params":{"effects_adjust_speed":40}}],
  "filters":[{"start":0,"duration":4,"track":"滤镜轨","name":"ABG","intensity":60}]
}
```

独立特效轨作用于该时间范围的画面，不保证只影响指定的画中画。若只想影响一个镜头，优先使用该 clip 的效果字段。

## 蒙版与画中画

普通矩形画中画可用多个视频轨道和transform；需要裁区时使用mask。全时长/局部复合用composites，内部多轨和原素材保持可编辑。矩形与无羽化圆形已测11.5 common_mask，保留experimental:true标记；其他形状仍暂存。圆形示例：

矩形裁切现在绑定已测本地缓存，并核对关键文件身份；空资源路径和变更缓存拒绝。零羽化静态/反转、位置尺寸动画及局部视频文字复合已有新原生静帧；非零矩形羽化仍未准入，生产校验/构建明确拒绝；只有零羽化几何可用。详见 [矩形蒙版](capabilities.md#mask-rect)。

```json
{"mask":{"name":"圆形","experimental":true,"size":0.7,"feather":0,"center_x":0,"center_y":0}}
```

可配置size、center_x/center_y、rotation、invert。center为原素材像素偏移，size为原素材高度比例，>0至4；矩形与圆形裁切蒙版当前都只准入feather=0。rect_width（素材宽比例）和round_corner（0–100）仅矩形可用。每片段只有一个蒙版；不能与同片段局部调色蒙版共存。圆形五属性动画、资源身份、复合与羽化失败边界见 [圆形与椭圆](capabilities.md#mask-circle)，单位见 [计划格式 · 蒙版](plan-format.md#蒙版)。教程界面里的蒙版尺寸（如 400、2100、2560）是界面像素值，不是计划的 size 或 rect_width 比例，按原素材尺寸换算后再填。蒙版中心偏离片段中心时，旋转片段会让圆窗绕片段中心转圈而不是原地自转；做唱片式自转时把蒙版 center 设为 0，用 transform 移动位置。“镜面”蒙版是对称的带状显示区，旋转后为斜条，不是倒影或镜像翻转（该形状仍暂存）。调色用途的圆形区域（`grading.local_circle`）不走本节的 `mask` 字段：交付后剪映打开保存重开，渲染仍是同一椭圆，但「蒙版参数」面板显示的是应用自己补的默认矩形，尺寸回读以计划或交付稿为准，改尺寸回计划重新生成。结构、静帧、UI/播放分别记录，不改成烘焙素材或自动转前台补做。

## 暂存操作与未执行状态

智能抠像和自动跟踪尚未接入可靠的新计算路径，普通剪辑不添加到计划或`jianying_tasks`，也不自动转为用户手动待办。先采用满足表达的已支持设计；必要效果无法等价实现时，在交付中说明具体剪辑缺口。

`jianying_tasks`仅兼容保留smart_cutout/tracking的未执行记录，不提交算法、不构成成功证据；含该记录的交付标记partial。只有用户当前明确要求相关研究或界面补做时才另行处理；工具不可用时如实说明未完成，不把必要操作推给用户后宣称剪辑完成。实际补做、UI和声音状态分别记录，不凭删除待办升级证据。

## 回读与兼容验收

显式layer_order与复合必须启用自由render_index模式，索引顺序本身不足以改变旧默认的文字前置。当前生成器写入并回读主稿/嵌入稿的模式，离线publish同步sidecar；该修复已用前景矩形遮挡文字的独立静帧核验。

`build/publish` 回读素材引用、原生参数、动画类型和时间。`verification.json.native_effects` 中 `parameter_readback=passed` 仅表示结构，`resources_and_rendering=unverified` 保留真实边界。测试还核对原素材路径和哈希未变；不产生烘焙效果视频。

计划使用原生转场时，另核对 `draft_content.json` 中 `materials.transitions` 与相应片段 `extra_material_refs` 的引用，检查实际资源、挂载片段、duration 和 is_overlap 是否符合计划；只统计计划里的 transition 字段或看到一般原生参数回读通过，不足以证明转场已正确落盘。结构核查与播放反馈分别记录，不能由其他工程的认可推定当前草稿也已通过。

用户负责最终完整视听检查和导出；普通后台草稿的实际资源/可编辑性验证仍如实标记。需要 UI 补做时，Agent 先核对相关参数、效果显示与保存重开，必要步骤未执行应列剪辑缺口。局部通过不证明全部资源或导出正确。剪映账号权限与缓存状态可能影响显示。不能使用未获得许可的资源或替用户开通会员。

接口与兼容依据：[pyJianYingDraft 官方仓库](https://github.com/GuanYixuan/pyJianYingDraft)，另核对本机固定版本的公开方法。
