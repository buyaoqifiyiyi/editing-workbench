# Agent 剪辑创作规划入口

本入口只负责读取独立引擎的 `planning` 指南，不保存第二份分流规则。有 `openedit-engine`（或兼容别名 `jianying-engine`）MCP 时调用 `read_guide("planning")`；否则执行：

```powershell
$enginePython = if ($env:OPENEDIT_ENGINE_PYTHON) { $env:OPENEDIT_ENGINE_PYTHON } elseif ($env:JIANYING_ENGINE_PYTHON) { $env:JIANYING_ENGINE_PYTHON } else { Join-Path $env:LOCALAPPDATA 'OpenEditEngine\venv\Scripts\python.exe' }
& $enginePython -m openedit_engine guide planning
```

按指南落实以下四项核心原则并判断方向是否需要用户先做选择（不在本页分叉第二套规则）：

1. **需求分流与授权边界**：方向未定时给 3 个（素材允许时）有实质差异的简短方向并推荐 1 个；已有脚本/参考/明确方向时直接落实；局部修改仅核对受影响片段与相邻接点；仅在核心目标冲突、不可替代素材缺失或需外部付费/外发服务（如自配云端 `music-edit`）时询问用户。
2. **每版双线与同款参考**：每版同步设计剧情/内容推进与镜头声画表现，新建/整体重剪前主动看同款参考并经 `search_knowledge` 对照通用与高级候选。
3. **6 步创作判断与开源实现**：连续选段 → 有镜头依据表达 → 声画协同 → 主动创造转场/接管条件 → 适量可执行技法与 AI 前提核查 → `build --preview-times`/`--draft`、短段单变量对照与 `audit` 分层回看。
4. **交付与两类验收分离**：仅需要文字建议或拆解时不扩大为建立工程或渲染；包含制作时将内容与视听取舍和技术验证分层分开汇报。引擎缺失时明确报告安装缺口，不把未读取的规则当成已执行。
