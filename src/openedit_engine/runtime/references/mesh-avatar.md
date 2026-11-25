# Mesh Avatar 真 2D 网格 Live2D 角色流程与规范

先用 `openedit mesh-avatar status`（或 `openedit doctor` 的 `mesh_avatar` 字段）确认本机是否已配置并就绪 **Mesh Avatar Studio**（MIT WebGL2 引擎，自动复用 `%LOCALAPPDATA%\OpenEditEngine\mesh-avatar.json` 与本机 Chrome/Edge）。**绝不在已有 `mesh-avatar` 可用时手搓整图 UV 波浪或像素假眨眼滤镜替代真实骨骼网格动画。**

- **适用对象边界**：适用于**正面或微侧半身透明立绘**；若原图带复杂背景先准备透明底（可用 `matting`）；**90° 纯侧脸或背影镜**不适合硬绑正面眼鼻骨架，应保留刚体立绘配合 `layer_motion_blur` + `light_wrap --bg`。

## 一、单张正面透明立绘零 ImageGen 直出短路径（最常用）

1. 在 Studio 内用 `tools/new-project.py <source.png> <project_name>` 创建工程，严格按 Studio `docs/agent-guide.zh.md` 生成 5px/20px 坐标网格并精标 `rig.draft.json`（多边形眼白、睫毛 ROI、发根权重锁死 `0` 的 4 节点发束弹簧链、双肩排除范围、颈椎支点；注意 `mouth.angle` 等角度字段使用**弧度制**）。
2. 执行 Studio `tools/build-layers.py` 拆出独立眼球/上下睫毛/眼眶补肤色底图与 `hairmask`，通过视觉代理核对叠加预览与分层图彻底隔离衣领与脸部后，运行 `npm run pack-avatar` 与 `npm run validate-avatar` 生成 `.mavatar`。
3. 直接用 `openedit mesh-avatar render` 无头输出匹配成片规格的透明视频（默认 `1280x720 @ 30fps png`，可按主时间轴指定 `--width 1920 --height 1080 --fps 30|60 --codec prores_4444`，自动发现本机浏览器并完成 `ffmpeg -v error -xerror` 全解码检查；动作关键帧保持 `3.2–4.6s` 舒缓呼吸与自然眨眼，切忌高频振荡），再通过 `openedit mesh-avatar attach`（支持任意合法画布）或 `edit-plan.json` 挂入主时间轴。

## 二、原图与原视频情境变体流程（需授权使用 ImageGen 时）

用户提供有权使用的原角色 PNG 和目标视频。原图是身份、服饰、发型、画风和姿势基准；视频只提供被选择的情境帧。流程不从视频抠出人物，也不把视频帧整张重绘成角色。

Studio 上游代码为 MIT，角色资产许可另计。使用本机已构建的 Mesh Avatar Studio，不自动安装/下载。初次配置：`mesh-avatar configure --studio <Studio目录>`。原图、视频和生成素材存到项目目录；不要覆盖原图或已有项目。

先在本机查看目标片段，抽取候选帧联系表：

    openedit mesh-avatar inspect-video --video <视频> --out <新分析目录> --start 0 --duration 12 --samples 12

每段只取 3–20 帧。均匀候选用于定位，不能替 Agent 判断表情或表演。Agent 查看联系表和原视频后，选择实际出现、值得制作的时间点与眼/嘴表现。

从角色 PNG 创建新的 Studio 项目，并从已选择的时间点抽少量情境帧：

    openedit mesh-avatar prepare-character --image <角色.png> --video <视频> --times <选中秒数...> --name <新项目名>

先依 Studio `docs/agent-guide.zh.md` 完成视力校准、rig 坐标、`build-layers.py` 和基本 pose review。**Rig 与 Mesh 图层构建后**再生成变体遮罩：

    openedit mesh-avatar prepare-variants --project <Studio项目目录>

该操作调用 Studio `variant-requests.py`，遮罩依据真实 rig 生成，并输出 `video-context-requests.json`。Agent 查看视频情境后，按变体把 `context_frame_indices` 记入选择 JSON，再运行：

    openedit mesh-avatar plan-variants --project <Studio项目目录> --selection <变体选择.json>

每条变体须记录名称、对应视频帧索引和画面观察到的表情。该操作校验源视频、原图、身份底图、遮罩和帧哈希，输出逐次调用清单。Agent 必须对每个选中的变体调用当前 ImageGen 工具，每次只传：原角色 `source.png`、一个对应 Mesh `mask.png`、该变体关联的少量已选视频帧。**不要把整段视频、其他无关帧或整个 Mesh 项目传给 ImageGen。** 视频帧是表情/情境参考，不能把背景、构图、全身姿势或镜头风格复制进角色图。

提示要求保持原角色身份、服饰、发型、线条、色彩、尺寸和定位，只改遮罩允许的眼/嘴区域。结果保存为清单指定的 `variants/<name>.png`。接着执行：

    openedit mesh-avatar build-character --project <Studio项目目录>

Studio `build-sprites.py` 会检查并仅导入遮罩区域，遮罩外变化或错位过大时拒收。`build-character` 生成固定 pose review 图；Agent 必须逐图查看原姿势、闭眼/张嘴、转头等关键状态，有明显脸部/发型变形就返修。确认变体图和 pose sheet 后，为每个变体写入清单指定的 `acceptance_receipt` JSON，字段为 `variant`、`image_sha256`、`mask_sha256`、`mesh_build_sha256`、`accepted_outside_mask: true`、`reviewed_pose: true`；哈希分别取最终变体 PNG、清单遮罩和 `built/sprites/sprites.json`。这是审阅记录，不是自动质量证明；图片或 Mesh build 改变后必须重新检查并更新回执。只有全部回执与当前文件哈希吻合，`pack-character --require-sprites` 和项目目录 render 才允许继续。之后打包、validate-avatar，再以显式局部动作关键帧渲染 1280×720、30fps、最多 300 帧的透明 PNG/RGBA MOV，并用原计划的副本创建新开源时间轴计划；原计划不覆盖。

ImageGen 每次只接收用户已授权的原图、一个对应遮罩和被选视频帧。授权只适用于当前这个项目；新人物或新视频需重问外发范围。Mesh 遮罩像素检查不能证明视频语义匹配，模型输出需要 Agent 查看。这个流程不自动理解歌词、选择音乐卡点或保证真实语音口型同步。导入 FilmCraft `.fcproj` 合成、预览静帧与导出严格解码分别验收；结构/静帧不能替代人类最终观片。

已有 `.mavatar` 的透明渲染兼容入口仍可直接使用 `mesh-avatar render --avatar <角色.mavatar> ...`。角色变体是 Mesh 可用的局部绘制素材，头身运动由 Mesh 参数与显式关键帧生成；它不会复现任意视频动作。
