# Agent 剪辑引擎

在 Codex、DSH 或其他已配置本机引擎的 Agent 对话里，使用 **Agent 剪辑**（`$editing-workbench`），提供素材路径、目标和要用的剪辑软件。Agent 读取创作指南、检索已学方法，直接通过 [MCP](docs/mcp.md) 或命令行执行，把计划、草稿和验收结果交付在项目目录。

续接老项目时提供原项目路径，Agent 从版本计划、素材引用和草稿恢复项目上下文。独立桌面与网页登录口已退役，旧会话状态仍在 `%LOCALAPPDATA%\JianyingEngine\workbench`。[历史工作台资料](docs/desktop.md)用于查阅旧版。

AE新增明确轮廓的可编辑前景遮罩接口：`clips[].foreground_mask`用3–32个源像素顶点组成严格凸多边形，支持0–100源像素羽化与布尔反转；复制源层继承变换/运动，静音置于文字前方。有遮罩时源层透明度须恒为1，自动人物分割/跟踪仍未准入。保存前与重开后逐项回读层序、源时钟、音频和遮罩参数，实际原生效果按包外验收记录分别确认。字段与边界见[AE前景多边形遮罩](src/jianying_engine/runtime/references/adobe-workflow.md#可编辑前景多边形遮罩)。

AE文字层新增可编辑位置、缩放与透明度线性动画，跨镜头按同一条文字时钟连续求值；AEP保存重开后逐项核对文字和关键帧参数，再经现有镜头回填链进入所选主剪辑时间轴。字段与边界见[AE动态文字](src/jianying_engine/runtime/references/adobe-workflow.md#可编辑整层动态文字)。

达芬奇接入新增纯文字原生字幕与AE镜头返回：`captions`生成可编辑SRT轨并烧录导出，`resolve-workflow return-ae`校验已完成AE包后，只替换主计划中的选定镜头，保留主配乐、字幕和其他片段，避免重复施加增益/变换。当前只覆盖[达芬奇指南](src/jianying_engine/runtime/references/resolve-workflow.md)列明的纯字幕与[AE有界子集](src/jianying_engine/runtime/references/adobe-workflow.md)，不改变当轮软件选择。

指定BGM的声画匹配新增`music-edit`：明确源选区准备、可配置的OpenAI兼容或Gemini原生音视频分析、音乐分段和镜头动作对拍、30fps剪映/Resolve计划编译与既有校验。协议、模型和端点由用户配置；仅显式`analyze`调用所选服务并产生API用量，未配置时不调用。模型创作与卡点质量需要真实素材验证，不能由接线测试代替。入口和完整边界见[原生视听与音乐编排](src/jianying_engine/runtime/references/music-edit.md)。

剪映与达芬奇持续作为适配后端。Agent 可按本轮要求组合或单独调用软件，续接时从工程确认当前宿主。[剪映模块](adapters/skills/editing-workbench/modules/jianying/GUIDE.md)和[达芬奇模块](adapters/skills/editing-workbench/modules/resolve/GUIDE.md)分别调用原接口；剪映未完成适配继续沿用原主表与缺口台账。安装专属的剪映资源、兼容环境与配置仍位于 Skill 的 `backends/jianying/`。

创作判断统一由[决策指南](src/jianying_engine/runtime/guides/planning.md)贯穿选段、声画组织、镜头连接、技法选择与修订。Agent 先读该指南，动漫/游戏情绪混剪与 MV 按[镜头与音乐方法](src/jianying_engine/runtime/references/styles/anime-mix.md#镜头与音乐如何共同推进)展开；具体操作仍受当前后端准入约束。流程可达与成片审美分别验收，后者需同素材短段和真实观看反馈。

[剪辑创作知识库](src/jianying_engine/runtime/references/creative-knowledge.md)整合剧情、镜头、声音、模板、工具与来源边界；可用 `jianying-engine knowledge "具体问题"` 或新鲜MCP的 `search_knowledge` 检索打包指南，再通过 `guide` / `read_guide` 阅读原文。检索只在本地进行，不访问项目媒体或网络；词语检索可能漏召回，不代表创作质量已提升。

参考片分析保留FFmpeg默认入口，另可显式选择PySceneDetect候选切点；`practice-compare`将两段明确选区及单侧可选声音生成带来源哈希的并排审片。安装前提、参数和实际视听边界见[参考片分析与实操对照](src/jianying_engine/runtime/references/creative-knowledge-workflows.md#参考片分析与实操对照)。这些本地工具不自动理解画面、比较同源或评判审美。

Agent 按[共同决策规则](src/jianying_engine/runtime/guides/planning.md#每版同时设计剧情与剪辑)检索当前剧情和剪辑问题，在每版实际片段中落实内容推进与视听表达，并分别汇报交付和验证结果。

软件适配范围为剪映、PR、AE、达芬奇。达芬奇的剪辑范围见[达芬奇指南](src/jianying_engine/runtime/references/resolve-workflow.md)，AE支持[独立制作](src/jianying_engine/runtime/references/adobe-workflow.md#独立ae调用)。PR按当前适配状态处理。降噪、转写等音频功能可独立调用。

旧工程中的交接资料与成果清单可继续用作定位依据。见[镜头交接](src/jianying_engine/runtime/references/shot-exchange.md)和[旧成果清单格式](docs/agents.md#成果与本机引用)。

0.6.3吸收兼容的本地字体资源思路：`captions[].font_path`支持静态TTF/OTF，构建时解析字体并复制到草稿`Resources/local-fonts/`，publish重绑定，迁移同步字体路径。已在Windows剪映11.6完成参数回读、离线发布、删除原字体后的后台原生静帧；UI保存重开和导出仍未验证。

能力与执行留在本机，外部 Agent 负责理解需求、规划和调用。无需模型也能运行素材探测、计划校验、按计划生成草稿等固定功能；转写、分离与跟踪使用可选本地依赖。程序不自带模型、素材、项目、账号或凭据。

## 安装与命令

使用 Windows 和 64 位 Python 3.12。把 `ffmpeg` 和 `ffprobe` 加入 PATH。需要原生工程时，按所选后端指南准备对应软件。以下命令安装引擎与 MCP，并验证规划知识可读取。

```powershell
git clone https://github.com/buyaoqifiyiyi/editing-workbench.git
Set-Location editing-workbench
$engineVenv = Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv'
py -3.12 -m venv $engineVenv
$enginePython = Join-Path $engineVenv 'Scripts\python.exe'
& $enginePython -m pip install '.[mcp]'
& $enginePython -m jianying_engine doctor
& $enginePython -m jianying_engine knowledge '声音桥与镜头连接'
```

Agent 可按需选择 `[audio]`、`[rhythm]`、`[tracking]`、`[exchange]`、`[mcp]` 或 `[all]`。`[all]` 包含这些引擎依赖，`[rhythm]` 提供离线 librosa 节拍候选。旧工作台的 `[workbench]` 和 `[desktop]` 仅供显式恢复旧版。OpenCV 包只安装一种，模型单独保存在项目或模型目录并显式传入。维护源码使用 editable 安装，发行使用 wheel。

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

## Agent 调用

将 [editing-workbench Skill 目录](adapters/skills/editing-workbench) 整个复制到宿主的 Skills 目录，保留其 `SKILL.md`、`agents/` 与 `modules/` 结构。例如 Codex 可使用 `$HOME\.agents\skills\editing-workbench`，DSH 取其已配置的 Skills 目录。升级已有副本时先备份，再更新仓库里提供的文件，保留本机已有的后端资源。宿主支持 MCP 时按 [MCP 接入](docs/mcp.md) 注册 `jianying-engine`；宿主有终端权限时也可用 Skill 中的 CLI 路径。

安装完成后可直接让 Agent 使用 `$editing-workbench`，给出素材路径、交付目标和所选宿主。换电脑时，能力准入按该设备的安装版本重新检查。

支持命令行调用与可选 stdio MCP；见 [MCP 接入](docs/mcp.md)。MCP是本机调用接口，不监听HTTP、不在本机运行大模型；显式`music-edit analyze`与`music-edit review`均按专用配置发送已选音视频给当前选择的模型，并可能产生用量。外部云Agent接收到的文本/画面也可能由该客户端发送给模型，调用方需控制发送范围。

规划、剪辑与维护知识在 `jianying-engine guide` 中按需读取。知识指导 Agent；可执行约束由引擎强制。UI 保存重开、完整视听和导出仍按项目各自验收。

## 维护与边界

`src/jianying_engine/runtime` 是执行代码与必要资源的唯一源码。旧 Skill 路径保留兼容入口，不再分别维护三套执行文件。运行数据、模型、日志、凭据与逐项目准入记录放在包外。

剪映版本与原生能力边界以[能力主表](src/jianying_engine/runtime/references/capabilities.md)为准。源码供现有授权范围内的部署与使用，第三方依赖遵循各自许可证。





