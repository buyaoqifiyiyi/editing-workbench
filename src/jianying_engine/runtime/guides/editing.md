---
name: jianying-editor
description: 剪辑工作台的剪映执行模块，根据素材、脚本或参考完成选段、声音与画面设计，交付剪映原生可修改草稿。创作判断读取planning；默认后台执行，画质增强、最终视听和导出由用户完成。
---

# 通用剪映剪辑助手

## 宿主与创作入口

本页是工作台内部持续维护的剪映执行接口，适用于用户选择剪映的新建、续接与局部修改；尚未完成的适配继续按剪映能力表和固定缺口台账推进。制作与修订先使用[创作决策层](planning.md)，再按本页的剪映操作执行。用户选择达芬奇时读[达芬奇主剪辑＋AE局部加工](../references/resolve-workflow.md)，明确指定Adobe路线时读[Adobe入口](../references/adobe-workflow.md)。剪映草稿、原生参数和用户导出规则仅适用于剪映；原素材、原工程和原接口资源保持原路径。

## 目标与分工

Agent按[创作判断流程](planning.md#创作判断流程)从实际素材形成主张、组织声画、选择镜头连接与技巧，再映射成本页操作；具体方法查[剪辑目标与方法选择](../references/general-editing.md)。遇到技术疑问时再查教程或模板。

方向分流由[创作决策层](planning.md#分流唯一维护位置)统一维护；执行与修订继续使用其判断，自行决定常规取舍，不要求用户补详细脚本或审核脚本。人物经历、台词、路径和完成状态只写有依据的内容。执行中只在三种情况询问用户：核心目标互相冲突、不可替代的素材缺失、需要会员范围外的新增付费。

- 本 Skill 负责剪辑，用户负责最终完整画面/声音检查和成片导出。结构与参数校验后的后台草稿即可交付；必要剪辑操作未完成时照实说明，不推成用户待办。
- 默认只运行后台工具，不启动、置顶或重启剪映，普通生成不依赖 `computer-use` 或 `JianyingController`。用户当前明确要求界面补做、研究或兼容测试时，才读 [高级原生界面操作](../references/native-ui-advanced.md)，完成相关操作并局部核验。
- 效果保留剪映原生图层和可调参数。普通剪映任务不外部预渲染或烘焙效果；用户明确选择多软件协作时按[镜头交接](../references/shot-exchange.md)使用外部中间素材，原生参数留在对应软件工程，不能冒充剪映原生效果。
- 补帧（含光流、帧融合）、超分/超清和技术性画质修复由用户自行完成，不研究接入，也不进计划、`jianying_tasks` 或剪辑待办。普通/曲线变速、倒放、合成、文字、声音及表达性调色仍按实际能力判断；使用处理后的素材须重新探测。
- 用户已授权会员资源：默认 `allow_vip_effects=true`，不重复确认；当前任务明确只用免费资源时才设 false。会员权限不证明下载、渲染或接口可用。
- 数据外置：原素材、计划、草稿、抽帧、项目反馈、测试日志和本机模板数据保存在 Skill 外；本 Skill 只保留通用经验、执行工具和必要能力契约。用户要求优化/维护或总结写入时才读 [维护层](maintenance.md)；普通剪辑不修改 Skill。

## 创作与修订

- 在现有 `notes` 保留[创作判断流程](planning.md#创作判断流程)产生的关键决定及未核实项；先确定选段与声画关系，再填写参数。复用已有分析，避免重复写一套创作脚本。
- 写脚本与选段顺序时，按 [从表达目标到画面连接](../references/general-editing.md#从表达目标到画面连接) 选择衔接方法，把关键起止构图、接管与停留、文字和声音写进脚本或 notes，再填原生参数。
- 画面与声音都要落实到操作。克制可以用接剪、留白或长镜头，但仍要做选段、构图与声音取舍。技术流按 [组合方法](../references/technique-combinations.md) 分配主体、背景、窗口、文字和强调层职责，有起止状态和清楚动作；只加全局滤镜不算完成。
- 文案具体、有镜头依据并前后呼应；语义、阅读排版与技术展示分别判断。用户反馈不足时读 [剪辑修订与表达判断](../references/creative-review.md)，先定位根因再修改。文案已认可而画面不足时，保留认可内容和必要阅读时间，重设分层揭示与构图变化。
- 整体优化默认从用户原素材建立新的版本计划：复用分析、来源和反馈，重新组织选段、顺序、声音与视觉，旧版只作对照，时间轴与效果不默认继承。用户指定局部修改或保留手工编辑时，以最新稿的独立副本为准，不用旧计划覆盖；读取不支持时说明未合并的部分。整体认可不自动锁定全版结构，也不等于兼容、混音或导出通过。

## 成片类型与片源分区

先确定表达目的，再判断片源支持什么，最后选择表现。动漫画风不自动等于漫剪；混合片按段落选择，只读取相关分区。

| 类型 | 按需读取 |
|---|---|
| 漫剪、节奏混剪、情绪 MV | [漫剪与节奏混剪](../references/styles/anime-mix.md) |
| Vlog、旅行、生活记录 | [Vlog](../references/styles/vlog.md) |
| 纪实、纪录片、访谈 | [纪录片与纪实](../references/styles/documentary.md) |
| 剧情解说、知识、教程 | [讲解与解说](../references/styles/explainer.md) |
| 产品、商业、品牌展示 | [商业展示](../references/styles/commercial.md) |
| 剧情叙事、相册、活动、口播 | [通用剪辑方法](../references/general-editing.md) |

片源另记录动漫/游戏/真人/静图/屏录、原始镜头或已剪成片、原有文字/音乐/抖动/切点及独立声音条件。原片自带的效果无法通过删掉本次计划的效果来消除。涉及事实时核对人物、时间、地点及出处。分类和理由简记在 notes，沿用现有 schema 与生成器。

绿幕、单色底图形、换背景或主体穿插时读 [绿幕与色度抠除](../references/green-screen.md)。需要分层补齐、渐进分屏、跨镜头主体连续或接触遮挡时读 [分层揭示](../references/layered-reveal.md)；先判断素材条件，几何窗口不能当轮廓抠像。

## 环境与能力

- 首次运行 `jianying-engine paths` 和 `jianying-engine doctor`。下文 SKILL_ROOT/skillRoot 指 paths 返回的 runtime，skillPython 指返回的 python；它们属于引擎，不是兼容 Skill 目录。读取当前安装、依赖和 active_draft_root，真实草稿根仍来自剪映配置。
- 使用独立引擎虚拟环境。基础依赖随本地包安装，音频、跟踪、MCP 按 extras 选择；不在 runtime 内创建 .venv，不改变全局 Python 或剪映版本。FFmpeg/ffprobe 仍按明确环境变量、PATH、引擎便携工具及兼容入口规则定位；显式失效报错。
- 迁移或 doctor 有缺口时读 [迁移适配](../references/migration.md)，按自动发现和明确映射另存计划；路径不猜，全局设置与手工/加密工程保持不动。ready/publication_ready 只是前置检查，不替代界面兼容或首页登记。
- 生产选型统一查 [能力状态](../references/capabilities.md)，字段与单位查 [计划格式](../references/plan-format.md)。按已准入子集调用；暂存项在普通任务中不试跑、不写计划或待办、不下载算法资源、不转前台补做。用户新提出相应研究、验证或复现时，才按 [逐项接入](../references/progressive-integration.md) 处理。
- 素材存在、资源枚举、旧 UI 工程、保存重开或教程学习，都不证明后台能重建。必要效果未接入时，先用满足表达的已准入设计；不能等价时写明缺口。资源 ID 和扩展字段只用查询或契约给出的值；`pending` 不等于执行成功。

## 执行流程

1. 在工作区建立独立任务/版本目录，探测素材并实际看选用画面。`inspect`/`reference` 只提供候选、抽帧和音轨；语义、OCR、听感和拍点由 Agent 另行判断。初始抽帧量要有限，对重点事件加采样。
2. 按目标自主构思并精剪，写 `edit-plan.json`：源入出点、时间轴秒数、声音、字幕、构图、多轨、关键帧与原生参数。有脚本时按语义匹配镜头；有参考时借用观看关系与方法；两者冲突时明确取舍。锁定选段前按 [镜头使用与去重](../references/general-editing.md#镜头使用与去重) 核对源范围和实际画面，修改后再核对受影响片段。没有脚本不阻塞执行。
3. 音乐主导时，先确定 BGM 源段和乐句，再锁定高潮与镜头顺序，读 [节拍卡点](../references/beat-marking.md)；原声与 BGM 的取舍读 [声音策略](../references/styles/anime-mix.md#bgm-与原声并置)。自动点、波形和峰值只是候选或技术结果；试听前不宣称精准卡点或混音合格。
4. 特效、滤镜、动画、混合、色键、蒙版先查 [原生效果](../references/native-effects.md) 和 `capabilities` 的实际枚举/参数。需要方法原理时按问题查 [教学索引](../references/tutorial-library.md)；AE 做法、外部烘焙和旧参数不照搬。
5. 明确要复用模板、或设计需要工程参数样本时，读 [模板方法](../references/template-library.md)，按当前外部索引和机器准入检索、拆解、局部重建。过期、未测、失败或时间基准不明的片段不进入执行。模板缺失不妨碍原创；整片时间轴不复制。
6. 用户授权补素材时读 [在线素材接入](../references/stock-sources.md)：下载到任务目录再探测，来源 sidecar 随素材保留；build 核对哈希并登记实际来源。CC BY 署名还要写进成片或发布说明。用户限定自有素材时，只用自有素材，不拿库存镜头冒充人物或产品。
7. 先 `validate`，修复缺文件、范围越界、同轨重叠、未知字段及主画面空洞；再 `build` 到一个不存在的新目录。黑场用明确的黑色素材。build 不覆盖素材、旧稿或全局索引。
8. 默认 `publish --draft ...` 到实际草稿根并保留旧版，然后等待、读取剪映自动首页登记。只有 `registration.status=registered` 才报告已登记；复制成功不等于首页显示。超时则核对路径：剪映只登记它运行时出现的新目录，所以剪映未运行时发布的稿用 `refresh-registration --draft ...` 触发一次真实新建（[后端与发布](../references/capabilities.md#backend)），重启剪映不是交付步骤。离线归档用 `--offline-copy --draft-root ...`。

需要让图片标记或受限单行文字标签跟随清楚且连续可见的单个目标时，先读 [离线单目标跟踪](../references/capabilities.md#offline-tracking)，使用明确首框生成轨迹，再按已准入的无变换画面子集写入可编辑位置关键帧；先查失跟与检查图，不把轨迹当作可靠隐私遮挡或剪映原生跟踪。

需要命令参数时运行 `scripts/editor.py --help`。可选的局部静帧核验读 [原生后台静帧预览](../references/native-preview.md)；它只验证指定时刻的低分辨率画面，不替代用户最终检查，也不证明算法计算、连续播放或导出。测试输出放在 Skill 外，结束时清理本次临时媒体/草稿，留最小外部证据。

## 交付与验收

交付一份完整的 `剪辑交付.md`，链接草稿、计划、字幕及来源；说明完成状态、主要创作操作与取舍、实际借鉴及改造、剪辑缺口和用户最终检查重点。只报告有真实落盘依据的操作，按已完成阶段说明进度，不编百分比。

后台验收覆盖素材探测、计划校验、结构与原生参数回读。实际打开、资源显示、试听、播放、保存重开和导出，按本项目自己的状态分别记录，不继承研究工程的结论。界面补做后按其记录更新交付；基础 `verification.json` 不证明补做后的工程，补做后也不重新 build 覆盖。

独立音频降噪与受限响度是已测原生参数子集，按 [能力状态](../references/capabilities.md#denoise) 核对格式、源范围、模型身份与真峰值余量；它们不是后台 PCM 导出或离线算法任务接口。试听前不宣称声音质量；未导出也不降低已完成的剪辑状态。后端来源与兼容边界见 [后端与发布](../references/capabilities.md#backend)。

人声与伴奏要分轨时，用 [离线人声分离](../references/capabilities.md#separation) 的 `scripts/separate.py` 拆出 `vocals` 与 `accompaniment`（要自备 htdemucs ONNX 权重与 onnxruntime，模型不随 Skill 分发），再用 [离线自动闪避](../references/capabilities.md#ducking) 的脚本从人声求包络、写回 BGM 轨的音量关键帧；没有模型就按已知语音区间手工给包络，不假装自动分离。

字幕要有实际语音依据时，用 [离线转写](../references/capabilities.md#transcription) 的 `scripts/transcribe.py` 从素材音频拿逐段时间文本与 `字幕.srt`（要自备 faster-whisper 模型，不需要 torch，不联网、不要宿主 token），需要入计划时使用 `scripts/captions.py`，按所选片段的截取、线性变速和时间轴位置生成 captions 并校验新计划；来源与拒绝边界见 [转写结果导入剪辑计划](../references/capabilities.md#转写结果导入剪辑计划)。它是模型原样输出、段级时间，**不是剪映官方智能字幕**：不人工改写文本、不补标点、不编造更细的时间，也不据此声称逐词样式；没有模型也没有用户文稿时，字幕时间标为暂定。

用户已提供或校正 SRT 时，复用 `captions` 操作的 `--srt` 与显式 `--timebase source|timeline` 导入纯文本字幕，按所选片段生成新计划；不要将人工字幕伪装成模型原样转写。支持边界见 [外部 SRT 导入](../references/capabilities.md#外部或人工校正-srt-导入)。

需要时间倒放时，先按 [离线短片段倒放](../references/capabilities.md#离线短片段倒放) 用reverse生成新素材，显式选择声音倒放或静音，再作为普通片段进入新计划。源素材重新编码且原片保留，不能以负speed或空reverse_path冒充；原计划字幕/效果时间不自动镜像。

同一来源被切成多个固定速度片段时，可用captions的 `--index 0 2 3` 一次映射转写或源时间SRT；最终时间SRT显式timeline。选择范围、重排/重复、静音片段与整批拒绝规则见 [多片段字幕映射](../references/capabilities.md#多片段字幕映射)。不要靠逐句猜时间、自动裁半句或逐段覆盖同一计划。

需要模型词级时间时，显式transcribe --word-timestamps，再用captions --level word生成独立可编辑词片段；用户文稿强制对齐和同句重点词样式仍是其他能力。不可按字数平摊或补造零时长词。数据、来源哈希与阅读限制见 [模型词级时间](../references/capabilities.md#模型词级时间与独立文字段)。

需要独立降噪音频文件时，先读 [离线FFT降噪](../references/capabilities.md#offline-denoise)，使用denoise-audio明确噪底/降噪量并比较dry/denoised。它与原生audio[].denoise不同；替代视频原声须静音原声、保留源入点，不能从处理成功推断听感改善。

需要矩形柔边时，可按 [矩形RGB覆盖率羽化](../references/capabilities.md#mask-red-coverage) 显式选择red_coverage_v1受限模式，使用正式validate/build/publish；不改共享缓存、不调用研究稿后改JSON。交付明确“受限接入”与当前工程/UI保存重开/导出验证范围。

需要绿/蓝幕去底时，先查 [安装内Chroma2](../references/capabilities.md#installed-chroma2)；符合子集再显式选installed_chroma2_v1。build绑定已核安装资源，发布/迁移必须保留运行依赖；不复用研究稿后改JSON。

需要滤色光效、乘法/加深、叠加/强光/柔光塑形或变亮/变暗逐通道混合时，按 [安装内混合模式](../references/capabilities.md#installed-mix) 使用显式对象；新增六种仅用于PNG叠层，不将视频研究稿当作正式入口。不要沿用旧mix_mode字符串或把其他枚举存在当作已接入。发布保留安装资源依赖，交付分别说明后台与UI证据。

需要同句内突出关键词时，按 [重点词着色](../references/capabilities.md#text-highlights) 给出原文字符start/end与color；保留原字幕及时间，不凭文本猜词级时间。整句不能含超出子集的字符，不自动拆emoji或改写原文绕过。

需要使用用户自备字体时，按 [本地静态字体](../references/capabilities.md#local-font) 在caption写`font_path`，与枚举`font`二选一。只接受静态TTF/OTF并复制进草稿；当前仅静态横排文字，不叠加动画、运动、重点词、透明度、弯曲、发光、竖排或复合。先核对字体嵌入/商用许可，再由实际项目检查字形覆盖和排版。

需要可编辑文字淡入淡出时，按 [GlobalAlpha受限子集](../references/capabilities.md#text-global-alpha) 写文字局部alpha键；需要先出现、保持阅读、再退出时用四点0→1→1→0，保留相应单行/默认字体约束；不凭视频alpha枚举或静态style.alpha存在推定任意组合可用。

需要重点词着色字幕整体淡入淡出时，按 [多色透明度组合](../references/capabilities.md#text-color-fade) 同时写范围和两点或受限四点alpha；保留一条可编辑字幕，不拆成多层近似。检查实际原生中间状态与同值静态控制，不借用单项通过替代组合验收。

需要角点拉入/复位时，按 [角点透视](../references/capabilities.md#perspective) 选择已测的局部坐标与PNG文字组；保留原生组与二维关键帧。新三角点不混用、多点/任意位置/视频仍拒绝；分别记录静态参数、动态中间态与UI/导出证据。

需要音乐卡点时，按 [离线节拍与切镜](../references/capabilities.md#beat-editing) 明确音频源段，先取得候选，再显式选每N拍密度生成新计划；不把候选当强拍或听觉校准结果。若当前MCP拒绝新operation而本机CLI已列出，使用正式CLI继续并如实说明该连接未重载，不猜不存在的MCP参数。

需要两行重点词字幕时，按 [重点词着色](../references/capabilities.md#text-highlights) 保留原文中的一个LF，每行分别指定不含LF的范围；保持静态白基色/非粗体，不自动改写、拆成多条或叠加透明度键。检查换行位置、两行字形与局部颜色，不用单行通过替代多行画面验收。

需要绿色局部色域明暗调整时，可按 [调色主表](../references/capabilities.md#grading) 使用green_hsl.lightness受限静图子集；黄色/青色同样可能变化，不能称为只作用语义绿物体。原生拍点字段存在只算研究线索，未完成编辑器显示/编辑保存与时钟验证前，不把离线候选写成已准入原生标记。

需要橙/黄/青/紫/紫红色域去饱和、增强、调整明度或偏移色相时，先按 [调色主表](../references/capabilities.md#grading) 核单PNG子集，再写对应HSL字段。每次只处理一个通道，饱和度与明度可同时调整但保持各自已测离散值；色相仍独立、明度/色相各仅−30/0/+30，查看邻近颜色和灰阶；不直接写原生通道号或把未测的视频/多通道组合放进生产计划。


需要“整句留在画面上，随词变色”时，使用 [动态逐词高亮字幕](../references/capabilities.md#karaoke)：已有模型词时间用captions --level word --karaoke-mode active；人工词/音节SRT用显式source或timeline时钟，选择active或cumulative。先核对输入时间，保留原文本，长句按可读显示组拆分。生成的连续状态仍为原生可编辑文字，多状态统一改句应重生成；不拿单独显示一个词、文字图片或按字数平摊时间充当逐词高亮。
