# AE局部制作与PR历史交接入口

按工作台本轮软件配置选择独立AE制作或组合中的AE镜头加工；本页不决定默认主剪辑软件。PR历史交接流程保留，当前软件目录未开放PR选择。旧剪映计划、草稿与素材不移动、不改写。统一操作为`adobe-workflow`，CLI/MCP共用白名单，具体参数查操作及子命令`--help`。

## 独立AE调用

选择“单独调用AE”或组合中只使用AE时，先按下文有界计划执行`validate`、`prepare`，再执行`run-ae --bundle <新交接目录> --ae-root <AE的Support Files路径>`，最后`verify --bundle <交接目录>`。使用已有AE路径配置时可省略`--ae-root`。这些步骤不要求PR安装、PR配置、完整Adobe的`doctor`或XML导入；`run-ae`只检查AE依赖，不能以其他软件缺失阻断独立AE任务。

`prepare`沿用已有`backend=pr-ae`计划格式和受信生成器，会附带历史XML文件，但独立AE交付以`ae-project.aep`、按需保留的重开副本和镜头AVI为准；XML不是必须交付的主工程。AE只渲染各镜头与镜头内原声，独立`audio`配乐原本由主时间轴混合，独立AE任务不能将该字段写入后声称已混入镜头输出。需要独立配乐混音或完整多镜头时间轴时，按用户目标和已选择的后端另行处理，不能隐性启动PR或达芬奇。

AE已有实例/工程保护、原生保存重开与渲染验证沿用下文守卫。这里开放的是既有AE有界能力的独立调用，不包含任意特效、完整高级往返或其他宿主原生工程。

