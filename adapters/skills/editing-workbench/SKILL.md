---
name: editing-workbench
description: Agent 本机剪辑技能，按素材、脚本或参考做创作规划与剪辑，直接调用纯开源剪辑引擎并应用已学方法。明确维护请求时维护引擎与知识。
metadata:
  version: "3.0.0-openedit"
---

# Agent 剪辑 · 制作、规划与维护（OpenEdit 纯开源栈）

在当前 Agent 对话接收要求和本机素材路径，直接使用 **OpenEdit Engine**（**ArtCraft FilmCraft** + **EffectCraft** + **FFmpeg** + 离线开源 AI 工具）的 MCP 或 CLI，从创作规划执行到严格校验与成片交付。按本轮要求和分工组织流程，具体创作判断以 [planning](modules/planning/GUIDE.md) 为准。续接旧项目时在原项目目录核对 `edit-plan.json`、素材来源及 `.fcproj` 工程；新项目放在独立子目录。

所有能力、创作知识与字段规范由 `openedit-engine` 提供。音频与追踪辅助工具按引擎白名单按需调用，不引入闭源商业软件、加密草稿或云账号依赖。

| 当前任务 | 执行路径 |
|---|---|
| 素材/脚本/参考剪辑或明确局部修改 | 先读引擎 `planning`（按分流表判断，每版同步设计剧情与剪辑、先看同款与检索知识、主动创造连接并作对照），局部修改保留已认可部分只动受影响范围，再读 [开源剪辑执行模块](modules/openedit/GUIDE.md) |
| 多轨主剪辑、Lumetri 调色、重点词高亮与字幕、导出 | 由 **FilmCraft** 承接（`.fcproj` + `validate`/`layout-tracks`/`build`/`preview`/`export`/OTIO/FCPXML） |
| 高级文字动画、图层样式、表达式、308 特效与透明叠层 | 由 **EffectCraft** 编译为锁定 `ProRes 4444 RGBA (.mov)` 透明叠加层合入主轨 |
| 仅方向建议、要求拆解 | [规划模块](modules/planning/GUIDE.md)，读取引擎 `planning`；只给建议不构建/导出 |
| 明确维护 Skill/引擎/知识 | [维护模块](modules/maintenance/GUIDE.md)，读取引擎 `maintenance` 后按影响范围执行与验收 |

有本机 MCP（`openedit-engine`）时，先调用 `describe_operations(include_backends=True)` 核对后端状态，按需 `read_guide` 读取 `planning`、`editing`、`capabilities`、`plan-format`、`reference/openedit` 与 `reference/creative-knowledge-workflows`（按场景组合工具路线）。

新建、整体重剪或解决创意问题时，按 `planning` 先看同款参考（区分真实连续看片与标题封面线索），用 `search_knowledge` 或 CLI `openedit knowledge "具体问题"` 检索已学创作方法并结合 `reference/creative-knowledge-techniques` 的“主动展开候选”作方案对照，再落到选段、切点、主动连接条件、J/L-cut 声桥、节奏与构图；多轨显式 `layer_order` 计划可先 `layout-tracks` 检查安全轨道复用，建议先 `build --preview-times` 或 `--draft` 核帧后再全分辨率导出与 `audit --contact-sheet` 质检。仅在核心目标冲突、不可替代素材缺失或需外部付费/外发服务（如配置自配多模态 `music-edit`）时询问用户，外发 `music-edit review` 前须核对画面与音轨版本及时间段一致；交付时按 `planning` 分别汇报内容表达与技术校验层级。

制作任务不自动进入维护；只要建议不扩大成实际构建。MCP `start_job` 返回 `job_id` 后持续查询 `job_status`，终态通过 `read_job_output` 读结果；`succeeded`（退出码 0）不替代 PNG 静帧检查、`openedit audit` 端到端质检、`ffmpeg -v error -xerror` 全解码核查及使用者的完整视听验收。严禁启动任何图形界面弹窗。

没有 MCP 或需命令行直调时，优先使用 `openedit` 或以下绝对解释器：

```powershell
$enginePython = if ($env:OPENEDIT_ENGINE_PYTHON) { $env:OPENEDIT_ENGINE_PYTHON } else { Join-Path $env:LOCALAPPDATA 'OpenEditEngine\venv\Scripts\python.exe' }
& $enginePython -m openedit_engine paths
& $enginePython -m openedit_engine doctor
& $enginePython -m openedit_engine backends
& $enginePython -m openedit_engine guide planning
& $enginePython -m openedit_engine guide editing
```

具体参数用 `-m openedit_engine <操作> --help`；维护按维护模块先读 `maintenance`。技能 ID 保持 `editing-workbench`，显示名为“Agent 剪辑”。
