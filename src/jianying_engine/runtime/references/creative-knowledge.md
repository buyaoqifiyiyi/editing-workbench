# 剪辑创作知识库

用于从已有理论、教程观察、模板方法和引擎工具中，按当前创作问题取用知识。入口服务于 [创作决策层](../guides/planning.md)：它帮助形成和核验决定，不自动理解素材、不替代创作者判断，也不扩大任何软件或素材授权。

## 如何调用

制作、整体重剪或用户指出创意问题时，先根据问题在下表选相关主题，再读取该主题和必要的既有方法卡；简单局部改动只查受影响部分。未列到的需求继续通过 `jianying-engine paths`、`guide`、`capabilities` 和本目录现有指南检索，不把本表当能力全集。

开始确定制作方案前，按[每版共同决策](../guides/planning.md#每版同时设计剧情与剪辑)同时核对剧情组织与剪辑表现所需的知识，让知识参与选材、切点、节奏、构图和声音选择；制作及修订中按新问题补查，不等时间轴铺满后才查技巧。

新鲜 MCP 可用 `search_knowledge` 按问题检索，结果包含注册的指南名、章节、行号和摘录；再用 `read_guide` 读取所选主题。检索只检查打包的引擎指南，不访问项目素材、不联网，也不判断成片质量。若长驻 MCP 尚未发现新工具或指南，不中断其他任务；使用当前引擎 CLI：

```text
jianying-engine guide reference/creative-knowledge
jianying-engine knowledge "剧情不清 人物目标与代价" --limit 5
jianying-engine knowledge "声音先入画面后入" --limit 5
jianying-engine guide reference/creative-knowledge-story
jianying-engine guide reference/creative-knowledge-techniques
```

命令中的 `jianying-engine` 指已安装引擎入口；按实际环境也可用该环境的 Python 执行 `-m jianying_engine`。`search_knowledge` 是只读 MCP 工具，不通过 `start_job`；`guide` 通过 `read_guide` 或 CLI 使用，不把指南名伪装成剪辑操作。检索基于词语与有限同义词，可能漏掉语义相近材料；没有结果不等于知识不存在，可继续看下表及原指南。

| 当下问题 | 先查什么 | 再查什么 |
|---|---|---|
| 有片段但看不出故事、人物动机或结果 | `reference/creative-knowledge-story`：事件、信息缺口、结构选择 | [通用剪辑](general-editing.md)、[漫剪](styles/anime-mix.md) |
| 情绪/节奏太平、镜头只是在接龙 | `reference/creative-knowledge-story`：变化和反差；`reference/creative-knowledge-techniques`：连接 | [创作修订](creative-review.md)、[音乐与动作](styles/anime-mix.md) |
| 学过手法却未用、转场或画面接管单一 | `reference/creative-knowledge-techniques`：按连接问题比较可执行方法 | [教程方法索引](tutorial-library.md)、[组合方法](technique-combinations.md)、[分层揭示](layered-reveal.md) |
| 对白被淹没、转写不可靠、音乐不贴 | `reference/creative-knowledge-audio`：原声、分离、音乐和听审 | [音乐编排](music-edit.md)、[节拍](beat-marking.md) |
| 用户说重复、小窗无意义、文字或叙事突兀 | `reference/creative-knowledge-workflows`：反馈与对照模板 | [创作修订](creative-review.md)、[镜头去重](general-editing.md) |
| 想用模板、文字、分屏或特殊效果 | `reference/creative-knowledge-workflows`：工具与模板 | [模板库](template-library.md)、[文字融景](text-in-scene.md)、[主体与图形](subject-graphic-interleave.md) |
| 纪录、产品、解说或旅行等不同片型 | [纪录](styles/documentary.md)、[商业](styles/commercial.md)、[解说](styles/explainer.md)、[Vlog](styles/vlog.md) 中对应段落 | 按实际内容补查声音、文字或连接，不把剧情结构套给所有任务 |
| 不确定软件能否做、如何校验或预览 | `reference/creative-knowledge-workflows`：工具与证据 | [能力状态](capabilities.md)、[计划格式](plan-format.md)、[原生预览](native-preview.md) |
| 想追查知识来自哪里、是否真正观看 | `reference/creative-knowledge-sources` | 各主题引用的来源和项目外验收记录 |

## 使用边界

- 先理解任务与材料，再让知识影响一次具体选段、结构、声音或连接决定；不要把知识卡当成固定风格或逐条必须添加的效果。
- 区分**已观察到的素材事实、基于事实的创作推断、尚待确认的假设**。网页标题、转写、静帧和连续视听的证据层级不同。
- 理论可借鉴不等于当前软件、版本和输入条件已准入。执行参数以 [能力状态](capabilities.md) 和对应后端指南为准；已安装资源、模型、素材和旧工程不复制进知识包。
- 针对用户的偏好和被否定版本，优先检索当前项目已有记录；只迁移适用条件，不将一次喜好变成所有作品的公式。原始素材、截图、会话、模型、凭据和缓存留在项目或外部维护目录。
- 一张卡被读取、一个脚本运行或草稿结构通过，都不证明成片好看。缺少当前版本的连续视听证据时，交付中明确未验证，不用静帧或模型自评替代用户的最终观看。
