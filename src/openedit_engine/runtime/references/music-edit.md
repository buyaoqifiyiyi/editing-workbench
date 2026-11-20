# 原生视听与音乐驱动剪辑

适用于用户已指定BGM、要求依据镜头内容和音乐意境自动编排或动作卡点。共用`music-edit` CLI/MCP白名单；接口协议、模型 ID 和地址均由当前配置决定。所选模型会直接接收BGM音频和连续视频，不经过ASR/图片描述替代其视听输入。普通Agent负责工具调用与后端执行，视听模型负责声画理解与选段建议；模型时间仍是估计。

## 配置与授权

通过本机专用配置提供协议、服务端模型 ID 和接口根地址。当前支持：

- `openai-compatible`：使用 `{base_url}/chat/completions`，媒体放在音视频内容块中；适用于提供该媒体格式的兼容服务。
- `gemini`：使用 Gemini 原生 `{base_url}/models/{model}:generateContent`，媒体通过 `inlineData` 传入；密钥放入请求头，不进入 URL。

两种协议都使用结构化 JSON 返回。模型 ID 没有内置默认值；切换服务时必须核对所选模型确实支持音频和视频输入。高级请求参数按当前协议进入请求体或 `generationConfig`，不能覆盖模型、媒体内容、认证和返回格式。

Qwen3.8-Omni-Flash的官方JSON Mode示例使用高级参数 `{"reasoning_effort":"none"}` 关闭思考模式；需要该模式时由用户配置保留，通用引擎不按模型名称强行添加参数。

专用配置为 `%LOCALAPPDATA%/JianyingEngine/omni.json`，可由 `JIANYING_OMNI_CONFIG` 或 CLI 全局 `--config` 指定包外文件。普通配置只存 enabled、protocol、model、base_url、api_key_env 和 request_options。旧工作台保存的 `omni.key` 由 Windows 当前用户 DPAPI 加密，引擎仍可直接读取。新配置可使用进程环境变量密钥；两者同时存在时优先使用已保存密钥。旧格式缺协议和模型时须在专用配置补齐。API 根地址与密钥区域须由用户自己的控制台确认。密钥留在专用环境或加密文件中。

```powershell
& '<引擎Python>' -m openedit_engine music-edit configure --protocol openai-compatible --model '<兼容服务的模型 ID>' --base-url '<服务的兼容 API 根地址>' --api-key-env MULTIMODAL_API_KEY
& '<引擎Python>' -m openedit_engine music-edit doctor
```

用 `doctor` 检查本机配置和密钥可读性。`configure` 用于创建新配置；已有文件时由用户按需更新其专用配置。环境变量密钥须对执行引擎的进程可见。未就绪时报告具体缺项。调用 `analyze` 会将选定媒体发送给配置的服务并产生 API 用量。通过排他创建 analysis-attempt.json 占用本包的唯一付费调用，防并发重复发送；HTTP 失败或超时不自动重试，也不在同一包重复调用。失败后保留 attempt 与能取得的 usage，确认状态后另建版本；不记录响应错误正文和密钥。只使用用户已明确配置的服务。

## 从指定BGM到计划

1. 用已有inspect/reference查看候选，Agent根据素材规模分批选出相关连续源范围；文件夹不自动全量上传。写包外request.json，明确目标、BGM选区和候选视频选区。
2. `prepare`生成本机媒体包：BGM的48kHz立体声WAV供计划和节拍检测，MP3供原生听觉输入；候选视频转小尺寸MP4供原生视觉/听觉输入，保留各自从0开始的局部时钟和原素材偏移/SHA。不提取字幕或图片说明冒充输入。
3. 配置就绪后`analyze`一次性提交BGM及所选视频。返回音乐分段、按内容排列的镜头、理由及可选动作锚点，记录实际usage。可解析的最终JSON保存在`analysis-response.json`供诊断，不保存思考过程；只有通过字段/时间/源范围校验才生成可编译的`analysis.json`。部分输出、未知源ID、无效时间、未覆盖BGM均拒绝。已有结果复用，不自动重新调用。
4. 对包内`media/music.wav`用已有`beats`取得真实候选，再`compile --beats`。程序仅将附近的模型切点/音乐事件吸附到候选，默认最大偏移80ms；不为每个节拍新增切镜。动作锚点通过调整源入点使动作发生在音乐目标帧，非简单地把镜头开头放到鼓点。所有边界量化到30fps；越过已分析源选区、量化后镜头消失等情况拒绝，由Agent调整候选重新分析，不放宽守卫。
5. compile 生成纯开源 `openedit` / `filmcraft` 编辑计划（`--backend openedit` 或 `--backend filmcraft`），并调用 `runtime_core.validate_plan` 校验；随后 Agent 用 `openedit build` 编排 `.fcproj` 与成片，结果回看再修订。alignment.json 记录音乐段落、选镜理由、实际源帧/目标帧及源事件量化误差。关键动作时间是模型估计，误差报告仅表示程序量化，不证明模型找准了动作。

```json
{
  "schema_version": 1,
  "goal": "先铺垫关系，再让动作命中响应BGM释放，保留动作准备与结果",
  "music": {"path": "D:/项目/bgm.mp3", "start": 12, "duration": 16},
  "sources": [
    {"id": "shot_a", "path": "D:/项目/素材一.mp4", "start": 30, "duration": 20},
    {"id": "shot_b", "path": "D:/项目/素材二.mp4", "start": 10, "duration": 20}
  ]
}
```

