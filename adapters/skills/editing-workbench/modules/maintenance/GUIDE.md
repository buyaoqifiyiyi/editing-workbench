# 剪辑引擎维护兼容入口

明确维护请求才使用本入口。软件目录和角色选择属于工作台配置，共用现有CLI/MCP；新增选项须准确标明接入范围，不能用UI选中代替能力准入。剪映和达芬奇都是工作台内部持续适配的后端，剪映的未完成项继续沿用原能力表、缺口台账和验收资料，不因对外统一名称而独立归档或停止维护。通过 `jianying-engine` MCP 的 `read_guide("maintenance")` 读取维护指南，或执行：

```powershell
$enginePython = if ($env:JIANYING_ENGINE_PYTHON) { $env:JIANYING_ENGINE_PYTHON } else { Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv\Scripts\python.exe' }
& $enginePython -m jianying_engine paths
& $enginePython -m jianying_engine guide maintenance
```

执行代码和知识唯一来源为引擎源码，不在旧 Skill 内另存实现。先核对当前能力、调用方和证据，再最小修改；使用现有运行时回归、包接口测试及 wheel 资源检查。记录、素材、模型、凭据、项目与日志留在包外。跳过、结构回读、原生静帧、UI、视听与导出分别记录；普通剪辑不自动修改引擎。
