# 模板工程方法库

仅在已经形成的设计需要原生工程参数证据，或用户明确要求模板复用时查阅。Agent默认从实际素材独立构思，不先检索模板再反推作品；这份库保留技术样本而不提供默认成片答案。相册、婚礼、企业、旅行和节奏短片中的方法可按需迁移。

## 外部数据与检索

模板索引和逐工程准入记录是本机数据，存放在 Skill 外。默认位置为 `%LOCALAPPDATA%/JianyingEditor/template-data`；通过绝对路径环境变量 `JIANYING_TEMPLATE_DATA_DIR` 可改到工作区外部数据目录，不允许指向 Skills 内部。文件为 `template-index.json` 和 `template-validation.json`。只移动/恢复原文件，不手工改 passed 或移除环境指纹；丢失数据时明确报错，不能静默选另一库。

新设备可复制外部索引/记录并设置新目录，源根用 `--source-root` 覆盖；来源、后端和实现变化仍须重新检验。空库可用 `verify_templates.py --draft ... --out ... --register` 登记首份来源。报告和测试工程只写独立任务目录。

实际数据目录以当前运行进程解析结果为准；打包宿主的LOCALAPPDATA可能不同。换到独立引擎后若默认索引缺失，先查原打包环境的外置库，不创建空库或重置准入。本机确认目标后可用显式环境变量或目录连接复用原库；只有确认默认目录不存在才建连接，不替换既有目录。每份来源的显式root优先，未指定root的别名使用本次source-root或索引source_root，不能把首个别名的root当成所有别名的默认值。缺来源时记录失败，不按标题猜路径或沿用历史通过数量。

```powershell
# $skillRoot 为当前加载 Skill 的绝对路径；不要照搬旧设备用户名。
$templatePython = 'E:\引擎环境\Scripts\python.exe' # 外部虚拟环境，替换为实际路径
& $templatePython "$skillRoot/scripts/templates.py" --search '相册 关键帧' --limit 3
& $templatePython "$skillRoot/scripts/templates.py" --id '查询结果的TPL标识' --verify-source
```

空格分隔检索词必须全部匹配标题、结构标签、效果和动画名称。标题仅作意图线索，JSON/封面不证明播放、算法或资源已执行；找不到合适模板时继续原创，不强行套相似样本。`--verify-source`只核对选中工程存在及字节哈希；来源不可用/变更退出2，未知ID退出1。原路径和索引摘要不能证明新交付自包含，实际素材另按授权整理。

## 参数阅读与迁移

需要照片墙收束、错峰轮播、滚动队列、文字阅读/离场、产品信息层次的具体参数依据时，读取 [模板图层编排与信息运动](template-layout-patterns.md)。该卡来自2026-10-08用户提供的电脑版模板草稿，区分旧sticker轨文字、预合成背景、二维空间与异常时码；它补充方法判断，不新增整套复用能力。

旧工程常将关键帧存于顶层keyframes分组，由片段keyframe_refs绑定；不要只找现代common_keyframes，否则会漏掉运动。按实际引用关联materials、extra_material_refs和关键帧ID，区别未使用素材与生效参数。

先记录层序、源范围、片段起止、声音依据、入场/到位/停留/释放，再决定哪些关系适用于新素材。坐标和时码依旧格式、画布、裁切、速度与源入点核对；数值相同不意味着视觉结果相同。当前计划用片段局部秒数，按 [计划格式](plan-format.md) 映射，不能原样塞入旧JSON字段或任意资源ID。

直接绑定与实际算法状态也要区分：存在matting.path但flag=0不能说启用了抠像；存在视频跟踪记录不能说已执行跟踪；存在特效ID不能说当前版本可下载或显示。高级能力仍遵循 [能力状态](capabilities.md)。

## 拆解与改造工具

### 入库与持续检验

用户要求已有模板及以后新增模板都检验后台可用性。`scripts/verify_templates.py` 对每份来源进行真实媒体定位/探测、逐片段计划校验与后端构造、受支持静态资源的精确枚举匹配、测试草稿生成和独立参数/引用回读。测试覆盖片段的线性运动及当前支持的参数子集；原复杂裁切、未由模板重建验证的算法/字体/贴纸/缓动等不会因为基础参数通过就升级为整套可用。

