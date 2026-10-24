# 本机 MCP 接口

使用官方 Python SDK `mcp==2.3.0`，仅运行 stdio，不监听 HTTP、不读取 Agent 凭据。新增`music-edit analyze`仅通过用户专用配置调用所选OpenAI兼容或Gemini原生模型，并发送明确选区的实际音视频，产生API用量；其他操作不调用模型。见[原生视听配置与边界](../src/jianying_engine/runtime/references/music-edit.md)。启动方式：

```powershell
& '<引擎Python绝对路径>' -m jianying_engine.mcp_server --state-dir 'D:\剪辑项目\引擎任务'
```

Agent 的 MCP 配置使用同一解释器作为 `command`，将 `-m`、`jianying_engine.mcp_server`、`--state-dir` 与目录分别作为 `args` 数组元素；不要拼成 shell 命令。MCP本身无需API key；仅启用视听分析时须配置对应服务。目录省略时使用 `%LOCALAPPDATA%\JianyingEngine\jobs`，也可设置 `JIANYING_ENGINE_STATE_DIR`。状态目录须在包与 Skill 目录之外。

## 六个工具

| 工具 | 用途 |
|---|---|
| `describe_operations()` | 返回核心 API 的白名单操作 |
| `read_guide(name)` | 读取核心 API 中具名打包指南；最多返回128Ki字符，明确标记截断，不接受任意路径 |
| `start_job(operation, arguments, timeout_seconds=1800)` | 使用参数 token 数组启动任务，返回 job_id；不代表完成 |
| `job_status(job_id)` | 查询状态、退出码、日志摘要与日志文件位置 |
| `read_job_output(job_id, stream='stdout', offset=0, limit=32768)` | 分页读取终态任务的受限日志，Agent无需文件系统访问 |
| `cancel_job(job_id)` | 终止该任务自己的子进程树，等待收割后返回 |

每个任务先经过核心 `command_for` 白名单校验；不存在任意 shell/Python 执行接口。`arguments` 的具体选项沿用操作对应的 CLI；外接 Agent 应传绝对素材、计划及输出路径，避免依赖宿主启动目录。工具启动返回 `rejected` 时没有任务 ID；非法 ID 返回 `unknown`。

运行中状态是 `running`；取消和超时收尾期间可能为 `cancelling` / `timing_out`。终态为 `succeeded`、`failed`、`cancelled` 或 `timed_out`，同时有 `finished` 时间和实际 `returncode`。`succeeded` 仅表示进程退出码为 0，仍须查看操作报告的验证范围，不能当成播放、UI 或导出验收。

`read_job_output` 仅接受当前会话的任务 ID，`stream`限于`stdout`或`stderr`，不接受文件路径。运行与日志收尾未完成时返回`not_ready`；完成后按Unicode字符偏移分页，`offset`是非负整数，`limit`为1到65536的整数。结果含`text`、`next_offset`、`total_characters`及`truncated`；`next_offset=null`表示读完保留内容。跨页拼接`text`后才尝试解析操作JSON。`truncated=true`表示原日志超过1MiB，保留下来的前缀不能当完整JSON；`complete_log`也不代表输出本身一定是JSON。UTF-8非法或截断字节会以替换字符解码。

## 生命周期与限额

同一服务最多并发 2 个任务，最多保存 128 个任务句柄；达到上限明确拒绝，不自动排队。默认超时 1800 秒，可指定正数至 7200 秒。每个 stdout/stderr 日志最多保留前 1 MiB，之后继续排空管道但不继续写盘；返回保留日志的最后 8 KiB，终态通过 `*_truncated` 标明截断。每个任务在独立随机目录保存两份日志及 `result.json`。stdout 仅由 SDK写 MCP 协议，子进程日志不会混入协议流。

任务只存在于当前服务会话，不在重启后恢复。磁盘回执保留供人工核查，不用旧 PID 恢复控制权；用户可在服务停止后自行归档日志。服务退出会清理自己启动的任务树，失败输出及运行时产生的项目产物由各操作自己的契约处理。

Windows 使用挂起创建的子进程，绑定设置了关闭即终止的 Job Object 后才恢复执行，避免子进程先逃出归属；这段依赖 Windows CPython 的进程句柄及 `NtResumeProcess`，绑定失败即拒绝启动并收割。服务正常退出或被终止时，Job Object关闭会结束所属进程树，不按名称查杀用户应用。POSIX使用专属进程组；退出时终止该组，不承诺脱离进程组的恶意子进程隔离。这是本机可信 Agent 的执行入口，不是多租户权限沙箱。

## 验证与来源

`python -m unittest discover -s tests -p 'test_jobs.py'` 检查退出码、取消、超时、输出上限、Windows子树和不误杀外部进程。`test_mcp.py` 使用官方客户端真实启动 stdio服务，完成协商、工具发现、白名单拒绝、doctor启动和状态查询；不发布草稿或操作UI。

2026-10-18 在独立引擎虚拟环境验证进程管理与SDK测试。真实stdio覆盖六工具发现、指南读取、非法操作与指南路径拒绝、doctor进程退出码0、状态轮询与完整分页读取；另覆盖多页Unicode拼接、stderr选择、非法偏移/页长/流拒绝、截断标记及运行中拒绝分页。SDK lifespan测试确认会话关闭收割活动任务，Windows子进程树取消后退出，独立启动的无关进程继续存活。这里不包含外部Agent模型接入、宿主配置安装或真实剪辑导出验收。

2026-10-18 核实：[官方 SDK](https://github.com/modelcontextprotocol/python-sdk)、[PyPI 2.3.0](https://pypi.org/project/mcp/2.3.0/)、[生命周期](https://py.sdk.modelcontextprotocol.io/handlers/lifespan/)、[stdio 客户端](https://py.sdk.modelcontextprotocol.io/client/transports/#stdio)。采用SDK的工具注册、参数Schema与传输实现，没有自行实现JSON-RPC。
