---
name: editing-workbench
description: 本机剪辑工作台统一入口：按素材、脚本或参考进行创作规划与剪辑，遵循用户选择的软件及其实际能力；明确维护请求时维护本机引擎与知识，普通制作不修改引擎。
metadata:
  version: "2.3.0-composable-backends"
---

# 剪辑工作台 · 制作/规划/维护统一入口

剪映与达芬奇都是本工作台内部持续维护的适配后端。保留剪映原始素材、工程、版本计划、实现、资源与验收资料，未完成的剪映适配继续沿用原主表和缺口台账，不作为退出使用的历史备份。由用户选择宿主；按会话选择剪辑、特效与合成、调色、音频后端组合或单独调用一个软件，旧会话未绑定时沿用其实际工程。共用创作判断，各宿主能力独立验证。工作台每轮软件配置优先于此前回合，保存配置不启动软件或模型任务。

工作台软件配置为独立模块，四软件按产品功能分类展示，当前可调用范围由每项功能的准入与本机安装共同决定。剪映只列剪辑；达芬奇与PR按剪辑/特效/调色/音频展示，未适配分类灰显，AE列特效与合成。产品能做不代表当前引擎已接入，不能从界面分类自行开放能力；音频辅助工具继续按内部白名单使用。

执行能力属于独立jianying-engine，唯一实现仍在正式源码。已安装的剪映环境、原配置与资源连接统一放在本Skill的`backends/jianying/`内部；分发模板只包含指引，不复制环境、凭据或项目。素材、草稿、模型和日志继续留在原项目路径。

| 当前任务 | 路径 |
|---|---|
| 素材/脚本/参考剪辑或明确局部修改 | 先读引擎planning并贯穿创作判断，按用户所选宿主读取执行指南 |
| 用户选择剪映新建、续接或修改 | 读取[内部剪映执行模块](modules/jianying/GUIDE.md)，使用既有剪映接口 |
| 本轮组合或单独调用选择达芬奇 | 读取[内部达芬奇执行模块](modules/resolve/GUIDE.md)，使用resolve-workflow |
| 单独调用已准入的软件或只做音频处理 | 软件适配范围仅剪映、PR、AE、达芬奇；音频功能作为内部工具按需调用，不强制主剪辑链 |
| 仅方向建议、要求拆解；jianying-planner文字请求 | [规划模块](modules/planning/GUIDE.md)，通过MCP读取planning；只建议不build/publish |
| 明确维护Skill/引擎/经验知识；jianying-editor-maintainer文字请求 | [维护模块](modules/maintenance/GUIDE.md)，读取maintenance后按实际范围执行 |

有本机MCP时先describe_operations，按需read_guide。操作和字段以当前白名单、capabilities/plan-format及用户当前项目宿主为准；不把旧指南中的宿主前言覆盖用户已明确的Resolve、Adobe或剪映选择。外部模块中的旧技能名在本入口分流，不需要独立技能目录。

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

本组只注册editing-workbench，显示名为“剪辑工作台”。jianying-editor、jianying-planner和jianying-editor-maintainer仅保留文字请求语义。安装目录已实体整合：Skills顶层不保留独立jianying-editor，剪映资源及原环境位于`backends/jianying/`，历史入口说明保留在内部。内部剪映后端继续适配，环境启动器和资源配置路由已按新路径维护；已有工程和正式引擎命令不受更名影响。
