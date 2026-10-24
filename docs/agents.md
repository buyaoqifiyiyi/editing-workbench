# 多 Agent 连接（0.6）

原生视听增量：指定BGM匹配镜头/卡点使用引擎`music-edit`，在专用配置就绪时由引擎直接将选定音视频发给当前选择的OpenAI兼容或Gemini原生模型，普通Agent统筹选区与执行。它不依赖ACP图像prompt；云调用只发生在显式analyze，配置缺失明确报告，不伪装完成。具体配置、有限范围、动作对拍和证据见[原生视听与音乐编排](../src/jianying_engine/runtime/references/music-edit.md)。

工作台顶部“视听 API”提供协议、模型 ID、接口根地址、密码型API Key、高级请求参数、启用开关、保留/清除密钥和本机配置检查。没有内置供应商或模型默认值；旧配置保留密钥并要求重新选择模型。密钥以Windows当前用户DPAPI单独加密保存，状态不返回明文；引擎下次调用直接读取，无需重启。保存与检查不发模型请求，不表示真实API连通性或余额已通过。

0.6已接通真实会话配置：连接ACP Agent后自动创建或恢复空会话、读取模型/推理/模式设置，配置需经Agent回执确认，准备会话本身不发模型prompt。DSH和OpenCode均已实测跨进程恢复。

工作台使用两种协议：Codex保留专用app-server适配器，其他Agent通过ACP连接。Agent负责模型调用和自身工具，引擎继续负责本机剪辑执行；添加一个目录选项不代表其已安装或通过验证。

## 当前目录与验证边界

| 选项 | 启动方式 | 本机状态 |
|---|---|---|
| Codex | `codex.exe app-server` | 已找到；沿用已验证的连接和实时模型目录 |
| DeepSeek Harness | 官方桌面CLI的Electron Node模式，`--profile acp` | 已找到；真实initialize与session/new通过，未发送prompt |
| OpenCode | `opencode.exe acp` | 1.19.3隔离安装于`%LOCALAPPDATA%\JianyingEngine\agents`；真实initialize与session/new通过，未发送prompt |
| Gemini CLI | `gemini --acp` | 未安装、未验证，需用户提供可运行入口 |
| Claude Code · ACP适配器 | `claude-agent-acp`，或Node执行适配器脚本 | 未安装、未验证；普通Claude CLI不等于此适配器 |
| 自定义ACP Agent | 用户指定exe和参数数组 | 只作为显式配置入口，连接后才验证协议 |

OpenCode隔离安装没有修改全局PATH，也没有复制其他Agent的账号。各Agent使用自己的登录、提供商和模型授权；工作台不代登录、不读取凭据文件、不把一个Agent的认证自动转给另一个。

## 在界面里连接

1. 打开左下角「连接设置」，选择Agent；「扫描」只刷新本机入口发现。
2. 已找到的入口可直接连接。未找到时填写可执行文件完整路径及JSON参数数组，例如OpenCode参数为`["acp"]`。
3. Node脚本请选择`node.exe`，参数第一个元素写脚本完整路径，例如`["C:\\tools\\agent\\dist\\index.js"]`。不执行`.cmd`、批处理或拼接shell命令。
4. 连接成功后核对Agent名称与状态，再新建会话、输入要求。启动参数只用于程序入口，不要在这里填写API密钥。

更换Agent会清空旧模型/强度选择；即使两个Agent返回相同模型ID，也不会沿用上一Agent的选项。Codex仍展示CLI实时模型/推理目录。ACP展示当前会话实际声明的模型、推理强度和工作模式；未声明的选项保持禁用，使用Agent当前设置。设置失败会显示脱敏原因并恢复已确认的值，未确认时不启动prompt。连接只读取会话设置，点击发送才创建模型回合。改变目录、新建或选择对应Agent会话时会读取该会话设置。

成功连接后，所选Agent与启动命令保存到外置状态目录的`agents.json`；失败连接不覆盖已保存选择。此文件不保存账号或凭据。默认位置为`%LOCALAPPDATA%\JianyingEngine\workbench`，也可由`--state-dir`指定。

## 会话绑定与恢复

Codex采用跨进程单写入者保护。若恢复明确返回`thread ... already has an active writer`，工作台通过协议`thread/fork`继承历史，在同一工作台会话中继续，并保存`continued_from`；原上下文和占用进程保留。仅该明确冲突触发此恢复，其他RPC失败仍报错且不自动发送或重试模型任务。历史由Codex分支继承，验证应比较`thread/read`的有效历史，不能将分支本地SQLite零条新记录误判为历史丢失。

