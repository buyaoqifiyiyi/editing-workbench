# 迁移适配与维护

用途：换Windows设备、Skill目录/用户名/盘符变化、依赖缺失或旧素材路径失效时自动诊断并处理可确定的路径。复用editor.py doctor、migrate.py，不启用暂存效果、不操作前端、不修改全局系统设置或剪映索引。

## 自动执行的维护流程

1. 从已安装引擎确定runtime目录；脚本和原生资源相对runtime读取，Python使用引擎所在的外部虚拟环境，不在runtime内创建.venv。旧Skill路径只作兼容入口。迁移首用及环境报错时运行doctor，读取ready、publication_ready、migration_instructions、advanced_schema_version_match。
2. doctor自动使用新设备LOCALAPPDATA定位剪映用户数据，读取currentCustomDraftPath；工具依次取明确环境变量、PATH、Skill相对tools/bin，并实际执行-version。旧盘符不可用、显式工具失效、依赖缺失等给出对应设置方法，不切到猜测的目录。不要照搬原电脑的C:/Users/MECHREVO或D盘路径。
3. 若用户已经说明“旧项目根搬到新项目根”且目标可核实，直接将该对应关系交给migrate.py，无需再次确认普通路径转换。相对素材路径按源计划目录解析；明确根映射按最长路径组件匹配，Windows大小写不敏感。目录重名/文件重名不用于自动猜测。
4. 默认只诊断；需要落盘时用--out另存新计划。转换schema_version=1计划内clips/audio的素材路径，以及captions[].font_path的本地字体依赖；保留其他配置和源文件。缺素材或字体不写新计划；已有输出拒绝覆盖。随后validate、build并回读新草稿。路径全部存在不证明素材内容相同，素材来源sidecar须一起保留，build继续核对来源哈希。
5. 若路径映射不明确，列出每个缺失素材的旧路径和预期位置，向使用者索取准确的新素材根目录。只询问无法推断的信息；不能根据同名文件自动替换。若版本不同，分别说明基础生成与高级结构的兼容缺口，另行测试后才能声明迁移通过。

机制适用于新生成计划；已有手工/加密草稿不能用全局字符串替换迁移。使用剪映原生素材重新链接或保留旧路径进行单独恢复/兼容核验；不把UI成功、恢复文件或后台回读当成完整成片质量验收。生成的草稿引用原素材路径；本地字体则在build时复制到草稿`Resources/local-fonts/`并由publish重绑定，删除原字体后发布副本仍保留技术依赖，但计划重建仍需要原字体或明确映射后的来源。只复制草稿目录到另一台机器，普通素材可能显示离线，优先在新设备按迁移后的计划重新build/publish。

## 搬迁哪些文件

在新设备的外部虚拟环境安装引擎wheel或项目，再单独迁移需要的项目计划、素材和素材来源文件。薄Skill入口可另行部署，不复制其中的Junction到新设备。**不要复制旧.venv作为新设备运行环境**；不要随包分享config.env里的私人API密钥、账号数据或缓存。配置文件按新使用者自己的凭证设置。同一用户迁移保留会员使用授权，但账号登录、资源下载、字体许可和新账号权限不由本机制代办或保证。

历史研究记录中的本机绝对路径只作证据引用。要带走研究样本，另行迁移对应工作区及恢复清单；旧设备归档引用不存在时如实说明，不搜索全盘或自动恢复全部实验。暂存清单和失败状态一并迁移，不自动启用未接入功能。

## 使用者如何设置

下面变量分别指向引擎runtime目录与外部虚拟环境的Python。实际路径替换为新设备位置，不照搬原机目录。

```powershell
$skillRoot = 'E:\引擎环境\Lib\site-packages\jianying_engine\runtime'
$skillPython = 'E:\引擎环境\Scripts\python.exe'
# 先在外部虚拟环境中安装引擎wheel或项目，勿在runtime目录创建环境。
& $skillPython "$skillRoot\scripts\editor.py" doctor
```

引擎环境缺依赖时，用该外部环境的Python运行 `-m pip install -r <runtime>/scripts/requirements.txt -c <runtime>/scripts/constraints.txt`。包内setup.ps1会拒绝创建runtime/.venv；通过旧Skill路径运行setup仍只处理其旧环境。依赖安装需要网络或预备wheel；不自动升级Python、FFmpeg、剪映，不修改全局Python。

当FFmpeg不在PATH时，设置当前进程变量。旧Skill路径调用兼容其tools/bin，但工具二进制不进入wheel：

```powershell
$env:JIANYING_FFMPEG = 'E:\工具\ffmpeg\bin\ffmpeg.exe'
$env:JIANYING_FFPROBE = 'E:\工具\ffmpeg\bin\ffprobe.exe'
# 只在使用非默认剪映用户数据位置时设置：
# $env:JIANYING_USER_DATA = 'E:\剪映用户数据\User Data'
& $skillPython "$skillRoot\scripts\editor.py" doctor
```

这些变量不会写入系统环境或剪映配置；后续新进程需继承它们或再次设置。需要长期使用时由使用者自行加入用户PATH，或者保留相对tools/bin。旧盘草稿目录失效时，在剪映设置中选择本机实际可用的保存目录，再运行doctor；不要自动改配置或把首页索引目录当草稿扫描目录。

诊断和根路径映射示例：

