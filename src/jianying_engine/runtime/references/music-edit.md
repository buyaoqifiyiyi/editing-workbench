# 原生视听与音乐驱动剪辑

适用于用户已指定BGM、要求依据镜头内容和音乐意境自动编排或动作卡点。共用`music-edit` CLI/MCP白名单；接口协议、模型 ID 和地址均由当前配置决定。所选模型会直接接收BGM音频和连续视频，不经过ASR/图片描述替代其视听输入。普通Agent负责工具调用与后端执行，视听模型负责声画理解与选段建议；模型时间仍是估计。

## 配置与授权

在工作台顶部点击“视听 API”，选择接口协议、填写服务端模型 ID、接口根地址和API Key，勾选启用并保存。当前支持：

- `openai-compatible`：使用 `{base_url}/chat/completions`，媒体放在音视频内容块中；适用于提供该媒体格式的兼容服务。
- `gemini`：使用 Gemini 原生 `{base_url}/models/{model}:generateContent`，媒体通过 `inlineData` 传入；密钥放入请求头，不进入 URL。

两种协议都使用结构化 JSON 返回。模型 ID 没有内置默认值；切换服务时必须核对所选模型确实支持音频和视频输入。高级请求参数按当前协议进入请求体或 `generationConfig`，不能覆盖模型、媒体内容、认证和返回格式。密钥栏留空保留已有密钥，勾选“清除已保存的密钥”可移除本机保存项；页面重开时不回显。保存/检查只验证本机配置，不发送素材、不调用模型，也不证明余额或API连通性。

专用配置为`%LOCALAPPDATA%/JianyingEngine/omni.json`，可由`JIANYING_OMNI_CONFIG`或CLI全局`--config`指定包外文件。普通配置只存enabled、protocol、model、base_url、api_key_env和request_options。页面填写的密钥单独经Windows当前用户DPAPI加密，保存到同目录`omni.key`，不进入会话、普通配置或状态回显；引擎在实际调用时解密。API根地址与密钥区域须由用户自己的控制台确认。仍兼容进程环境变量密钥；两者同时存在时优先使用已保存密钥。旧三字段配置会保留加密密钥，但不推断模型，页面提示重新选择协议和模型。不读取其他Agent凭据，不将密钥放任务单、计划、CLI参数或日志。

```powershell
& '<引擎Python>' -m jianying_engine music-edit configure --protocol openai-compatible --model '<兼容服务的模型 ID>' --base-url '<服务的兼容 API 根地址>' --api-key-env MULTIMODAL_API_KEY
& '<引擎Python>' -m jianying_engine music-edit doctor
```

doctor只报告配置/密钥可读性，不联网、不验证余额或模型权益。配置默认不存在；CLI configure显式启用，不覆盖已有配置；页面保存可更新端点、启用状态及加密密钥。页面保存后引擎子进程直接读取，无需修改环境变量或重启；仅修改环境变量时，工作台及其Agent/引擎子进程需在正常退出后重新启动才能继承；不要强退其他任务。未就绪时报告缺少配置，不重复请求密钥、不假称已听过BGM。调用`analyze`会将选定媒体发送给配置的服务并产生API用量；其他步骤不调用模型。通过排他创建analysis-attempt.json占用本包的唯一付费调用，防并发重复发送；HTTP失败或超时不自动重试，也不在同一包重复调用。失败后保留attempt与能取得的usage，确认状态后另建版本；不记录响应错误正文和密钥。只使用用户已明确配置的服务，不扩展到其他付费模型。

## 从指定BGM到计划