每个会话绑定Agent ID及协议/启动命令指纹。切换Agent或命令后不会把旧会话文本自动重放给新Agent；请使用对应Agent会话或新建会话。

0.4只检查旧loadSession字段，因此曾错误禁用DSH恢复。0.6改为识别现代sessionCapabilities.resume并调用session/resume，兼容旧session/load。DSH/OpenCode的空会话均实测跨进程恢复到相同会话ID，模型目录保留；恢复后首条新消息补充本机工作台约定，不重放旧任务。未声明任何恢复接口的Agent仍需新建会话。真实模型长任务上下文与复杂工具恢复尚未实测。

工作台保存已确认的模型、推理和模式，恢复后按Agent当前目录重新设置。选择后即使未发送消息就关闭，也会恢复该选择。Agent升级后不再支持旧值时，明确显示错误和当前实际设置，保留旧选择待用户重新配置；不会静默使用默认值执行任务。

## 成果与本机引用

添加文件夹只登记绝对路径，Agent按需查看内容；不会预先展开整个文件夹。单个文件可以预览，显式文件引用分批显示。

回合完成自动扫描工作目录中的草稿、文档、字幕、图片及音视频成果，不再限制三层目录或100项；跳过链接、依赖目录及已登记的素材目录。成果随会话保存，也可手动刷新。「查看」可直接预览文档与媒体，「打开目录」用于在本机管理文件。文档按纯文本展示，单份预览上限512KiB；更大的文档请在本机打开。

文件现在分为「交付成果」「项目资料」「过程文件」。只有项目根`workbench-delivery.json`登记且实际存在的项进入交付成果和徽标；未声明的草稿/文档仅作为项目资料，抽帧、预览、分析数据和日志等过程文件折叠保留。多个项目各自分组，不按mtime或文件扩展名推断最终版。完整草稿目录只形成一个条目，不将内部JSON、字幕、素材和subdraft逐项计数；打开该条目所在目录可查看完整内容，文件未移动或删除，也不表示已打包外部素材。

清单格式：`{"schema_version":1,"items":[{"path":"draft-v2","title":"正式可编辑草稿"},{"path":"剪辑交付.md","title":"交付说明"}]}`。path必须相对所属项目，拒绝绝对路径、上级路径和链接；草稿目录映射到其draft_content.json。清单不提升播放/导出验证等级，不能用引擎_skill_manifest.json的生成身份代替项目的交付决定。没有清单时不自动把普通文件认作正式成果；后台Obsidian交接只列正式交付引用。

## 权限与中断

用户已明确授权剪辑全程无权限确认。Codex在thread/start、thread/resume、thread/fork设置`approvalPolicy=never`、`sandbox=danger-full-access`，每次turn/start再次显式传入`approvalPolicy=never`与`{type:dangerFullAccess}`，覆盖旧会话原有策略。它允许工作台Agent直接以当前用户权限处理素材、输出和本机引擎依赖；只配置工作台自有进程，不改Codex桌面、全局配置、账号或剪辑引擎能力准入。

若已知命令、文件或权限请求仍到达客户端，在后台使用本次允许/本回合权限响应，不发布确认卡片、不进入awaiting_approval；审计仅记录时间、方法、请求/会话/item ID，不保存命令正文或凭据。创作问题由Agent自主决定；提问工具兜底使用提供的首选项或自主决策指引，不伪造秘密信息。外部MCP表单仍拒绝，未识别协议请求由桥接器拒绝，均不弹窗。没有可用能力的部分报告失败或限制，免审批不等于所有工具操作都能成功。

ACP权限请求在后台选择Agent提供的`allow_once`；如果只有持久允许而无一次性允许，则拒绝，不升级长期授权。其运行权限仍由Agent自身管理，client filesystem=false/terminal=false只表示不提供客户端回调。免审批不扩大剪辑职责、不授权额外付费服务；中断与自有进程回收保持原边界。用户主动选择素材/工作目录的文件选择器、忙时主动关窗的退出确认不是Agent任务审批。

## DSH本机兼容处理

