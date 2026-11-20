# Agent 剪辑创作规划入口

本入口只负责读取独立引擎的 `planning` 指南，不保存第二份分流规则。有 `openedit-engine`（或兼容别名 `jianying-engine`）MCP 时调用 `read_guide("planning")`；否则执行：

```powershell
$enginePython = if ($env:OPENEDIT_ENGINE_PYTHON) { $env:OPENEDIT_ENGINE_PYTHON } elseif ($env:JIANYING_ENGINE_PYTHON) { $env:JIANYING_ENGINE_PYTHON } else { Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv\Scripts\python.exe' }
& $enginePython -m openedit_engine guide planning
```

按指南判断方向是否需要用户先做选择；需求明确且包含制作时，同步读取引擎 `editing` 指南与 [开源剪辑执行模块](../openedit/GUIDE.md) 落地执行。用户仅需要文字建议或脚本拆解时不扩大为建立工程或渲染。引擎缺失时明确报告安装缺口，不把未读取的规则当成已执行。
