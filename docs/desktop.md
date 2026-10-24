# 剪辑工作台桌面安装版 0.6

当前桌面视觉采用专业深色面板：石墨分区、黑色源监看器、紧凑工具栏与任务dock、右侧资源dock。源图像可切换适合窗口/100%原始像素，状态栏显示实际解码尺寸；注册图片在可见素材批次中懒加载缩略图。软件配置采用左职责矩阵、右本机软件清单，未适配/未安装项仍灰显禁选。原三模块与执行契约保持。

0.6补齐会话模型/推理/模式配置、DSH/OpenCode恢复，以及任务完成后的成果自动刷新。成果页可直接查看文档/字幕/JSON或播放支持的媒体，右侧目录入口仍可定位原文件。单独选择旧会话会重新发现该工作区成果；已有成果清单随会话保存。

2026-10-21按制作工作台重新组织界面：中央素材监看、下方任务单、右侧素材/进度/成果资源面板；顶部独立切换“制作工作区”“软件配置”“执行记录”。旧会话作为项目任务保留，长对话归入执行记录；不迁移原项目。资源面板桌面默认展开，窄屏通过右上入口打开。Ctrl/Cmd+Enter执行任务，Enter换行；原Agent模型、推理强度与连接入口保留。

软件配置按产品功能分类，软件安装状态与功能适配分别判断。达芬奇和PR出现在剪辑/特效/调色/音频中，未适配项灰显；AE列特效，剪映只列剪辑。显示产品功能不代表已开放执行能力；旧配置与原生工程保持，过期配置需明确重选。

桌面窗口复用工作台界面、多Agent连接和本机剪辑引擎。0.4保留Codex专用app-server，并增加DSH、OpenCode等ACP目录；已找到、已连接和已验证的范围见[Agent连接说明](agents.md)。Windows启动器负责找到已安装的引擎，Python通过pywebview与Edge WebView2打开原生窗口；模型、账号和素材保持外置。

## 素材引用

选择文件夹时只登记文件夹绝对路径，Agent按要求自行查看目录；工作台不预先扫描、展开、复制或上传整个文件夹。目录内文件数量不受原64项限制；单独选择文件仍可预览，多条文件引用分批显示。长路径清单保存为本机索引，避免请求正文过长；目录引用和索引都不代表Agent已经读过内容。

## 当前安装位置

源码已移至 `C:\Users\MECHREVO\Desktop\剪辑工作台源码`；旧工作区的 `engine/` 仅作 Junction 兼容入口。当前虚拟环境关联桌面源码，应用及快捷方式继续使用原安装位置。移动源码不会移动项目、模型、会话或账号。

2026-10-18 已核对安装文件与快捷方式目标：

| 位置 | 内容 |
|---|---|
| `%LOCALAPPDATA%\Programs\JianyingWorkbench\剪辑工作台.exe` | .NET Windows启动器 |
| Windows桌面「剪辑工作台」 | 启动上述程序 |
| 开始菜单「剪辑工作台」 | 启动上述程序 |
| `D:\剪辑项目\启动剪辑工作台.lnk` | 同一桌面程序的项目入口 |
| `D:\剪辑项目\工作台项目` | 三个快捷方式传入的工作区 |

双击任一快捷方式进入桌面窗口。若已有会话保存过其他工作目录，进入后仍应核对界面中的实际工作区。直接双击exe而未传`--workspace`时，默认使用当前用户文档目录下的「剪辑工作台」。