```powershell
& '<引擎Python>' -m openedit_engine music-edit prepare --request 'D:/项目/request.json' --out 'D:/项目/perception-v1'
& '<引擎Python>' -m openedit_engine music-edit analyze --bundle 'D:/项目/perception-v1'
& '<引擎Python>' -m openedit_engine beats --input 'D:/项目/perception-v1/media/music.wav' --duration 16 --out 'D:/项目/beats-v1'
& '<引擎Python>' -m openedit_engine music-edit compile --bundle 'D:/项目/perception-v1' --beats 'D:/项目/beats-v1' --backend filmcraft --out 'D:/项目/music-plan-v1'
```

MCP对应`start_job(operation="music-edit", arguments=["prepare", "--request", "<绝对路径>", "--out", "<新目录>"])`，其他子命令同理；轮询成功后读输出。旧长驻MCP未发现该操作时使用当前CLI，不重启共享会话。无需用Gemini CLI或ACP图像prompt转发实际音视频；发送发生在本机引擎专用入口。

## 成片全模态审片

`music-edit review --bundle <包>`沿用prepare生成的真实音视频与当前专用API配置，提交连续成片及其自带音轨；附带BGM仅供参考。prepare的sources应选择要审查的带声音成片，music选对应配乐范围；每条上传视频的局部时钟从0开始。支持同包比较版本，原文件身份和已有媒体边界保持。

高清/快速变化短段使用`prepare --video-profile review`：按原尺寸保留到最大1920×1080、不上采样低清源，30fps H264 CRF18/6Mbps上限与192kbps AAC；旧默认proxy的640×360/280kbps保持。文件与总Base64体积限制不放宽，过大时缩短选区。profile与video_fps记录在manifest；Gemini原生请求对review视频默认显式写`videoMetadata.fps=8`，也可用`--video-fps 1|8`作同媒体采样对照；音频块和OpenAI兼容请求不附这个字段。官方生成接口默认为1fps；是否由中转端遵循8fps仍须实际验证，不能从源视频30fps推定模型逐帧观看。参考[VideoMetadata](https://ai.google.dev/api/generate-content#VideoMetadata)。

返回兼容仅去掉覆盖整个最终文本的单层json或无语言Markdown围栏；外围解释、多段围栏、错误JSON、重复键、非有限数值、未正常结束响应仍拒绝，后续source_id/时间/结构校验保持。此兼容不修改历史失败回执，也不触发自动重新调用。

高速局部可在prepare显式加`--frame-times 0.5 0.7 0.85 1.0 1.12 1.5`，单位为所选视频内的局部秒数。本例仅展示格式，实际时刻应覆盖当前动作的进入、中间和结束。要求review profile、恰好一个视频、1..8个严格递增且早于选区结束的有限时间；从原选区提取最大640×360且不上采样的JPEG，与完整音视频一起输入模型，并标出source_id及所请求时间。源非整帧时刻存在解码量化，不把标注秒数称为测量的帧精度。

补充帧保存在manifest.frames，纳入哈希、目录边界与单文件/总Base64体积检查；Gemini使用image/jpeg inlineData，OpenAI兼容使用image_url。参数入口、两协议请求字节及非法时间/篡改拒绝由回归验证；图片、视频和音频是联合输入，不能把模型对补充帧的识别称为独立连续视频理解。没有补充帧的旧包和调用保持兼容。

输出review.json的summary与带source_id/局部起止时间、类别、严重度、具体见闻、建议和信心的observations。review-attempt.json排他占用一次调用，失败不自动重试；报告记录实际模型/协议/主机/usage及输入输出哈希，不保存密钥或思考。审片结果不能compile成时间轴，不能自动改工程或替代用户认可。模型采样、低清代理和音频感知均有限，重要问题应回看原生画面并以可用音频测量核实；模型漏报不代表没有问题。

已接通且当前任务授权使用视听服务时，在代表性短段制作后、向用户交付候选前调用review；真实内容修改后审查新的带声音版本，无变化时复用报告。不能用早期素材analyze、纯结构检查、静帧或解码成功替代当前版本的声画审片。未能调用须明确缺口，不默认为审片通过。

## 当前范围与证据

单次1..8个候选视频选区，每段2..120s、总计≤600s；BGM选区2..180s，时长需为30fps整帧。每份编码媒体小于9MiB，总编码媒体≤18MiB，兼顾Gemini内联请求边界；准备前先明确缩小源范围。这里是本地实施边界，不是模型最大上下文。分析代理视频最长边约640像素、30fps，模型服务仍可能内部抽样，快速动作需关键短段补看。未支持自动索引整部电影或无界文件夹。

模型只提出source_id和局部时间，编译器映射回原视频，不执行模型返回的代码或路径。原视频按纯开源后端准入：若源视频为手机 VFR 建议先 `normalize-media`；计划默认生成 `30fps` 规范供 FilmCraft 编译，具体扩展字段参见 [能力说明](capabilities.md) 与 [计划规范](plan-format.md)。计划音轨引用包内music.wav，整个感知包作为项目依赖保留，不删除它。BGM增益当前保持1，原视频音量0；需要对白/原声时由Agent按原接口另行安排。

本地媒体构造、两种协议的真实编码内容/认证/请求/响应、模拟模型回执、动作映射和两后端计划校验属于技术接线。真实云端调用、音乐意境匹配、动作识别、正常速度连续视听、宿主保存重开及最终导出分别验收；没有API配置或未用当前模型实测时不声称内容匹配或审美通过。该增量不结清剪映原生beat markers缺口。

来源：[Gemini generateContent](https://ai.google.dev/api/generate-content)、[Gemini视频理解](https://ai.google.dev/gemini-api/docs/video-understanding)、[OpenAI兼容音视频示例](https://docs.qwencloud.com/developer-guides/speech/multimodal-speech)。仅复用公开协议说明，未复制或安装第三方插件代码；云模型服务授权/计费按用户账户。