```powershell
# 全库检验；--id可重复使用以检验指定来源。
& $templatePython "$skillRoot\scripts\verify_templates.py" --out 'D:\检验任务\全库-v1' --register
# 新增本地工程，检验后写入索引及后台准入记录。
& $templatePython "$skillRoot\scripts\verify_templates.py" --draft 'D:\新模板\draft_content.json' --out 'D:\检验任务\新模板-v1' --register
# 新增ZIP，只读取指定工程与其实际引用的主素材，不整包解压。
& $templatePython "$skillRoot\scripts\verify_templates.py" --draft 'D:\新模板.zip' --member '包内工程/draft_content.json' --out 'D:\检验任务\ZIP-v1' --register
```

检验结果保存在任务目录的检验结果/汇总及参数证据文件。外部 `template-validation.json` 是机器准入记录，包含来源SHA256、当前剪映/后端和实现指纹、各片段与原生组件的通过/失败状态；不要手工写passed、复制旧状态或只改索引的evidence字段。templates.py查询会附backend_admission；template_borrow.py实际改造前再次检查准入，无当前记录、未通过片段、未经验证时间基准或实现/环境改变时拒绝。只读拆解可继续用于研究。执行中的新素材和新时间轴仍须按普通生成器校验并build回读。

partial_verified表示有片段子集通过，不能扩大为完整模块迁移或成片可用；research_only表示未取得可执行片段证据。未知时间基准不通过范围匹配猜测；本自动检查只准入旧源入点0、原速度1的运动，以及无需运动换算的静态片段。特殊格式可保留研究证据，待有明确解释和专项重建测试后维护检验器，不手工解除。