使用已安装DeepSeek Harness 0.2.1-rc.2的官方CLI入口；本包通过`--require dsh_stdio.cjs`预加载，仅把`console.info`重定向到stderr，避免账号插件的诊断文本污染ACP stdout。直接JSON-RPC输出保留原通道。此处理没有改写DSH安装文件、账号或其原profile配置；不是替换DSH内核，也不把OpenDesign的`dsh-profile-jsonl`冒充ACP。

DSH路径位于`%LOCALAPPDATA%\Programs\DeepSeek Harness`，桌面exe在`ELECTRON_RUN_AS_NODE=1`下执行ASAR内CLI。普通系统Node无法直接读取ASAR虚拟路径，目录发现会生成完整启动命令；不要手动把该exe替换为`node.exe`。

## 进度与后台交接

工作台仅要求 Agent 执行剪辑与成果验证，不在剪辑回合内维护软件、整理记忆或写入 Obsidian。新回合及 Codex 旧会话恢复会带入这一约定；ACP 约定仍是普通提示文本，不能当作系统级沙箱。

进度面板优先展示 Codex `turn/plan/updated` 或 ACP `plan` 的真实步骤及完成步骤数；没有计划时显示当前状态，不推算百分比。内部分析记录默认合并，工具流水可展开，异常保留可见。旧会话记录不删除。

后台交接是可选本机文件写入：在状态目录（默认 `%LOCALAPPDATA%\JianyingEngine\workbench`）的 `handoff.json` 设置 `obsidian_vault` 为既有库的绝对路径。库内必须已有 `01-收件箱`。任务成功结束后后台独立写入一份新交接，包含当前回合交付摘要和成果路径，不调用模型、不请求 Agent 权限、不覆盖已有笔记，也不自动整理或提升为已核实项目结论；写入失败记录在 `server.log`。未配置时不写。该设置和所有笔记均在源码包外。

