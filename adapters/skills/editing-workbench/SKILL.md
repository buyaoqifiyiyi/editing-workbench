---
name: editing-workbench
description: Agent 本机剪辑技能，按素材、脚本或参考做创作规划与剪辑，直接调用本机剪辑引擎并应用已学方法。明确维护请求时维护引擎与知识。
metadata:
  version: "2.4.0-agent"
---

# Agent 剪辑 · 制作、规划与维护

在当前 Agent 对话接收要求和本机素材路径，直接使用引擎 MCP 或 CLI，从规划执行到汇报实际产物。按本轮要求、现有工程和已确认的职责分工选择宿主，具体以 [planning](modules/planning/GUIDE.md) 为准。续接旧任务时在原项目目录核对版本计划、素材引用与草稿。新项目放在剪辑工作区的独立目录。

各宿主共用创作判断，按本机安装和各自的能力准入执行。剪映和达芬奇继续作为正式后端，AE按已接入的特效与合成子集执行，PR的开放范围以当前引擎指南为准。音频辅助工具按白名单按需调用。

执行能力、创作知识与能力主表由 `jianying-engine` 提供。剪映兼容环境、原配置和资源连接在已安装 Skill 的 `backends/jianying/`。项目素材、草稿、模型、学习来源与验收记录按原路径使用。

| 当前任务 | 路径 |
|---|---|
| 素材/脚本/参考剪辑或明确局部修改 | 先读引擎planning并贯穿创作判断，按用户所选宿主读取执行指南 |
| 用户选择剪映新建、续接或修改 | 读取[剪映执行模块](modules/jianying/GUIDE.md)，使用既有剪映接口 |
| 本轮组合或单独调用选择达芬奇 | 读取[达芬奇执行模块](modules/resolve/GUIDE.md)，使用resolve-workflow |
| 单独调用已准入的软件或只做音频处理 | 软件适配范围仅剪映、PR、AE、达芬奇；音频功能作为内部工具按需调用，不强制主剪辑链 |
| 仅方向建议、要求拆解；jianying-planner文字请求 | [规划模块](modules/planning/GUIDE.md)，通过MCP读取planning；只建议不build/publish |
| 明确维护Skill/引擎/经验知识；jianying-editor-maintainer文字请求 | [维护模块](modules/maintenance/GUIDE.md)，读取maintenance后按实际范围执行 |

有本机MCP时先describe_operations，按需read_guide。操作和字段以当前白名单、capabilities/plan-format及用户当前项目宿主为准；不把旧指南中的宿主前言覆盖用户已明确的Resolve、Adobe或剪映选择。外部模块中的旧技能名在本入口分流，不需要独立技能目录。

新建、整体重剪或解决创意问题时，用 `search_knowledge` 或 CLI `knowledge "具体问题"` 检索已学方法，通过 `read_guide` 或 `guide` 读对应知识。把选用技法落实到镜头、切点、节奏、构图或声音，制作中核对，交付时按 planning 分别报告内容与视听表达。仅做建议或拆解时按请求交付文本。

制作任务不自动进入维护；只要建议不扩大成项目制作；方向明确或已授权自主决定时不重复确认开始。制作细节、具体创意和能力准入以独立引擎当前指南为准。

MCP start_job返回job_id后持续job_status，结束通过read_job_output读结果。job_id不是完成，succeeded/退出码0不等于保存重开、完整视听或导出。默认不操作剪映前台；最终完整视听和导出遵循当前任务与引擎分工，不提升证据层级。

没有MCP时沿用已安装解释器：

```powershell
$enginePython = if ($env:JIANYING_ENGINE_PYTHON) { $env:JIANYING_ENGINE_PYTHON } else { Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv\Scripts\python.exe' }
& $enginePython -m jianying_engine paths
& $enginePython -m jianying_engine doctor
& $enginePython -m jianying_engine guide planning
& $enginePython -m jianying_engine guide editing
```

具体参数用`-m jianying_engine <操作> --help`；维护按对应模块读maintenance。运行时缺失则报告安装缺口，不下载未知同名项目。读取引擎文件时以paths返回真实目录为准。

技能 ID 继续使用 `editing-workbench`，显示名为“Agent 剪辑”。历史名称“剪辑工作台”及 jianying-editor、jianying-planner、jianying-editor-maintainer 的文字请求仍由本技能分流。