当轮组合包含达芬奇主剪辑时，已成功AE包可通过[有界镜头回填](resolve-workflow.md#ae镜头加工与回填)的`resolve-workflow return-ae`另存主计划；不依赖PR。该步骤保留AEP和渲染AVI，在主时间轴只接收已核验视频，避免再次施加AE已烘焙的增益与变换。

## 当前可执行范围

支持明确`backend="pr-ae"`、`schema_version=1`的新计划。画布最高1920×1080或1080×1920，整数24/25/30/50/60fps，全片不超过30秒、最多8个连续恒速视频镜头；时间必须落在整帧。源视频、画布规格不同会在AE中按contain适配。前景遮罩仅接受下文明确提供的凸多边形；不接静图、独立叠加视频、自动人物分割/跟踪、其他遮罩、转场或剪映效果枚举，未知字段明确拒绝。

每个镜头独立AE合成，支持像素位置、缩放、旋转、不透明度与线性运动关键帧；文字层支持独立的位置、缩放、透明度线性关键帧。跨镜头文字使用整条文字的时钟，按镜头交集求值，不在每个镜头重启动画。文字使用像素字号，不沿用剪映字号单位。原声保留在镜头中，独立BGM由所选主剪辑后端混合。AE `.aep`保留参数，返回视频不包含可调的AE原生图层。

```json
{"schema_version":1,"backend":"pr-ae","name":"Adobe sample","canvas":{"width":320,"height":180,"fps":30},"clips":[{"path":"source.mov","source_in":0,"start":0,"duration":2,"volume":1,"transform":{"scale":1,"x":160,"y":90,"opacity":1},"motion":[{"time":0,"x":145},{"time":1.9666666666666666,"x":175}]}],"captions":[{"text":"PR + AE","start":0,"duration":2,"font_size_px":24,"color":"#FFFFFF","x":60,"y":35}],"audio":[]}
```

`transform.scale`为contain后的倍率；`x/y`为画布像素中心；opacity为0–1，volume为0–2线性增益。视频的motion.time相对镜头起点，逐属性独立线性求值并合并位置向量时间点，锁定空间直线插值，不宣称缓动/跟踪等高级族完成。独立audio字段为path/start/source_in/duration/volume，首版仅限stereo，mono BGM明确拒绝。

## 可编辑整层动态文字

文字条目为`captions[]`，必填`text/start/duration/font_size_px`，可选`color/x/y/scale/opacity/motion`。`scale`为文字层倍率（默认1，0.01–10），不是字号；`opacity`默认1，范围0–1。`motion`最多64个关键帧，使用`time`及至少一项`x/y/scale/opacity`，时间相对该文字条目的开始，严格递增且落在整帧内，不得等于或超过文字持续时间。每项属性独立线性插值，未给出的属性保持既有曲线或基础值；未在time=0赋值时以基础值作为0时刻端点，向首个关键帧插值，最后关键帧之后保持到文字结束。

```json
{"text":"动态文字","start":0.5,"duration":2,"font_size_px":72,"x":400,"y":260,"scale":0.8,"opacity":0,"motion":[{"time":0,"x":400,"opacity":0},{"time":0.4,"opacity":1},{"time":1,"x":800,"scale":1.2},{"time":1.9666666666666666,"opacity":0}]}
```

这里是整层文字的线性运动，不是逐字动画器、表达式、缓动预设或第三方插件。跨剪辑点时，各镜头保留同一条文字曲线的对应片段，原生图层分别存在于各镜头合成中，完整工程仍可编辑。

执行器保存前与重新打开AEP后生成`ae-text-readback.json`，记录文字、字号/颜色、图层入出点及位置/缩放/透明度关键帧的时间、数值与线性插值。父进程对照规范化计划核验；渲染后还要检查实际文字运动和时序。只有源码或readback通过，不等于完整视听验收。

## 可编辑前景多边形遮罩

`clips[].foreground_mask`将同一源视频的指定区域放到文字前方，适合已有明确轮廓的主体与文字穿插。对象必填`vertices`，可选`feather_px`和`inverted`；不提供此字段时保持原有单源层结构。顶点为3–32个`[x,y]`，按顺时针或逆时针沿边界排列，必须组成严格凸多边形：不重复点、不重复首点闭合、不共线、不自交，也不接受凹多边形。坐标必须为有限数值且不能是布尔值，范围为`0 <= x <= 源视频宽度`、`0 <= y <= 源视频高度`。坐标使用源视频原像素，不是画布坐标；源与画布尺寸不同也不重写顶点。

```json
{"foreground_mask":{"vertices":[[160,40],[480,40],[480,340],[160,340]],"feather_px":4,"inverted":false}}
```

上例是添加到源尺寸至少640×360的clip中的字段。`feather_px`默认0，范围0–100，为源像素标量，写入AE双轴羽化`[f,f]`；`inverted`默认`false`，仅接受布尔值。执行器创建一份原生可编辑的封闭ADD遮罩，顶点切线全零，遮罩透明度固定100、扩展固定0。不接受遮罩动画、Bezier切线、多遮罩、扩展或其他未列字段；自动人物分割、轮廓跟踪与插件仍未准入，运动主体需要另行提供合适轮廓，不能将静态多边形当作自动抠像。

每镜头先创建源层和相交的文字层，再复制既有源层为`Editable foreground N`。复制层继承同源路径、非零源入点、入出点、contain适配、静态变换与已准入运动，显式关闭音频并置于最上方；层序为前景、文字、原源层。原源层承担原声，前景不会重复增益。遮罩跟随源层变换运动，文字仍保留自己的动画。为了避免两份半透明源在遮罩内叠加改变语义，有前景遮罩时`transform.opacity`必须为1，任何显式视频`motion.opacity`也必须为1；淡入淡出可用于文字，无遮罩视频保持原有透明度范围。

执行器保存前及重开AEP后写入`ae-layer-readback.json`，记录每镜头全部图层的实际层序，以及前景源路径、源入点、入出点、音频开关和唯一遮罩的名称、模式、反转、顶点/切线、闭合、羽化、透明度与扩展。父进程逐项对照规范化计划；该回读与文字回读分别保留，输出均拒绝覆盖。参数核对之外，还需通过原生渲染检查遮挡、轮廓边界、继承运动和声音；本轮实际宿主结果以包外验收记录为准，编译测试不证明原生保存重开或画面成功。AEP保留可编辑前景和文字，回填视频为烘焙结果。

## PR历史交接流程

1. `adobe-workflow configure --ae-root "AE的Support Files绝对路径" --pr-exe "Premiere主程序绝对路径"`只保存外置应用路径；`doctor`只检查路径与文件身份，不认证安装完整性、授权或接口可用性。
2. `adobe-workflow validate --plan 计划.json`先探测媒体并拒绝越界/非整帧/未知字段；不会启动宿主。
3. `adobe-workflow prepare --plan 计划.json --out 新目录`生成受信JSX、标准`premiere.xml`、规范化计划和来源哈希；输出目录必须新建，原片不覆盖。
4. `adobe-workflow run-ae --bundle 交接目录`要求当前无AfterFX.exe、AfterFX.com或aerender进程；仅执行从当前计划重新生成并逐字核对的JSX，拒绝脚本/源媒体/编译器漂移。正常启动自有AfterFX.exe，待启动后用.com分发脚本，创建/保存/重开检查AE工程，退出自有创建实例后再aerender；使用Lossless/无损模板输出rawvideo AVI＋PCM16，模板缺失或输出有损明确失败，不能静默退回H264。崩溃恢复/启动对话框可能阻断脚本，需要正常模式进入后在明确所有权与空工程条件下处理；不自动重置偏好、不把人工启动恢复称为无人值守通过。失败回执与日志保留，不强行终止其他软件或重复覆盖运行。渲染后重新核来源SHA，音频核PCM/48k/stereo、样本时长及相对源选区的增益RMS；RMS不能证明所有音频内容对齐。
5. AE全部输出核对尺寸、帧率、帧数并全量解码后，在PR原生“文件→导入”导入`premiere.xml`，另存新的`.prproj`，验证关闭重开和实际导出。这里是legacy Final Cut Pro XML/xmeml v4，不是FCPX的`.fcpxml`。
6. `adobe-workflow verify --bundle 交接目录`重新校验受信生成物、来源和镜头输出；不代替PR原生工程重开、内容检查或导出验收。

PR目前没有在本工作台准入无需插件的公共命令行导入/保存入口，原生界面步骤需按实际工具条件执行并报告；准备XML不等于PR工程完成。本入口不安装插件、不启用开发者模式、不使用QE、不改防护或激活。完全无人值守的PR控制、AE任意特效、复杂音频/色彩和旧剪映工程无损转换均不在首版覆盖范围。

`run-ae`在外置配置目录使用Windows进程间非阻塞宿主锁，覆盖检查、启动、渲染与校验；另一工作台任务占用时明确返回busy，异常退出自动释放。用户另行启动AE不受此锁控制，仍由运行进程检查和JSX现有工程拒绝守卫保护。冷启动仅派发一次只读就绪探针，之后最多等待120秒，只轮询随机token回执与自有进程状态；派发报错或超时也不重复注入脚本。只有有效回执确认空工程后，才重验交接包并仅派一次创建脚本；已有工程或就绪超时保留现场、不强杀。超时明确报告单次探针后启动对话框可能阻断、未派发创建且未强制终止进程；不能保证启动期间首个探针一定被接收。实际启动恢复窗口仍可能阻断，不因一次正常冷启动成功推断所有启动情况已解决。PR曾在保存工程重开后无响应，保存/重开稳定性尚未准入。

XML声道采用已在PR原生导入与导出中验证的成对立体声结构：sequence启用`explodedTracks`，每组轨道声明`currentExplodedTrackIndex=0/1`、`totalExplodedTrackCount=2`与`premiereTrackType=Stereo`，音频clip声明`premiereChannelType=stereo`，左右各对应一个mono输出组；媒体file首次完整定义，后续只引用id。普通双mono轨仅设置输出通道仍会在PR中居中混音，不作为可用结构。原声增益已由AE写入镜头片，XML不重复施加；BGM以计划线性增益混合，不额外加声像或sqrt(2)补偿。当前BGM只接收双声道；更多声道与mono保持拒绝。原生证据包含左440Hz/右880Hz分离与原声0.5＋BGM0.25的增益对照；这不替代用户完整主观视听验收。

来源：[Adobe XML导入](https://helpx.adobe.com/premiere/desktop/organize-media/import-files/migrate-from-final-cut-pro-x.html)、[Adobe Project API](https://developer.adobe.com/premiere-pro/uxp/ppro-reference/classes/project)、[Apple xmeml](https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/FinalCutPro_XML/Basics/Basics.html)。安装/版本、原生输出和PR实机闭环证据保存在包外维护记录，不能用文档或静态字段检查代替。
