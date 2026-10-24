# 剪映本地执行引擎

AE新增明确轮廓的可编辑前景遮罩接口：`clips[].foreground_mask`用3–32个源像素顶点组成严格凸多边形，支持0–100源像素羽化与布尔反转；复制源层继承变换/运动，静音置于文字前方。有遮罩时源层透明度须恒为1，自动人物分割/跟踪仍未准入。保存前与重开后逐项回读层序、源时钟、音频和遮罩参数，实际原生效果按包外验收记录分别确认。字段与边界见[AE前景多边形遮罩](src/jianying_engine/runtime/references/adobe-workflow.md#可编辑前景多边形遮罩)。

AE文字层新增可编辑位置、缩放与透明度线性动画，跨镜头按同一条文字时钟连续求值；AEP保存重开后逐项核对文字和关键帧参数，再经现有镜头回填链进入所选主剪辑时间轴。字段与边界见[AE动态文字](src/jianying_engine/runtime/references/adobe-workflow.md#可编辑整层动态文字)。

达芬奇接入新增纯文字原生字幕与AE镜头返回：`captions`生成可编辑SRT轨并烧录导出，`resolve-workflow return-ae`校验已完成AE包后，只替换主计划中的选定镜头，保留主配乐、字幕和其他片段，避免重复施加增益/变换。当前只覆盖[达芬奇指南](src/jianying_engine/runtime/references/resolve-workflow.md)列明的纯字幕与[AE有界子集](src/jianying_engine/runtime/references/adobe-workflow.md)，不改变当轮软件选择。

指定BGM的声画匹配新增`music-edit`：明确源选区准备、可配置的OpenAI兼容或Gemini原生音视频分析、音乐分段和镜头动作对拍、30fps剪映/Resolve计划编译与既有校验。协议、模型和端点由用户配置；仅显式`analyze`调用所选服务并产生API用量，未配置时不调用。模型创作与卡点质量需要真实素材验证，不能由接线测试代替。入口和完整边界见[原生视听与音乐编排](src/jianying_engine/runtime/references/music-edit.md)。

工作台以制作任务组织界面：素材监看、任务单、阶段进度与成果集中在制作区，历史交互放入执行记录；软件配置为独立模块。界面继续使用原本机引擎与会话数据，不迁移已有项目或把记录清空。

工作台支持在“软件后端”面板按剪辑、特效与合成、调色、音频搭配软件，也可单独调用一个后端。配置随会话保存，保存配置不启动软件或模型任务；旧会话保持原工程软件，新会话沿用保存的初值。具体使用和数据保留见[多软件配置](docs/agents.md#多软件配置与剪映数据保留)。Agent统一Skill为`editing-workbench`（剪辑工作台）；现有引擎命令、剪映原始数据与资源路径继续兼容。

软件面板自动检测本机安装与基础接入条件，未安装、版本不兼容或未开放适配的选项置灰并显示原因；最多每10秒随轮询更新，可手动重新检测。保存和发送前再次核对，卸载软件不会删除旧会话选择或自动切换后端。安装检测不代表授权、宿主连接及实际任务验收。

软件按产品功能放入各分类，并单独维护执行准入：达芬奇、PR展示剪辑/特效/调色/音频；AE列特效与合成；剪映仅列剪辑。当前达芬奇仅剪辑已接入，调色等分类灰显“尚未适配”；PR各项暂不可用。界面分类不会开放未验证能力，也不删除剪映原有内部参数接口。

剪映与达芬奇都是工作台内部持续维护的后端。用户选择剪映可新建或续接项目，未完成适配继续沿用原主表与缺口台账。统一Skill内的[剪映模块](adapters/skills/editing-workbench/modules/jianying/GUIDE.md)和[达芬奇模块](adapters/skills/editing-workbench/modules/resolve/GUIDE.md)分别调用原接口；安装专属的剪映资源、原环境与配置已实体整合到editing-workbench/backends/jianying，Skills顶层不再保留独立jianying-editor。项目素材/工程和正式引擎源码不移动，环境启动器与配置读取按内部新路径保持一致。

创作判断统一由[决策指南](src/jianying_engine/runtime/guides/planning.md)贯穿选段、声画组织、镜头连接、技法选择与修订。工作台所有后端先进入该指南，动漫/游戏情绪混剪与MV按[镜头与音乐方法](src/jianying_engine/runtime/references/styles/anime-mix.md#镜头与音乐如何共同推进)展开；具体操作仍受当前后端准入约束。流程可达与成片审美分别验收，后者需同素材短段和真实观看反馈。

软件适配范围仅剪映、PR、AE、达芬奇。首次新会话初值为达芬奇剪辑＋AE特效，可由用户重新搭配。达芬奇当前接入顺序剪切、原声/配乐及静态变换，见[达芬奇范围](src/jianying_engine/runtime/references/resolve-workflow.md)；AE可[独立制作](src/jianying_engine/runtime/references/adobe-workflow.md#独立ae调用)。PR计划继续适配，当前未开放选择。降噪、转写等音频功能保留为工作台内部工具。软件配置不扩大各后端能力，也不代表任意软件间工程或特效可无损互通。

旧镜头交接工具与验收资料保留用于历史兼容，见[镜头交接](src/jianying_engine/runtime/references/shot-exchange.md)。Blender、Kdenlive不在当前软件适配计划中，不在目录、选择器或自动检测中展示；已有作品不自动迁移。

成果面板按项目交付清单识别正式交付，完整草稿按一项展示；计划等项目资料、抽帧/预览/日志等过程文件单独折叠，徽标只计正式交付，不删除原文件。项目根`workbench-delivery.json`登记最终草稿目录或文件，格式见[连接说明](docs/agents.md#成果与本机引用)。

旧Codex会话被其他进程占用时，工作台会保留历史并在当前工作台会话中分支继续；不接管占用者。恢复失败不会将未提交的要求追加成已发送消息。

工作台按用户授权采用全程免审批模式：Codex新建、恢复、分支及每回合均使用never审批策略与本机完全访问，剪辑任务无需反复确认；其他已知协议请求在后台一次性处理。此配置仅作用工作台自有Agent进程，不修改Codex桌面或全局配置。仍按真实计划展示阶段进度，执行流水默认折叠；Agent只负责剪辑，Obsidian交接由后台单独写入。见[连接说明](docs/agents.md#权限与中断)。

0.6.3吸收兼容的本地字体资源思路：`captions[].font_path`支持静态TTF/OTF，构建时解析字体并复制到草稿`Resources/local-fonts/`，publish重绑定，迁移同步字体路径。已在Windows剪映11.6完成参数回读、离线发布、删除原字体后的后台原生静帧；UI保存重开和导出仍未验证。

0.6.2修复删除最后一项素材后的空列表渲染报错；打开工作台后自动连接上次成功选择的Agent，连接失败显示原因并保留手动重连，不自动发送任务或反复重试。

0.6.1修复素材选择后无可见反馈：添加成功自动展开素材面板并选中新项，输入区持续显示素材名称/数量；重复选择提示已存在，取消和错误均给出反馈。文件夹仍只登记路径，不预先展开或上传内容。

0.6已接通Agent会话设置与恢复，成果完成后自动发现并可在工作台预览。使用[Agent接入说明](docs/agents.md)和[桌面说明](docs/desktop.md)查看实际支持范围。

0.5.1素材入口按本机引用工作：文件夹只登记绝对路径，Agent按需列目录和读取，不预先展开全部文件；单个文件可预览。取消原64项素材数量限制，多项文件引用分批显示。

源码实际位置：`C:\Users\MECHREVO\Desktop\剪辑工作台源码`。本机独立环境以 editable 方式关联此目录，日后修改直接在这里进行。旧工作区的 `engine/` 是指向这里的 Junction 兼容入口，不是第二份源码。

维护任务清单、备份与验收证据仍在 `C:\Users\MECHREVO\Documents\deepseek-harness\default-workspace\jianying-workspace` 的 `任务清单.md`、`Skill维护层` 与 `运行数据`。维护前读取任务清单并备份，修改后按影响验证并更新该处台账。项目素材和账号数据不放进源码目录。

工作台0.5更新为OpenDesign式白底、大输入区和亮色强调界面；输入框内可切换Agent，右上可展开素材与成果面板。

能力与执行留在本机，外部 Agent 负责理解需求、规划和调用。无需模型也能运行素材探测、计划校验、按计划生成草稿等固定功能；转写、分离与跟踪使用可选本地依赖。程序不自带模型、素材、项目、账号或凭据。

## 安装与命令

当前已验证 Windows + Python 3.12。先在包目录之外创建独立虚拟环境，再从本项目安装：

```powershell
$engineVenv = Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv'
python -m venv $engineVenv
$enginePython = Join-Path $engineVenv 'Scripts\python.exe'
& $enginePython -m pip install '.[all]'
& $enginePython -m jianying_engine doctor
& $enginePython -m jianying_engine --help
```

基础安装不含音频推理、节拍分析、跟踪、MCP 或工作台；可选择 `[audio]`、`[rhythm]`、`[tracking]`、`[mcp]`、`[workbench]`、`[desktop]` 或 `[all]`。`[rhythm]`提供离线librosa节拍候选，`[all]`同时包含该依赖和桌面窗口；只需要桌面界面可选`[desktop]`。OpenCV 包只安装一种，模型单独保存在项目/模型目录并显式传入。维护源码采用 editable 安装并显式选择所需extras；发行使用 wheel，不复制虚拟环境。

```powershell
& $enginePython -m jianying_engine validate --plan plan.json
& $enginePython -m jianying_engine build --plan plan.json --out new-draft
& $enginePython -m jianying_engine transcribe --help
& $enginePython -m jianying_engine tracking --help
& $enginePython -m jianying_engine beats --input music.wav --start 0 --duration 16 --out beat-candidates
& $enginePython -m jianying_engine beat-apply --plan plan.json --beats beat-candidates --every 4 --out beat-plan.json
& $enginePython -m jianying_engine guide editing
& $enginePython -m jianying_engine paths
```

所有命令均由白名单分发到同一套执行逻辑，不拼接 shell。参数、返回码和 JSON 结果保留现有契约；输出拒绝覆盖、来源/准入验证和证据层级不因封装放宽。发布与登记仍是单独动作。

激活该虚拟环境后也可直接用 `jianying-engine`。未激活时使用上面的绝对解释器，不要求修改全局 PATH。

## 外部 Agent

[多Agent连接](docs/agents.md)：Codex保留专用app-server，DSH/OpenCode/Gemini/Claude适配器与自定义入口使用ACP。当前本机找到Codex、DSH与隔离安装的OpenCode。0.6根据ACP真实会话回执展示和设置模型、推理强度与工作模式，并恢复已确认选择；未声明的配置使用Agent自身设置。Gemini/Claude仍未安装、未验证，各Agent独立登录。

[Windows桌面版](docs/desktop.md)继续使用.NET启动器和pywebview/WebView2，桌面与开始菜单快捷方式已部署，工作区为`D:\剪辑项目\工作台项目`；[网页入口](docs/workbench.md)保留。左下「连接设置」可选择、扫描或指定Agent入口，不需要更换剪辑执行引擎。

```powershell
& $enginePython -m jianying_engine workbench --desktop --workspace 'D:\剪辑项目\工作台项目'
# 若只使用网页，先退出桌面服务，再选择下列替代命令：
# & $enginePython -m jianying_engine workbench --workspace 'D:\剪辑项目\工作台项目' --open
```

上面两种启动方式二选一，工作目录须已存在；同一状态目录不能同时运行网页与桌面实例。会话、`agents.json`与日志外置于`%LOCALAPPDATA%\JianyingEngine\workbench`。网页关标签不停止后台；桌面关窗按忙时确认/闲置退出处理，重复启动唤醒已有桌面窗口。会话绑定Agent和启动命令指纹，不跨Agent重放；各协议权限与恢复边界见连接说明。

0.6验收：75项Python测试、11组前端控制器模拟通过；DSH/OpenCode真实配置与空会话跨进程恢复通过。确定性测试Agent通过真实ACP和MCP完成计划校验、1秒合成草稿生成、成果自动发现和文档预览，无模型调用。该测试不证明真实模型剪辑、剪映UI保存重开、完整视听或导出；本轮新增界面未完成实机视觉验收。

支持命令行调用与可选 stdio MCP；见 [MCP 接入](docs/mcp.md)。MCP是本机调用接口，不监听HTTP、不在本机运行大模型；仅显式`music-edit analyze`按专用配置发送已选音视频给当前选择的模型。外部云Agent接收到的文本/画面也可能由该客户端发送给模型，调用方需控制发送范围。

规划、剪辑与维护知识在 `jianying-engine guide` 中按需读取。知识指导 Agent；可执行约束由引擎强制。UI 保存重开、完整视听和导出仍按项目各自验收。

## 维护与边界

`src/jianying_engine/runtime` 是执行代码与必要资源的唯一源码。旧 Skill 路径保留兼容入口，不再分别维护三套执行文件。运行数据、模型、日志、凭据与逐项目准入记录放在包外。

核心依赖与剪映版本边界沿用既有检查。新包装不会使剪映私有 token 接口、原生新抠像或未准入效果自动可用。当前项目为私有本地工具，未另行授予公开分发许可证；第三方依赖遵循各自许可证。