1. 用已有inspect/reference查看候选，Agent根据素材规模分批选出相关连续源范围；文件夹不自动全量上传。写包外request.json，明确目标、BGM选区和候选视频选区。
2. `prepare`生成本机媒体包：BGM的48kHz立体声WAV供计划和节拍检测，MP3供原生听觉输入；候选视频转小尺寸MP4供原生视觉/听觉输入，保留各自从0开始的局部时钟和原素材偏移/SHA。不提取字幕或图片说明冒充输入。
3. 配置就绪后`analyze`一次性提交BGM及所选视频。返回音乐分段、按内容排列的镜头、理由及可选动作锚点，记录实际usage。JSON通过客户端字段/时间/源范围校验才保留；部分输出、未知源ID、无效时间、未覆盖BGM均拒绝。已有结果复用，不自动重新调用。
4. 对包内`media/music.wav`用已有`beats`取得真实候选，再`compile --beats`。程序仅将附近的模型切点/音乐事件吸附到候选，默认最大偏移80ms；不为每个节拍新增切镜。动作锚点通过调整源入点使动作发生在音乐目标帧，非简单地把镜头开头放到鼓点。所有边界量化到30fps；越过已分析源选区、量化后镜头消失等情况拒绝，由Agent调整候选重新分析，不放宽守卫。
5. compile按当前软件生成剪映或Resolve既有计划，并调用其原有校验器；随后Agent用原有build或resolve-workflow执行，结果回看再修订。alignment.json记录音乐段落、选镜理由、实际源帧/目标帧及源事件量化误差。关键动作时间是模型估计，误差报告仅表示程序量化，不证明模型找准了动作。

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
& '<引擎Python>' -m jianying_engine music-edit prepare --request 'D:/项目/request.json' --out 'D:/项目/perception-v1'
& '<引擎Python>' -m jianying_engine music-edit analyze --bundle 'D:/项目/perception-v1'
& '<引擎Python>' -m jianying_engine beats --input 'D:/项目/perception-v1/media/music.wav' --duration 16 --out 'D:/项目/beats-v1'
& '<引擎Python>' -m jianying_engine music-edit compile --bundle 'D:/项目/perception-v1' --beats 'D:/项目/beats-v1' --backend resolve --out 'D:/项目/music-plan-v1'
```

MCP对应`start_job(operation="music-edit", arguments=["prepare", "--request", "<绝对路径>", "--out", "<新目录>"])`，其他子命令同理；轮询成功后读输出。旧长驻MCP未发现该操作时使用当前CLI，不重启共享会话。无需用Gemini CLI或ACP图像prompt转发实际音视频；发送发生在本机引擎专用入口。

## 当前范围与证据

单次1..8个候选视频选区，每段2..120s、总计≤600s；BGM选区2..180s，时长需为30fps整帧。每份编码媒体小于9MiB，总编码媒体≤18MiB，兼顾Gemini内联请求边界；准备前先明确缩小源范围。这里是本地实施边界，不是模型最大上下文。分析代理视频最长边约640像素、30fps，模型服务仍可能内部抽样，快速动作需关键短段补看。未支持自动索引整部电影或无界文件夹。

模型只提出source_id和局部时间，编译器映射回原视频，不执行模型返回的代码或路径。原视频按既有后端准入：Resolve仍要求30fps/CFR及其他既有约束；不因生成建议而准入变速、字幕、复杂特效。计划音轨引用包内music.wav，整个感知包作为项目依赖保留，不删除它。BGM增益当前保持1，原视频音量0；需要对白/原声时由Agent按原接口另行安排。

本地媒体构造、两种协议的真实编码内容/认证/请求/响应、模拟模型回执、动作映射和两后端计划校验属于技术接线。真实云端调用、音乐意境匹配、动作识别、正常速度连续视听、宿主保存重开及最终导出分别验收；没有API配置或未用当前模型实测时不声称内容匹配或审美通过。该增量不结清剪映原生beat markers缺口。

来源：[Gemini generateContent](https://ai.google.dev/api/generate-content)、[Gemini视频理解](https://ai.google.dev/gemini-api/docs/video-understanding)、[OpenAI兼容音视频示例](https://docs.qwencloud.com/developer-guides/speech/multimodal-speech)。仅复用公开协议说明，未复制或安装第三方插件代码；云模型服务授权/计费按用户账户。