这是依赖本机现有运行环境的安装版：单独复制exe到其他电脑不能迁移引擎。它依赖`%LOCALAPPDATA%\JianyingEngine\venv\Scripts\pythonw.exe`、已安装的引擎与`[desktop]`依赖、Microsoft Edge WebView2 Runtime及Windows .NET运行环境。所选Agent也须有本机可运行入口及自身登录配置。pywebview的Windows依赖说明见[官方安装文档](https://pywebview.flowrl.com/guide/installation.html#windows)。

## 维护与启动

在`engine/`源码目录补装桌面依赖：

```powershell
$enginePython = Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv\Scripts\python.exe'
& $enginePython -m pip install '.[desktop]'
& $enginePython -m jianying_engine workbench --desktop --workspace 'D:\剪辑项目\工作台项目'
```

`[all]`包含音频、跟踪、MCP、网页工作台及桌面窗口；新环境完整安装可用`'.[all]'`。发行时安装对应wheel及所需extras，不拷贝旧虚拟环境、登录目录或模型。构建启动器使用`desktop/build.ps1 -Install -Workspace 'D:\剪辑项目\工作台项目'`，要求本机.NET Framework C#编译器与引擎环境可用；项目入口快捷方式由部署步骤维护。

服务仍仅监听本机回环地址。桌面模式要求EdgeChromium，不自动降级旧浏览器内核。窗口只加载本机工作台地址，不向任意网页暴露Python桥接能力。

## 窗口与服务生命周期

桌面模式实现以下行为，原生实机验收状态见下节：

- 闲置时关窗，保存会话并退出自有服务及当前Agent进程树。
- 执行中、启动中、停止中或等待审批时关窗，先询问是否中断并退出；拒绝则保留窗口和任务。
- 重复启动同一状态目录的桌面实例时，唤醒已有窗口，不另建会话或第二套后台。
- 网页模式与桌面模式共用状态目录时，先正常退出已有模式，再切换；不能让两个实例同时占用同一状态。已有网页实例不会被桌面启动器强行接管或停止。

旧网页命令仍可用：`python -m jianying_engine workbench --workspace 'D:\剪辑项目\工作台项目' --open`，须使用引擎解释器。**关闭网页标签不会退出网页后台；关闭原生窗口则按上述桌面生命周期处理。** 网页后台的身份核对与停止方法见[工作台停止说明](workbench.md#停止与升级)。

## 外置状态与故障

默认状态目录为`%LOCALAPPDATA%\JianyingEngine\workbench`，保存会话、成功连接后的`agents.json`、服务记录、诊断日志与WebView运行数据；CLI可用`--state-dir`指定其他外置目录。`server.json`可能包含本机访问或唤醒令牌，不作为公开安装资源分享。用户素材与成果保存在工作区，账号配置分别由各Agent管理，`agents.json`不保存凭据。

启动器报错时会显示诊断位置；子进程异常退出记录在该状态目录的`launcher.log`，服务细节查看`server.log`。按日志时间区分历史故障与当前状态。升级前关闭窗口及自有服务，保留工作区和状态备份，再更新引擎wheel与启动器。不要按`python`或`codex`进程名批量清理。

## 选型与许可证

本轮优先复用已有Python引擎和HTML界面，所以采用pywebview原生窗口加小型启动器，减少新增运行栈。pywebview 6.2.1的许可证是 **BSD 3-Clause**，不是MIT；版本元数据与源码许可证一致。[PyPI版本](https://pypi.org/project/pywebview/6.2.1/)、[官方许可证](https://github.com/r0x0r/pywebview/blob/6.2.1/LICENSE)。

参考方案：[Electron](https://www.electronjs.org/docs/latest/)内嵌Chromium和Node.js，[许可证为MIT](https://github.com/electron/electron/blob/main/LICENSE)；[Tauri](https://v2.tauri.app/start/prerequisites/)在Windows复用WebView2，但开发链增加Rust和C++构建工具。两者都能承载此界面，本轮已有Python运行环境，尚无迁移前端与发布栈的必要。第三方许可证不改变本项目当前私有本地工具的分发边界。

## 验证状态

0.6新增会话模型/推理/模式设置与恢复、成果自动发现及预览，75项Python测试和11组前端控制器模拟通过。DSH/OpenCode真实协议配置与空会话恢复、确定性Agent通过真实MCP生成合成草稿已验；无付费模型回合。本轮未完成新增界面的实机视觉验收，以下历史窗口验收不替代新增功能的视觉证据。

已核实：启动器已安装，桌面、开始菜单和项目快捷方式均指向该exe并传入指定工作区；源码包含窗口、忙时确认、重复唤醒和关闭清理逻辑。

桌面0.3历史验收：40项Python测试及5组前端模拟断言通过；实机验证WebView2窗口和图标、工作区/DPI适配尺寸、本机Codex连接、文件选择器打开与取消、重复启动复用窗口、闲置关窗后自有进程退出和server.json清理。忙时确认仅模拟验证；原生选择器选中素材的完整流程当时未测。

0.4本轮59项Python测试和8组前端模拟断言通过；DSH/OpenCode真实initialize与session/new成功，无prompt。桌面API顺序连接Codex/OpenCode/DSH均connected；原生窗口已实际选择OpenCode、自动填充参数并显示连接成功，重新启动加载DSH预加载后界面也显示已连接。wheel隔离安装及102个资源一致性检查通过；未验证Gemini/Claude安装、OpenCode实际会话恢复、真实模型剪辑任务或导出。






