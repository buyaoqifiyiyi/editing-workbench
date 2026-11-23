# OpenEdit 引擎维护兼容入口

明确维护请求才使用本入口。当前引擎已全面转向纯开源架构（**FilmCraft** + **EffectCraft** + **FFmpeg** + 离线开源 AI 组件），唯一正式源码位于仓库 `src/openedit_engine/`。通过 MCP 调用 `read_guide("maintenance")`，或执行：

```powershell
$enginePython = if ($env:OPENEDIT_ENGINE_PYTHON) { $env:OPENEDIT_ENGINE_PYTHON } else { Join-Path $env:LOCALAPPDATA 'OpenEditEngine\venv\Scripts\python.exe' }
& $enginePython -m openedit_engine paths
& $enginePython -m openedit_engine guide maintenance
```

执行代码和知识唯一来源为引擎源码，不在 Skill 目录内分叉第二套实现。修改后务必区分并完整汇报单元测试（`unittest`）、自检（`openedit checks`，标注因缺权重而跳过的项）、实时 stdio MCP 回环、1080p 导出严格全解码（`ffmpeg -v error -xerror`）与 PNG 静帧核对；严禁擅自提交 Git、启动 GUI 或覆盖宿主 `.magpie-library`。普通剪辑任务不自动修改引擎源码。
