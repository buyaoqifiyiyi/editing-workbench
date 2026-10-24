# 剪辑工作台创作规划入口

本入口只负责读取独立引擎的 `planning` 指南，不保存第二份分流规则。有 `jianying-engine` MCP 时调用 `read_guide("planning")`；否则执行：

```powershell
$enginePython = if ($env:JIANYING_ENGINE_PYTHON) { $env:JIANYING_ENGINE_PYTHON } else { Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv\Scripts\python.exe' }
& $enginePython -m jianying_engine guide planning
```

按指南判断方向是否需要选择；明确要求直接拆解，原请求包含制作时按用户所选宿主读取执行指南；剪映用`editing`，当前达芬奇用`reference/resolve-workflow`。用户只要建议时不扩大成制作工程。引擎缺失时说明安装缺口，不把未读取的规则当成已执行。
