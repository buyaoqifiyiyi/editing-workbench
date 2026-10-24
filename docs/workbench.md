# 本地剪辑工作台：网页入口与共用操作

当前界面采用制作工作区、独立软件配置和执行记录三个模块。制作区包含素材监看、任务单和资源面板；项目任务沿用现有会话记录，原素材/工程和历史不迁移。软件按产品功能展示，每个功能分别校验安装与适配状态，未开放项灰显；具体见[多软件配置](agents.md#多软件配置与剪映数据保留)。

当前新项目默认达芬奇主剪辑＋AE局部加工。Agent通过统一 `resolve-workflow` 操作生成DRP与视频，参数和边界见[达芬奇指南](../src/jianying_engine/runtime/references/resolve-workflow.md)。已有工程不会自动迁移；达芬奇已有用户工程打开时执行器会停止保护现场。成果面板支持DRP交付和视频预览，最终完整视听仍由用户验收。

0.4工作台通过所选本机Agent理解需求并调用剪辑引擎：Codex使用专用app-server，其余目录采用ACP，安装与验证范围见[Agent连接说明](agents.md)。0.2网页入口和0.3引入的[Windows原生窗口](desktop.md)共用界面与执行契约。`D:\剪辑项目\启动剪辑工作台.lnk`以及桌面、开始菜单快捷方式均为桌面程序入口；本文保留网页命令及共用操作。

## 启动与使用

要求Windows、Python 3.12、已安装的引擎及`[workbench]`或`[all]`依赖，并有可运行的所选Agent。工作目录必须已存在。

```powershell
$enginePython = Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv\Scripts\python.exe'
$workDirectory = 'D:\剪辑项目\工作台项目'
New-Item -ItemType Directory -Path $workDirectory -Force | Out-Null
& $enginePython -m jianying_engine workbench --workspace $workDirectory --open
```

激活引擎环境后，可直接运行：

```powershell
python -m jianying_engine workbench --workspace 'D:\剪辑项目\工作台项目' --open
```

服务只监听本机`127.0.0.1`，默认自动选择端口。网页模式重复启动可复用网页服务；桌面模式重复启动唤醒已有原生窗口。两种模式不能同时占用同一状态目录，切换前先正常退出已有服务。已保存会话可能恢复原工作目录，进入页面后核对当前目录。

1. 在左下「执行引擎设置」选择/扫描Agent，必要时指定exe与JSON参数数组。连接进行协议初始化，不发送prompt；Node脚本使用node.exe加脚本参数，不执行.cmd或shell。
2. 核对工作目录，选择素材或填写本机素材路径。原素材保留原位；后续移动或删除会影响计划与草稿引用。
3. Codex模型和强度取实际目录，也可沿用CLI配置。ACP首版沿用Agent默认模型/强度，空目录不表示无法使用默认模型；切换Agent会清除旧选择。
4. 在制作任务单填写要求，点击“执行任务”或Ctrl/Cmd+Enter开始；Enter只换行。这一步才创建或继续Agent回合；进度页展示真实阶段，执行记录保留要求与反馈。需要终止时使用停止操作；切换Agent或命令不自动重放旧会话。
5. 在资源面板成果页查看生成文件与验证结果。后台结构通过、实际播放和导出分别验收；最终完整视听与导出仍由用户完成。

各Agent使用自己的登录和提供商配置；工作台不读取认证文件、不代登录、不跨Agent复制账号，也不替用户购买额度。更换登录或提供商后重新连接，模型调用仍使用该Agent配置对应的服务和用量。

## 素材、权限与数据位置

素材列表会作为本机绝对路径写入发给所选Agent的提示；选择素材本身不会把整个视频直接上传。Agent执行时可能读取素材、抽帧、查看图像或将工具结果交给所选模型，因此这不是离线模型工作流。路径、对话、读取到的文本及图像可能进入配置的云端服务上下文。

Codex以所选目录为工作目录，沿用`workspace-write`与`on-request`。ACP权限由Agent自身管理；客户端不提供filesystem/terminal回调不等于Codex沙箱。素材列表不是读取白名单，也不保证Agent只能看到这些文件。ACP只批准显式的一次性允许选项；若没有`allow_once`，拒绝而不扩大成永久允许。工作台指令要求保留原素材、另建版本；遇到审批时检查具体路径与操作。[权限和恢复边界](agents.md#权限与中断)。

状态默认保存在 `%LOCALAPPDATA%\JianyingEngine\workbench`：

| 文件 | 用途 |
|---|---|
| `sessions.json` | 会话、消息、素材路径和事件记录 |
| `agents.json` | 成功连接后的Agent选择与命令；不含凭据 |
| `server.json` | 当前服务 PID、端口与本机地址 |
| `server.log` 及轮转日志 | 服务诊断 |
| `server.lock` | 同一状态目录的进程互斥 |
| `launcher.log`、`webview/` | 桌面启动异常及外置WebView运行数据 |

这些数据留在包外；需要自定义位置可传`--state-dir`。会话记录可能包含私人路径与工作内容，`server.json`可能包含本机访问/唤醒令牌，不作为公开分发资源。Agent自身的会话与配置仍由对应Agent管理。

## 停止与升级

**关闭浏览器标签不会停止后台服务，也不会自动中断正在进行的模型任务。** 先在工作台停止当前任务并等到停止完成。若从终端启动服务，在该终端按`Ctrl+C`可退出；工作台关闭自己创建的Agent进程树，不按进程名称清理其他应用。

桌面模式的关闭按钮接入原生生命周期：闲置退出；忙时先确认，再中断并关闭自有服务。桌面用户优先用关窗退出，不需要日常手工结束PID；具体实现与原生验收范围见[桌面说明](desktop.md#窗口与服务生命周期)。下列PID核对流程用于旧网页后台或异常排查。

后台快捷方式启动时，先检查状态记录与实际进程是否一致：

```powershell
$workbenchState = Join-Path $env:LOCALAPPDATA 'JianyingEngine\workbench'
$serverInfo = Get-Content -LiteralPath (Join-Path $workbenchState 'server.json') -Raw | ConvertFrom-Json
$workbenchProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($serverInfo.pid)"
$workbenchProcess | Select-Object ProcessId, ExecutablePath, CommandLine
```

确认该 PID 的解释器属于引擎环境，命令行明确包含 `-m jianying_engine.workbench`（CLI 包装入口也接受 `-m jianying_engine workbench`），再停止这个 PID；PID 不存在或命令不符时不要停止。不要使用按名称批量结束 `python` 或 `codex` 的命令。状态文件可能残留，不能只凭 `server.json` 就确认进程身份。

确认后可在同一 PowerShell 中执行以下限定检查：

```powershell
$engineBin = Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv\Scripts'
$baseBin = & (Join-Path $engineBin 'python.exe') -c 'import sys; print(sys.base_prefix)'
$expectedExecutables = @((Join-Path $engineBin 'python.exe'), (Join-Path $engineBin 'pythonw.exe'), (Join-Path $baseBin 'python.exe'), (Join-Path $baseBin 'pythonw.exe'))
$workbenchProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($serverInfo.pid)"
if (-not $workbenchProcess -or $workbenchProcess.ExecutablePath -notin $expectedExecutables -or
    $workbenchProcess.CommandLine -notmatch '\s-m\s+jianying_engine(?:\s+|\.)workbench(?:\s|$)') {
    throw '进程不存在或身份不匹配；未停止任何进程。'
}
Stop-Process -Id $workbenchProcess.ProcessId
```

升级前停止服务，备份工作区与外置状态。源码维护可在 `engine/` 目录使用引擎解释器执行 `-m pip install -e '.[all]'`；wheel 部署安装新的 wheel 及所需 extras。之后执行 doctor 和相关测试，再重新启动、连接。升级包不会迁移模型或替换用户素材，不复制旧虚拟环境。

## 历史验证范围与参考

以下是2026-10-18网页0.2阶段的冻结读数，不是桌面0.3验收：31项Python测试通过；Codex真实验证仅覆盖本机app-server连接、账号布尔状态及实时模型列表，未发送真实模型任务。模拟协议测试覆盖发送、中断、审批与失败回收，但不等于真实剪辑回合完成。5组前端模拟断言通过；实际界面核验了连接等待反馈、实时模型/推理强度、窄窗面板，以及图片路径添加/预览/移除。原生文件选择对话框、真实模型任务当时未验证。97个发行资源逐字节一致，wheel隔离安装通过3项引擎检查、2项MCP测试与10项工作台接口测试。桌面本轮验证单独记录在[桌面说明](desktop.md#验证状态)。

协议依据：[Codex App Server 官方文档](https://learn.chatgpt.com/docs/app-server)。界面与本地 Agent 连接方式参考本机 OpenDesign 实现，执行仍复用本引擎的现有契约与证据边界。[CLI 说明](../README.md)与[MCP 接入](mcp.md)可独立使用。

