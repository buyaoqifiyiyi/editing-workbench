# 达芬奇主剪辑＋AE局部加工

工作台当轮选择达芬奇时，主时间轴或单独剪辑任务使用本接口；本页不决定其他会话的软件。复用Agent、素材分析、会话、任务调度与成果面板；`resolve-workflow`是CLI/MCP共用白名单入口，不依赖PR，也不自动迁移剪映作品。组合中使用AE时，其原生图层留在AEP，回填的是渲染素材，使用下文有界镜头返回流程。

选段与制作前先读[创作决策层](../guides/planning.md)，动漫/游戏情绪混剪与MV再读[镜头与音乐如何共同推进](styles/anime-mix.md#镜头与音乐如何共同推进)。制作与修订持续判断声画、连接和技法，再按本页子集落地；创作意图写在项目说明中，严格Resolve计划不接收额外notes或审美字段。复杂表现受本页准入约束，不能把其他宿主的技法说明当作已可执行能力。

## 当前有界范围

限定Windows和已实测的Resolve 21.1.1（含本机21.1.1.10），需安装目录中的ResolvePython与既有脚本连接。不会替用户修改脚本安全设置。升级版本后必须重新验收，不能跳过版本守卫。

- 原生顺序视频剪切，显式记录帧位置；一个视频轨。
- 原声与配乐：48kHz双声道，原声可静音；各非静音片段用独立立体声音轨，线性音量0或0.00001..2。
- 静态缩放、位置、顺时针旋转、透明度；参数写入原生工程并逐值读回。
- 纯文字字幕：中文、单/双行、跨剪辑点，导入可编辑SRT字幕轨并烧录导出；逐条核对文字、起止帧与轨道启用状态。
- 保存、关闭并重开比较；导出可编辑`project.drp`及NVIDIA H.264/AAC `output.mp4`，检查实际帧数/尺寸/帧率并全解码。

位置关键帧、多层视频、调色、变速、Fusion、AI及外部插件仍不在达芬奇执行子集内，未知字段明确拒绝。字幕样式沿用新工程的字幕轨默认设置，本接口不接字体、颜色、位置或字词动画；需要已准入AE文字/运动时按下文处理镜头。不生成缺效果的工程后声称成功。

视频源及画布fps暂限30，画布为偶数尺寸，单边不超过1920且短边不超过1080。最多256个无空洞、无重叠的顺序视频片段、16段独立配乐、总长600秒；这些是参数上限，不代表做过相应压力测试。源必须为恒定30fps；`inspect`的平均帧率不能证明CFR，因此prepare及执行前后会重新检查全部视频帧PTS，拒绝变帧率、时间戳断裂或源帧不足。首阶段每个视频源也限600秒、最多18001帧，PTS检测限时120秒。长源先将选中片段另存为恒定30fps、Rec.709、48kHz立体声中间素材，保留原片与转换记录；不把VFR的秒数直接当作源帧索引。

## 命令

通过当前引擎解释器执行`-m jianying_engine`（路径见`paths`），无需修改全局PATH：

```text
jianying-engine resolve-workflow configure --resolve-root <达芬奇安装目录>
jianying-engine resolve-workflow doctor
jianying-engine resolve-workflow validate --plan <resolve-plan.json>
jianying-engine resolve-workflow prepare --plan <resolve-plan.json> --out <新版本交接目录>
jianying-engine resolve-workflow run --bundle <交接目录> --timeout 600
jianying-engine resolve-workflow verify --bundle <交接目录>
jianying-engine resolve-workflow return-ae --bundle <已成功AE交接目录> --plan <主剪辑原计划> --clip-index 2 --shot-index 1 --out <新回填计划.json>
```

配置保存在引擎外的`%LOCALAPPDATA%/JianyingEngine/resolve.json`，也可显式传`--resolve-root`，或用`JIANYING_RESOLVE_ROOT`。`doctor`只读程序文件与版本，不证明宿主连接或授权权益。

先打开达芬奇并等待就绪。`run`只连接已有宿主：如果打开了已保存/非空用户工程，执行器拒绝创建，不关闭或强退它。成功导出后，仅当本任务创建工程的UUID和完整快照仍匹配时，保存并正常关闭它，让下一新任务可以连续运行；用户在执行期间改动工程则停止并保留现场。软件异常或脚本连接不可用时保留现场，不改安全设置、不循环启动或强杀进程。

`prepare`只创建新交接目录，计算计划、全部媒体与执行代码身份。执行前后重新探测原计划、核对规范化内容及全部哈希；改过代码、源片或参数后必须prepare新版本。`run`跨进程互斥，每个交接包仅尝试一次，超时也不重用；先查看失败回执和宿主状态，再决定下一版。

## 计划格式

```json
{
  "schema_version": 1,
  "backend": "resolve",
  "name": "剪辑版本名",
  "canvas": {"width": 1920, "height": 1080, "fps": 30},
  "clips": [
    {"path": "source.mp4", "source_in": 0, "start": 0, "duration": 2,
     "volume": 0.5,
     "transform": {"scale": 1, "x": 960, "y": 540, "rotation": 0, "opacity": 1}}
  ],
  "captions": [{"text": "中文字幕\n第二行", "start": 0.2, "duration": 1.6}],
  "audio": [
    {"path": "music.wav", "source_in": 0, "start": 0, "duration": 2, "volume": 0.25}
  ]
}
```

所有时间以秒输入，必须落在完整30fps帧上；源范围不得超过实测时长。路径为计划文件相对路径或本机绝对路径，拒绝URL/UNC。`scale`为fit-contain基础上的等比倍率；`x/y`为画布像素，默认中心；`rotation`正数顺时针，范围±360度；`opacity`为0..1。原生角度方向相反，执行器负责转换；原生音量使用20log10(线性增益)的dB，0音量不生成该音轨。不可因SetProperties返回True就省略读回。

不修改项目全局色彩管理。输入须有明确色彩解释；初测无标签源出现色偏，按Rec.709转换并标记的对照消除了该测试的偏移，不代表所有色彩场景已认证。

字幕字段为`captions[].text/start/duration`，最多512条，按时间升序且不重叠，不越主时间轴；时间同样必须落在整帧。文字最多500字符、1–2个非空行，可用换行，不接SRT样式标签或额外样式字段。交接包的`captions.srt`由整数帧生成，执行时重算核验。原生SRT相对于时间轴当前末尾追加，因此执行器先在空时间轴导入字幕，再按显式帧位放入视频/音频，避免字幕整体后移。

## AE镜头加工与回填

只有当轮软件组合包含AE，或用户明确要求AE加工时才走此流程，不因字幕需要自动切软件。先按[独立AE调用](adobe-workflow.md#独立ae调用)准备并渲染新AE包；只使用该入口已经准入的原生文字、静态变换和线性运动，不使用PR或Dynamic Link。

`return-ae`接收成功AE包与原始Resolve主计划，镜头编号从1开始。AE单镜头局部起点可为0，即使目标是主时间轴的第2段或更后镜头；源路径、源入点、时长、画布/帧率和原声增益必须匹配。独立AE配乐并未烘焙到镜头中，AE计划的`audio`必须为空，配乐留在主计划混合。

返回时重新核验AE包、原始素材、实际AVI解码及成功执行记录中的渲染哈希，只替换选定镜头；新片段入点归零，音量改为1、变换归中，因为AE已烘焙对应增益/变换。已核验的AE rawvideo RGB片段标记`data_level="full"`，导入后明确设置和读回完整色阶，避免Auto错误扩展灰阶而改变文字淡入淡出。其他视频默认`data_level="auto"`；同一源文件不能在同一工程请求冲突的源色阶。保留其余镜头、主配乐和字幕，另存新计划与`.return.json`记录，不覆盖原计划。随后对新计划执行validate/prepare/run/verify；返回计划成功不代表达芬奇工程已经生成。

交付同时保留AEP与中间AVI：AEP仍可编辑文字/运动参数，Resolve接收带效果视频，不包含可调AE图层。修改AE后生成新的渲染包并再次回填，不能用手改成功回执代替渲染。AE冷启动使用只读就绪探针，空工程就绪后只派一次创建脚本；已有工程或启动恢复窗口会阻断，不修改安全设置或强杀宿主。

## 交付与验收

成功状态是`technically_completed`，且`execution.json`中`native_project_verified`与`native_render_verified`为true。`prepared_not_executed`不是工程已生成；渲染队列Complete仍需父进程解码验证。失败留`native-result.json`、`execution.json`和日志。

只把`project.drp`、`output.mp4`、最终字幕`captions.srt`、需要交付的AEP及最终说明登记到项目根`workbench-delivery.json`；规范化计划、回执、日志和抽帧不是正式成果。DRP在成果面板按原生工程展示，可打开所在目录，在达芬奇项目管理器导入。

技术检查不证明所有画面、音频内容正确；切点、构图、左右声道、混音与色彩要另做项目见证检查，最终完整主观视听由用户验收。长片、高负载、崩溃恢复和AE高级镜头回填分别验收，不把首阶段成功写成完整无人值守或所有剪辑功能已迁移。