```powershell
# 环境诊断：输出缺口及设置方法，不修改文件。
& $skillPython "$skillRoot\scripts\migrate.py"
# 先看转换结果，不写入：
& $skillPython "$skillRoot\scripts\migrate.py" --plan 'E:\迁移项目\edit-plan.json' --map 'D:\旧项目=E:\迁移项目'
# 全部素材存在时另存新计划；可重复--map添加其他明确旧根。
& $skillPython "$skillRoot\scripts\migrate.py" --plan 'E:\迁移项目\edit-plan.json' --map 'D:\旧项目=E:\迁移项目' --out 'E:\迁移项目\v2\edit-plan.json'
& $skillPython "$skillRoot\scripts\editor.py" validate --plan 'E:\迁移项目\v2\edit-plan.json'
& $skillPython "$skillRoot\scripts\editor.py" build --plan 'E:\迁移项目\v2\edit-plan.json' --out 'E:\迁移项目\v2\草稿'
```

migrate退出码0表示本次环境/路径检查通过，1是输入或写入错误，2表示后台环境或素材仍有缺口；它不证明实际打开/渲染。--out仅代表请求另存计划，不自动安装依赖、发布草稿或修改已有工程。doctor即使后台ready仍可能publication_ready=false或ui_compatibility=unverified，按对应范围报告。

## 可选离线音频依赖

基础引擎依赖不包含转写与分离运行时。需要这两项时，在同一个外部虚拟环境显式安装已验证版本，沿用基础依赖约束，避免升级已有剪辑依赖：

```powershell
& $skillPython -m pip install -r "$skillRoot\scripts\requirements-offline-audio.txt" -c "$skillRoot\scripts\constraints.txt"
& $skillPython -m pip check
```

模型仍由调用者放在 Skill 外并通过 `--model` 传入；安装包不等于模型推理通过。运行 `run_checks.py` 时须同时提供 `--demucs-model`、`--whisper-model` 及四项外部实测计划，逐项核对 `failed` 与 `skipped`，不能仅凭退出码 0 宣称全部通过。安装可能需要联网，转写与分离执行仍使用本地模型。若 `.venv` 是 Junction，安装作用于其目标环境，不另建或覆盖环境。

## 可选离线跟踪依赖

离线单目标跟踪另需 `scripts/requirements-tracking.txt`（在引擎外部虚拟环境中用 `pip install -r ... -c scripts/constraints.txt` 安装）。只选一个 OpenCV 包，避免四种 `cv2` 包互相覆盖；现有环境已装其它 OpenCV 变体时先检查 CSRT 是否可用，不自动卸载用户依赖。PyAV 用真实 PTS 解码，CSRT 不下载模型。安装后执行 `run_checks.py --only tracking tracking_apply coverage`；缺依赖的跳过不代表跟踪通过。

## 版本和验证

当前高级模板取自剪映11.5.0.14471，后端固定pyJianYingDraft 0.3.0。doctor从注册表读到匹配版本，只证明匹配取样版本，不证明实际运行版本或所有效果显示正确。不同版本不自动升级/降级；重新检查兼容性，保留失败证据。

已实测的版本兼容状态（证据目录见“变更记录”，不代替异机验收）：11.5.0.14471为取样基线，该安装现已不在本机，旧哈希只作历史记录；11.6.0.14552在2026-10-16完成基础生成回归（14项通过、4项因缺外部实测计划跳过），同日补齐降噪/响度/缓动/已有遮罩缓存四项的外部实测计划后全量run_checks为18项通过、0失败、0跳过（四项计划的真实素材来源、判定数据出处与未覆盖边界见工作区`运行数据\外部实测计划\来源说明.md`，变更记录见`Skill维护层\变更\2026-10-16-补齐四项外部实测计划-v1`；这些仍是结构级回归，含已有缓存复用而非新计算，界面保存重开与渲染另验）、高级结构生成与原生静帧预览渲染，同时确认11.6把UI保存过的草稿写成加密字节，后台无法读取，打开/编辑与完整视听仍由使用者验收（后台在publish与native-preview入口按字节判定明文/加密/损坏/缺失并具名拒绝，不解密不覆盖，自检`scripts/draft_state_self_check.py`）。

回归必须使用实际交付环境的解释器。2026-10-18 较早记录中的 23 通过 / 2 跳过，来自未装离线音频依赖的 Skill 环境；两个检查在探针环境单跑通过，属于不同解释器证据，不能拼成一次全量通过。本轮依赖安装与同一解释器的最新回归记录见工作区 `Skill维护层/变更/2026-10-18-运行环境与入口对齐-v1`。历史记录还出现过 `bad allocation`，只能说明当次分配失败；`run_checks.py` 实际为每项启动独立子进程，不能据旧描述断言两个模型在同一检查进程中累积。内存不足时保留失败报告，按当前运行条件诊断，不改阈值或把失败改成跳过。

维护时运行`migration_self_check.py`验证路径转换、冲突/缺素材拒绝、旧目录失效提示与转换后生成；修改共享工具定位时同时运行self_check.py和native_self_check.py；增删或改写`assets/native-preview-<版本>.json`、换剪映版本时运行`native_preview_self_check.py --install <安装目录>`（`run_checks.py`已含native_preview项，默认取本机发现且有对应版本清单的安装目录，也可用`--install`指定）。测试输出放新工作区，不发布到用户草稿区。

本机制首次自检为本机模拟迁移，不是第二台设备验收。异机验收至少分别记录环境可运行、计划/素材有效、新草稿结构与参数回读、实际剪映打开/可编辑性；最终完整视听和导出仍由用户完成。
