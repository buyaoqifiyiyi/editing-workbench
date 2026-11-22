# 本机 MCP 接口（openedit-engine）

使用官方 Python SDK `mcp` 仅运行本机 **stdio** 传输，不监听 HTTP 端口、不启动任何 GUI 应用、不读取 Agent 私人凭据。仅在调用方显式触发 `music-edit analyze` 或 `music-edit review` 时，才会按使用者单独配置的专用 API 端点发送选定音视频并发起模型请求；其余全部剪辑与探测操作均在本地纯开源环境中离线完成。见 [原生视听与音乐编排](../src/openedit_engine/runtime/references/music-edit.md)。

## 启动与客户端配置

可使用脚本命令 `openedit-mcp`（或兼容别名 `jianying-engine-mcp`），也可直接使用虚拟环境 Python 启动模块 `openedit_engine.mcp_server`（兼容模块 `jianying_engine.mcp_server`）：

```json
{
  "mcpServers": {
    "openedit-engine": {
      "command": "C:/Users/MECHREVO/AppData/Local/OpenEditEngine/venv/Scripts/python.exe",
      "args": ["-m", "openedit_engine.mcp_server"]
    }
  }
}
```

- 默认任务状态持久化目录为 `%LOCALAPPDATA%\OpenEditEngine\jobs`（兼容环境变量 `OPENEDIT_ENGINE_STATE_DIR` 或 `JIANYING_ENGINE_STATE_DIR`，也可在启动参数末尾传入 `["--state-dir", "<绝对路径>"]`）。状态目录应位于源码与 Skill 包之外。

## 七个受控工具

| 工具 | 用途 |
|---|---|
| `describe_operations(operation=None, include_backends=False)` | 返回纯开源白名单操作说明、全部 39 个白名单操作（含 `auto-cut` 气口粗剪、`matting` AI 抠像、`sfx` 程序化音效、`motion-analyze` 光流与 LUT 等）的结构化参数 Schema（`operation_schemas` 100% 覆盖）、会话任务限额（`job_limits`）及本地指南注册表；传 `operation` 聚焦单操作；传 `include_backends=true` 附带只读检测开源后端可用性（FilmCraft / EffectCraft / FFmpeg） |
| `read_guide(name)` | 读取打包注册的内置指南与参考文档（最多返回 `128Ki` 字符并清晰标出 `truncated`）；拒绝任意文件系统路径或符号链接越界 |
| `search_knowledge(query, limit=5)` | 在捆绑指南/参考库中执行按小节 Okapi BM25（`k1=1.2, b=0.35`）本地检索，返回指南名、小节标题/行号、`anchor`、局部多行 `context` 与关联 `capabilities` 锚点提示（`capability_hints`） |
| `start_job(operation, arguments=None, params=None, timeout_seconds=1800)` | 经核心 `command_for` 白名单校验启动独立进程任务（`arguments` 数组与结构化字典 `params` 二选一），返回 `job_id`；`job_id` 仅代表已启动，不代表完成 |
| `job_status(job_id)` | 查询任务当前状态（`running`/`cancelling`/`timing_out`/`succeeded`/`failed`/`cancelled`/`timed_out`）、返回码、日志尾摘录及日志文件位置；已被 LRU 淘汰的终态任务返回磁盘目录提示 |
| `read_job_output(job_id, stream='stdout', offset=0, limit=32768)` | 按 Unicode 字符偏移分页读取终态任务的受限 `stdout`/`stderr` 日志（`limit` in `1..65536`）；任务尚未完成时返回 `not_ready` |
| `cancel_job(job_id)` | 终止所属子进程树（Windows 下通过 Job Object 收割）并等待结束后返回 |

## 并发、队列与进程树隔离

- 同一 MCP 会话允许最多 **2 个** 并发活动子任务；内存中按 LRU 保留最近 **128 个** 任务句柄，满额时**仅淘汰最早访问的终态任务**，保留全部活动任务句柄，且被淘汰任务的磁盘日志目录（`stdout.log`、`stderr.log`、`result.json`）保持不删。
- 单任务默认超时 1800 秒（可设置正数至 7200 秒），每个流磁盘日志上限 `1 MiB` 并如实标记截断。
- Windows 使用 `CREATE_SUSPENDED` 创建子进程并绑定 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` Job Object 后再用 `NtResumeProcess` 放行，关闭会话或调用 `cancel_job` 时连同 FFmpeg / FilmCraft / EffectCraft 子进程一并清理。
- 所有任务传递绝对路径参数，`succeeded` 仅代表进程退出码 0，仍须读取完整结构化输出核实 `build-report.json` 与 `strict_decode`。