协议参考：[Codex App Server](https://learn.chatgpt.com/docs/app-server)。免审批模式按上述显式运行策略执行，不依赖用户反复选择acceptForSession。

## 资料与维护

官方资料：[DeepSeek ACP](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.md)、[OpenCode ACP](https://docs.opencode.ai/docs/acp/)、[Gemini ACP](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/acp-mode.md)、[Claude Agent ACP](https://github.com/agentclientprotocol/claude-agent-acp)。DSH/OpenCode使用MIT，Gemini/Claude适配器使用Apache-2.0；适配器许可证不等于模型服务授权。具体包名迁移、安装资源及OpenDesign对照见[本轮调研](C:/Users/MECHREVO/Documents/deepseek-harness/default-workspace/jianying-workspace/运行数据/多Agent-20261018/research.md)。

0.6验收：75项Python测试、11组前端模拟通过。DSH/OpenCode真实配置回执与空会话跨进程恢复通过，恢复时核对已确认的选择。测试Agent经真实ACP、MCP生成1秒合成草稿，成果自动发现和文档预览通过，无模型调用。证据见[功能打通目录](C:/Users/MECHREVO/Documents/deepseek-harness/default-workspace/jianying-workspace/运行数据/功能打通-20261018)。真实模型长任务、Gemini/Claude、新增界面的实机视觉与最终剪映导出未验。

0.4历史验收为59项Python测试、8组前端模拟；当时完成三Agent连接和OpenCode/DSH原生连接界面检查、102个资源的wheel一致性检查，不包含0.6新增功能。冻结证据见[多Agent目录](C:/Users/MECHREVO/Documents/deepseek-harness/default-workspace/jianying-workspace/运行数据/多Agent-20261018)。



## 当前主剪辑后端

Agent统一入口为`editing-workbench`（剪辑工作台）；旧Skill名称仅保留文字请求兼容。引擎命令和Python包名仍使用`jianying-engine`/`jianying_engine`，更名不迁移现有工程。

所有后端先读 `guide planning`，在选段、制作和修订中使用同一[创作判断流程](../src/jianying_engine/runtime/guides/planning.md#创作判断流程)。动漫/游戏情绪混剪与MV按指南继续读对应风格方法。工作台的新会话指令和每轮反馈约定均保留该入口，已有会话在下一轮也可读取磁盘上的新指南；指南可达不证明真实模型已正确应用或审美提升。

软件执行依工作台当轮配置。达芬奇读取`reference/resolve-workflow`；剪映读取`editing`、`capabilities`与`plan-format`；独立AE读取`reference/adobe-workflow`的独立分支。各后端通过既有CLI/MCP操作执行，原生参数和验收契约分别保留。AE等原生工程与AVI中间素材在成果面板按文件展示，提供所在目录，不当文本或浏览器视频强行解码。

## 多软件配置与剪映数据保留

在独立“软件配置”模块中选择制作工具：组合模式按剪辑、特效与合成、调色、音频选择软件，剪辑最多一个，其余职责按实际接入可多选；可以只选特效，不必配置主剪辑。单独调用只使用一个软件，保存选择后在任务单填写具体制作要求，再启动任务。

保存配置更新当前会话和新会话初值，不会启动软件、连接Agent或发送任务；执行中不可更改。会话切换及重开恢复各自配置。未绑定选择的旧会话显示“沿用会话工程”，保留实际工程与历史路径；明确保存选择后才绑定。配置过期时保留历史并提示重选，执行前拒绝错误配置。线程级指令仅保存稳定规则，可变选择逐回合传输，避免同会话切换后被首轮软件锁定。

软件目录分别显示接入范围和本机检测状态。工作台读取已有路径配置、Windows安装登记、PATH及常见安装目录，并核对实际程序文件与基础接入条件；卸载残留不作为已安装依据。未安装、版本不兼容、关键运行文件缺失或未开放适配的软件禁选，显示原因，单独调用同样受限。本机状态随页面轮询最多每10秒重新检测，也可点击“重新检测”；保存配置和发送任务前强制复核。旧会话选择在软件卸载后仍保留，提示重新选择并阻止执行，不自动切换软件；已选的失效复选项可取消后保存其他可用选择。

软件适配范围仅剪映、PR、AE、达芬奇。分类展示与执行准入分开：剪映仅在剪辑中展示；AE在特效与合成中展示；达芬奇、PR出现在剪辑、特效与合成、调色和音频中。达芬奇目前仅剪辑接入，其他分类灰显“尚未适配”；PR各分类尚未准入，安装后也不自动开放。剪映原调色/效果实现留在内部接口，UI分类不会删除其能力或历史数据。

Blender和Kdenlive不再列为待适配项，也不参与软件目录或自动检测；旧交接代码、资料和工程保留。降噪、转写等音频功能作为工作台内部工具，继续按任务检查依赖和模型，不列为第五个软件后端。

检测只读，不启动软件、不验证订阅授权或实时宿主连接；静态条件满足仍须执行各后端原有准入。此界面不提升原生能力或保证任意组合的工程互通，各软件未完成适配仍独立推进。

- 剪映原素材、草稿/原生工程、版本计划、原生参数与资源引用、能力契约和验收来源保留原路径。新后端不自动迁移、覆盖或清理它们，既有剪映执行入口继续保留。
- 剪映接口仍在持续适配；统一Skill的内部剪映模块连接原实现与资料，未完成项继续按原台账推进。用户要求进一步整合后，三处安装目录的剪映资源已实体并入editing-workbench/backends/jianying，顶层不保留独立jianying-editor或旧规划/维护目录；原文件内容保留，环境启动器、DSH/工作区环境连接和内部config.env读取已同步新路径。
- 当前会话记录`backend_selection`，全局保存`backend_default`用于新会话；首次初值为达芬奇剪辑＋AE特效。旧会话缺该字段时保持未绑定，不根据全局默认改写原工程。
- 共用素材分析、创作决策和任务/成果管理；原生工程生成、效果参数、执行与验收分别由软件适配层承担。选择主剪辑软件与按需镜头交接可以共存，不能据此承诺跨软件无损交换。
- 软件选项按产品职能放置，每个分类按当前实际接入范围开放；未安装与未适配分别显示原因。缺少核心能力时说明取舍，不静默换软件或丢效果。新适配按子集验证后开放，旧配置若含已退出UI分类的剪映调色等角色则保留记录并提示重选。
- Skill采用中性名称`editing-workbench`，内部[剪映执行模块](../adapters/skills/editing-workbench/modules/jianying/GUIDE.md)和[达芬奇执行模块](../adapters/skills/editing-workbench/modules/resolve/GUIDE.md)按当轮选择读取，其他软件由目录给出对应引擎指南。安装环境与配置归内部backends/jianying，资源连接仍指唯一正式源码，项目原路径和工程验真标识保持不变；分发包不携带本机环境或凭据。