实现或环境改变后，登记保留未重验工程的旧记录及其自身environment，只更新真正重验的工程；旧记录继续stale_environment，不因全局指纹更新自动准入。失败重验也应保留真实状态，不能清空其余工程证据。独立控制模板可验证替换素材、文案、同比改时长和新ID生成，但只能证明机制；原库每份来源仍须独立取得当前准入。判断顺序见[11模板复用](progressive-integration.md#11-模板复用来源准入和局部重建分别判断)。

测试逐片段保留失败原因，不按模板数量计算技法达成率。source媒体丢失、歧义、范围越界、字段/枚举不兼容与后端构造失败分别记录；同ID冲突的静态资源不任选。原生资源的精确枚举及参数回读仍不能证明下载/显示正常。UI预览、保存重开、视觉与声音留在未验证状态，按主Skill分工交付。

所有测试草稿、临时ZIP媒体与背景色卡默认随临时目录删除，不写现用草稿目录。保留检验报告、失败原因、参数、内容哈希与校验回执；正式作品和原模板不清理。此前测试目录的清理先保留必要证据，并核实绝对路径位于明确的测试目录内。

`scripts/template_borrow.py` 补上来源读取到普通剪辑计划的路径。先核对索引哈希，再按“轨道序号:片段序号”（均从0开始）关联主材料、附加材料、递归可关联材料、顶层旧关键帧及现代片段关键帧。报告保留完整片段参数、包内/ZIP资源定位、缺失引用和同ID冲突的所有材料版本；不任选冲突记录，不下载资源，也不修改原工程。ZIP条目定位不表示媒体已解码或资源可显示。

```powershell
& $templatePython "$skillRoot\scripts\template_borrow.py" --id 'TPL-9c23a5410f35' --out 'D:\当前任务\模板拆解-v1'
& $templatePython "$skillRoot\scripts\template_borrow.py" --id 'TPL-9c23a5410f35' --mapping 'D:\当前任务\局部映射.json' --base-plan 'D:\当前任务\原创计划.json' --out 'D:\当前任务\局部改造-v1'
& $templatePython "$skillRoot\scripts\editor.py" build --plan 'D:\当前任务\局部改造-v1\edit-plan.json' --out 'D:\当前任务\新草稿-v1'
```

无mapping时只生成`拆解报告.json`。有mapping时另存`edit-plan.json`和`借鉴记录.json`，调用现有生成器检查素材、字段、范围和时间轴；借鉴记录的`plan_validation=passed`才表示可进入build。验证失败保留失败证据并返回退出码2，不把失败计划交付为已完成。输出目录必须不存在。原计划、原工程与模板资源均保留；测试草稿不发布到用户现用草稿目录。

映射由Agent结合素材分析与当前设计填写，例子只展示参数格式，不是固定表现：

```json
{
  "name": "当前作品-局部运动改造-v1",
  "selections": [{
    "segment": "0:0",
    "path": "素材/当前照片.jpg",
    "start": 4,
    "duration": 2.5,
    "track": "局部画面",
    "fit": "contain",
    "time_basis": "local",
    "motion_mode": "linear_rebuild"
  }]
}
```

有base-plan时将选择追加到其新副本，多份模板可依次并入不同新输出目录；同一片段也可多次换素材、改时间和轨道重建。无base-plan时mapping必须有name/canvas；选择需要构成从0开始连续的video主轨。新增图层默认接在已有layer_order末尾，Agent按全片设计调整；同轨重叠、画面空洞和超出主片长会拒绝，工具不自动填洞、重剪或替Agent决定结构。

画面选择显式填写fit、time_basis、motion_mode；替换素材path相对mapping文件解析，source_in/speed默认0/1，volume默认0。duration为新的时间轴秒数，原运动按完整片段时长同比调整。旧片段的素材、源入点、速度、音量和裁切不自动沿用。新画幅与来源不同需allow_canvas_change=true并重新检查构图；有base-plan时沿用该计划的画幅。transform可明确覆盖静态变换，运动轨迹继续由提取点决定；需要整体移位/改轨迹时直接改新计划的对应点，不假定静态位置会给关键帧自动加偏移。

时间基准必须根据来源证据选择，不能仅因某个数值落在范围内就猜测：local表示原片段局部时间；source_relative表示源区间内偏移再除原speed；source_absolute表示先减原source_in再除原speed。两种源时间转换只适用于常速关系。超出所选区间、微秒舍入后重复点、缺失/冲突关键帧及混合新旧格式均拒绝，不偷偷裁掉点。旧低四位flags按该库可观察到的位置/缩放/旋转/alpha选择转换；其他通道保留在报告。现代转换支持这些标量及UNIFORM_SCALE，未知属性拒绝。当前库flags含义来自样本对应关系，跨格式解释和最终视觉等价仍需核对。

motion_mode=linear_rebuild明确选择按参数重建线性运动，不宣称复制了原生缓动或完整模块。旧graph非空会拒绝；旧裁切、曲线变速、算法、蒙版、特效/动画资源、调色、字体及其他字段保留研究证据，不会暗中迁移。原复杂裁切与替换素材的fit不能等同；重新设计画面布局。

音频选择需提供替换path和新start/duration/track，可写source_in/speed/volume/fade_in/fade_out；不沿用旧配乐与拍点，音量自动化会拒绝。文字选择需提供重写的text和自行确定的size，支持x/y、基础color/bold/border/style与可用运动；不复制原文、字体或文字模板。文字alpha动画仍拒绝。旧flags涉及当前无明确文字alpha字段时不能猜值。

需要静态原生特效、滤镜、动画、转场、混合、色键或矩形蒙版时，从报告查看技术意图，再通过capabilities核对当前枚举和参数。画面选择可加`native`对象，字段与普通clips的对应字段完全一致；文字native只接受animations。工具按显式配置重新生成原生材料并记录，不照搬旧资源ID；非矩形蒙版等暂存项仍停用。例如查询到确切枚举后可写`"native":{"effects":[{"kind":"scene","name":"白色边框"}]}`。未配置的资源不会进入新草稿，不把名字相似视为同款，也不把写入视为下载/显示验证。

新计划只含现有schema字段，build生成新ID，避免旧关键帧悬空与重复导入轨道ID冲突；借鉴记录保存来源哈希、原时间范围、时间基准、实际新增项、遗漏与冲突。构思、必要原生重建和删改仍由Agent完成，合入后继续按全片检查衔接。

局部重建必须有真实新计划和独立回读；整体模板仍不受支持。最小回归可运行 `scripts/test_template_borrow.py`。

## 整套复用与局部重建

用户明确要求模板复用时，核对目标表达、槽位数量、画幅、素材主体和节奏是否适合原布局。普通原创任务默认自行设计并直接执行选段、精剪、声音与视觉操作；需要成熟局部技法或参数依据时可查样本，按自己的设计改造并继续统筹全片。不以“没有合适模板”为由退回固定布局或降低创作目标，也不把全片视为模板模块的连续拼接。

旧素材多使用`##_draftpath_placeholder_…_##`；按工程包根目录解析，不能把它当本机绝对路径，也不能仅因占位标记就判缺失。字体和效果可能指向旧系统缓存，按资源ID和当前剪映识别结果核对，不盲目替换全部路径或假定都失效。

复用只操作独立新副本，保留用户原工程。未实际导入、资源显示、保存重开时如实记录兼容性；不将复制目录或读回JSON称为套用成功。模板里未接入的高级功能不因复制旧结构而升级为后台重建能力。

研究方法和使用模板素材分别记录来源；选中但不可用的核心图片、文字或效果明确列出。与任务无关的原作者人物、名单、音乐、装饰和固定长度不默认继承。新增付费资源另按主Skill授权范围处理，会员授权不替代外部模板/素材使用范围核对。
