# OpenEdit 引擎维护指南

仅在用户明确提出“维护引擎、修复 Bug、扩展能力或同步文档/技能”时进入本流程；普通剪辑任务不自动修改引擎源码。

## 源码架构与唯一真值

- **正式核心源码**：`src/openedit_engine/`（其中 `api.py` 维护唯一操作白名单与 BM25 知识检索，`software_backends.py` 与 `software_detection.py` 维护开源后端目录与只读探测，`runtime/scripts/` 存放底层编排与 `*_self_check.py`，`runtime/references/` 与 `runtime/guides/` 存放指南与技术规范）。
- **向后兼容垫片**：`src/jianying_engine/` 仅保留 9 个极薄委托垫片文件映射至 `openedit_engine`，不在该目录下新增或分叉第二份业务逻辑。
- **Skill 适配器模板**：`adapters/skills/editing-workbench/` 是外部宿主（`.agents/skills/editing-workbench` 与 `.gemini/config/skills/editing-workbench`）的唯一源模板；同步 Skill 目录时只同步 `SKILL.md`、`agents/` 与 `modules/`，**绝对禁止改动或覆盖**宿主目录中可能存在的 `backends\` 与 `.magpie-library`。

## 修改流程与禁止事项

1. 修改代码或文档前先通过 `openedit paths`（或 `python -I -m openedit_engine paths`）核实当前虚拟环境解释器指向正式源码目录（editable 安装）。
2. 先审视假设、调用方与测试合约，尽量复用 `runtime_core.py`、`runtime_common.py`、`craft_rpc.py`、`file_hash.py` 等公共原语；新增白名单能力时同步更新 `api.py`、`OPERATION_SCHEMAS`、对应文档与单测。
3. **严格禁止**：
   - 未经用户明确指令执行 `git commit` 或 `git push`；
   - 启动任何 GUI 窗口进程（FilmCraft / EffectCraft / 商业软件前端）；
   - 修改 `%LOCALAPPDATA%\JianyingEngine\runtime-admissions.json` 或他人/历史剪映用户草稿；
   - 在代码或指南中写回假定商业硬件锁（如 640x360/2s 限制）。

## 分层回归验收清单

改动结束后按影响范围在正式虚拟环境执行验证，并逐项记录实测读数（不将 skipped/mock 算作全通过）：

1. **包与接口单元测试**：`& <venv-python> -m unittest discover -s tests -v`（覆盖 API 白名单、MCP 协议与结构化参数、Job 进程树清理、BM25 检索与锚点、后端探测、安全校验及各业务流程）。
2. **运行时诊断与自检**：`openedit doctor`、`openedit backends`、`openedit checks`（注意核实 `passed` / `failed` / `skipped` 项，如 `separate`/`transcribe` 因无外部模型权重跳过须如实标注）。
3. **实时 stdio MCP 核验**：以独立子进程起 `openedit_engine.mcp_server` 及兼容入口 `jianying_engine.mcp_server`，完成 `initialize`、`tools/list`、`describe_operations(include_backends=True)`、`search_knowledge` 与 `read_guide`。
4. **实机渲染与严格解码复验**：在独立临时目录编译 1080p 测试工程（含 EffectCraft ProRes 4444 透明叠层 + FilmCraft 多轨与中文字幕高亮 + 导出 MP4），核验 `ffmpeg -v error -xerror` 全解码与 PNG 预览图后清理临时目录。
5. **外部台账归档**：向 `Skill维护层/变更/<日期-事项-版本>/` 写入变更 `README.md` 与 `hashes.txt`（实测 SHA-256），并更新外部 `任务清单.md`。
