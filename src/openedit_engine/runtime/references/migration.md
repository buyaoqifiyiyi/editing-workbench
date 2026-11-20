# 迁移适配与维护

用途：更换设备、目录/盘符变动、离线模型权重迁移或环境缺依赖时，安全诊断并重建纯开源剪辑工程（FilmCraft + EffectCraft + FFmpeg）。复用 `openedit doctor`、`openedit migrate` 与 `openedit checks`，绝不修改用户私人数据或覆盖已有输出目录。

## 素材路径迁移

当工程素材根目录从旧路径移动到新路径（例如由另一块盘符或他人交付目录迁移）时，按以下规范执行确定性路径重映射：

1. **查看迁移状态与缺口**：先不传 `--out` 进行纯诊断，确认实际失效的媒体与字体引用；`migrate` 支持相对路径按源计划目录解析，明确根映射 `--map 旧根=新根` 按最长路径组件匹配（Windows 路径大小写不敏感，允许重复多次 `--map`）。**绝不根据同名文件猜测替换**，映射不明确时直接列出具体缺失路径向使用者确认。
2. **写入迁移后计划并复验**：确认所有源媒体和字体（`clips[].path`、`audio[].path`、`captions[].font_path`）都在目标位置存在后，传入 `--out` 生成新版 `edit-plan.json`（保留原 plan 不变；目标若已存在则拒绝覆盖），随后重新执行 `validate` 与 `build` 从明文规范生成全新 `.fcproj` 工程：

```powershell
# 1. 环境诊断
openedit doctor
# 2. 只读诊断并预览根路径映射（不落盘）
openedit migrate --plan 'E:\迁移项目\edit-plan.json' --map 'D:\旧项目=E:\迁移项目'
# 3. 核对全部素材与字体存在后写出新版计划，并重编工程
openedit migrate --plan 'E:\迁移项目\edit-plan.json' --map 'D:\旧项目=E:\迁移项目' --out 'E:\迁移项目\v2\edit-plan.json'
openedit validate --plan 'E:\迁移项目\v2\edit-plan.json'
openedit build --plan 'E:\迁移项目\v2\edit-plan.json' --out 'E:\迁移项目\v2\project'
```

- 路径存在不代表文件内容完全相同：**请随素材一并迁移 `.provenance.json` sidecar**，`build` 会逐个校验文件 `sha256` 摘要，防止同名错片或静默替换。
- 退出码语义：`0` 表示全部路径/环境检查通过；`1` 表示参数非法或输出写入冲突；`2` 表示环境工具或计划内素材/字体仍存在缺口。

## 新设备环境配置与依赖安装

在新设备的外部 Python 虚拟环境中安装 `openedit-engine`（推荐 Python 3.12+，勿在包内部创建 `.venv`）：

```powershell
# 查看当前解释器、运行时目录与指南位置
openedit paths
# 安装纯开源引擎及常用工具组（按需选用 [mcp],[rhythm],[audio],[tracking],[exchange],[all]）
pip install -e '.[mcp,all]'
```

若 `FilmCraft`、`EffectCraft` 或 `FFmpeg` 未置于默认安装路径或系统 `PATH`，可在当前会话显式设置进程环境变量后重新执行 `openedit doctor` 核验：

```powershell
$env:OPENEDIT_FILMCRAFT_CLI = 'C:\Tools\FilmCraft\filmcraft-cli.exe'
$env:OPENEDIT_EFFECTCRAFT_CLI = 'C:\Tools\EffectCraft\effectcraft-cli.exe'
$env:OPENEDIT_FFMPEG = 'C:\Tools\ffmpeg\bin\ffmpeg.exe'
$env:OPENEDIT_FFPROBE = 'C:\Tools\ffmpeg\bin\ffprobe.exe'
openedit doctor
```

## 可选离线音频与跟踪依赖

- 基础运行时不包含大体积 AI 权重。离线语音转写（`transcribe`）与人声分离（`separate`）需先在引擎外部虚拟环境中按约束安装 `scripts/requirements-offline-audio.txt`（配合 `-c scripts/constraints.txt`），并在调用时通过 `--model` 传入本地 Whisper / Demucs ONNX 权重路径。
- 离线单目标跟踪（`tracking`）需按 `scripts/requirements-tracking.txt` 安装单一 OpenCV contrib 变体（确保 `cv2.TrackerCSRT_create` 可用）与 PyAV，避免混装多种 `opencv-*` 包冲突。

## 打包与跨机交接清单

- **应该带走**：项目 `edit-plan.json`、原始音视频与图片素材、字体文件、`*.provenance.json` sidecar、生成的 `.fcproj` / `.ecproj` 及 `.otio` / `.fcpxml` 互换工程、交付文档（`剪辑交付.md` / `素材来源.md`）。
- **不要打包复制**：旧机器虚拟环境目录、私人 API Key / `omni.key` 凭据文件、临时渲染缓存。换机后在新环境重新 `openedit validate` 与 `openedit build`，并重新运行 `openedit checks` 验证新机运行时一致性。
