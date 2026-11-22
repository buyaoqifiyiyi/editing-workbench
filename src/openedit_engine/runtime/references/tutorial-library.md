# 教学方法索引

历史原始课程学习卡与采样资料保存在工作区外（不打入本开源引擎安装包）；全部可复用的跨软件实战精华（涵盖 15 类全课程、`秋叶剪辑` 41 集、`七七学剪辑` 第 214 集、`Need-it-L` 动漫排版、`帧帧爱剪辑` 10 集、`今天剪废了吗` 及 81 套桌面模板编排）已统一按 OpenEdit 纯开源实现重整理至 [已学课程实战总手册](course-techniques-openedit.md) 及下列专题卡。查阅规则见 [方法知识](general-editing.md#方法知识和执行证据分别判断)，当前支持能力统一以 [能力状态](capabilities.md) 为准。

| 技术问题 | 方法要点 | 落点 | 归档出处 |
|---|---|---|---|
| 关键帧曲线、缓动与挤压拉伸弹跳 | 斜率 `dv/dt`、独立轴弯曲轨迹、`scale_x`/`scale_y` 非等比形变 | [已学课程总手册](course-techniques-openedit.md#关键帧曲线缓动与运动力学) | 全课程 01（李百万Tony/阳阳） |
| 局部强调、压低背景 | 同源双层，上层蒙版，下层降透明/模糊 | [组合方法](technique-combinations.md#按目标检索) | qiuye-editing.md |
| 分区逐次出现、渐进分屏 | 旧镜头为底，新分区错拍补齐 | [分层揭示](layered-reveal.md#split) | xiao-ying-tao-d.md、jian-shenme-ji-70-92.md |
| 条带拼合露缝 | 按画布等分算中心，`feather: 0` + 纯色底查缝 | [分层揭示](layered-reveal.md#split)、[已学课程总手册](course-techniques-openedit.md#动态几何蒙版与钢笔贝塞尔遮罩) | xiao-ying-tao-c.md、jian-shenme-ji-70-92.md |
| 窗口揭示、扫过节奏 | 底景不动，蒙版关键帧间距定快慢 | [三类遮罩](technique-combinations.md#三类遮罩不能混用) | qiuye-editing.md、jian-shenme-ji-01-23.md |
| 字形内显示画面、字形穿越 | 组内黑底白字；放大到白区盖满 | [三类遮罩](technique-combinations.md#三类遮罩不能混用)、[已学课程总手册](course-techniques-openedit.md#多级嵌套合成字中视频与动漫分层排版) | xiao-mao-anime-mix.md、xiao-ying-tao-b.md、全课程文字组 |
| 动漫人物分层排版、重影与双层字 | 边带角标 → 透明前景+重影 → 双层偏移字 → 大圆补空 → 错峰入画 | [已学课程总手册](course-techniques-openedit.md#多级嵌套合成字中视频与动漫分层排版) | Need-it-L 动漫排版教程 |
| 超燃卡点接力与穿屏转场 | 动作匹配 → 四角小窗黑框 → 窄条先快后慢接管 → 局部/爆闪分工 → 透明穿屏 | [已学课程总手册](course-techniques-openedit.md#超燃卡点接力与多图模板编排) | 七七学剪辑第 214 集 |
| 照片墙、轮播、滚动队列 | 收束单帧、下层对齐上层到位、纹理周期整数倍防跳帧 | [已学课程总手册](course-techniques-openedit.md#超燃卡点接力与多图模板编排) | 电脑版模板 81 套归档 |
| 亮度遮罩转场重影 | 两路互补遮罩再叠加 | [三类遮罩](technique-combinations.md#三类遮罩不能混用) | jian-shenme-ji-01-23.md |
| 窗口阴影、边框、弧形遮幅 | 同蒙版纯色副本置下；椭圆黑层 | [组合方法](technique-combinations.md#按目标检索) | jian-shenme-ji-24-46.md、qiuye-editing.md |
| 闪白、漏光衔接 | 亮峰对准切点，只盖短区间（`beat_flash`） | [作用范围](technique-combinations.md#作用范围与时长) | xiao-ying-tao.md |
| 揭示起点、冲出画框、逐层长出、一帧多时段 | 触发点扩散；同源副本同位同时；分层依次入场 | [组合方法](technique-combinations.md#按目标检索) | xiao-ying-tao.md、xiao-ying-tao-a/b/c.md |
| 回忆快闪、定格像卡帧 | 头中尾 alpha 交错；声画触发+状态差 | [技术流段落](technique-combinations.md#技术流段落如何组织) | jian-shenme-ji-01-23.md、jian-shenme-ji-24-46.md |
| 调色偏色、色轮与局部校色 | 波形分量定位偏色、曲线/三色轮/`local_circle`、`composites[]` 统调 | [已学课程总手册](course-techniques-openedit.md#完整调色工作流基础校正曲线色轮与局部调色) | qiuye-editing.md、全课程 09 |
| 轮廓跟踪、失准修补与横转竖跟拍 | CSRT 追踪与分段接续；横转竖用 YuNet 人脸跟拍 | [已学课程总手册](course-techniques-openedit.md#运动跟踪失准修正与竖屏跟拍) | 全课程 02 / 02b / 02c |
| 四角透视与 3D 空间运镜 | 叠层 JSX 脚本 / 3D 相机透视特效与 `0→峰值→0` 关键帧 | [已学课程总手册](course-techniques-openedit.md#四角透视3d-空间运镜与特效参数关键帧) | 全课程 05 / 05c / 14 |
| 轮播、胶片带、进度条与信息卡 | 下层起点取上层完整入画时刻，配合线性/缓动关键帧 | [开源动效配方库](recipes.md) | jian-shenme-ji-47-69.md、jian-shenme-ji-70-92.md |
| 照片卡点冲击 | 切点最大偏离，本拍内回正 | [节拍卡点](beat-marking.md#对-bgm-剪辑流程的补充) | jian-shenme-ji-01-23.md |
| 实物从屏幕、道具出现 | 同机位两次实拍，接点对齐 | [绿幕选型](green-screen.md#选型依据) | xiao-ying-tao.md、xiao-ying-tao-a.md |
| 句内重点词、口播粗剪与响度母带 | `highlights` 静态高亮 vs `rough-cut` 卡拉 OK + `ducking` + `audio-master` | [文字融景](text-in-scene.md#从句子到可执行排版)、[已学课程总手册](course-techniques-openedit.md#声音处理字幕高亮与口播粗剪一条龙) | xiao-ying-tao-c.md、全课程 10 / 12 |
| 情绪片连接、统一包装、竖排 | 按情绪功能选重影/白场/黑场 | [漫剪](styles/anime-mix.md#片源与段落设计) | maix-romance-mix.md、xiao-mao-anime-mix.md |
| 人物首次出场、动作方向相反 | 定格分层；同相位切，翻转查标识 | [通用剪辑](general-editing.md#原创操作与全片统筹) | tutorial-library-原版.md、jian-shenme-ji-01-23.md |
| 旅行开场揭示 | 短标题、可承受遮挡、不拖延 | [Vlog](styles/vlog.md#转场与文字) | tutorial-library-原版.md |
| 仿剪拆解、脚本对镜 | 原生动画与多轨都列候选 | [脚本与参考](general-editing.md#script-reference) | workflows.md |
| 运镜冲击力、无痕动势转场与音乐呼吸感 | 预备+畸变、快对快慢对慢、分轨纯鼓标点、3–10帧蓄力快切与节奏突变 | [运镜转场与节奏卡点实战](camera-transitions-rhythm.md) | B 站 15 条高播剪辑工艺实战 |

另见方法卡 [主体与图形穿插](subject-graphic-interleave.md)。

## 从方法到作品的调用

先按当前段落的视觉问题选方法，再核对素材前提和当前开源引擎的能力边界；不按教程列表逐项凑效果。分屏要说明怎样形成与接管，成组运动要说明共同中心和到位状态，动作卡点要说明哪个事件响应声音。方法描述、后端能执行、当前作品实际采用、用户认可分别保留证据。

技术流的设计与实际短段复核走 [创作决策流程](../guides/planning.md#每版同时设计剧情与剪辑)，用户反馈与跨任务复用走 [短段比较与反馈积累](creative-review.md#短段比较与反馈积累)。新增教程先补当前真实映射；没有验证实践和作品结果，不把笔记增加量当作能力升级。

不依赖复杂手绘钢笔的技术流也可按下面的纯开源能力直接组织，不必退回相框与小幅推进：

| 连接问题 | 已有纯开源入口 | 检查重点 |
|---|---|---|
| 非绿幕复杂背景人物/前景穿插与重影 | [`openedit matting --gpu`](capabilities.md#ai-matting)（MIT BiRefNet / RVM → `ProRes 4444 RGBA`） | 黑/灰/白三底验边，复核叠加运动帧不闪烁 |
| 横屏（16:9）素材转为竖屏智能跟拍 | [`openedit auto-reframe`](capabilities.md#auto-reframe)（YuNet 人脸 + 场景切点隔离） | `fit="cover"` 无黑边，摇镜关键帧不跨切点滑动 |
| 口播粗剪、去气口、字幕卡拉 OK 与响度统一 | [`openedit rough-cut`](capabilities.md#rough-cut) + [`openedit audio-master`](capabilities.md#audio-master) + `--draft` | 核对专有名词转写与正常速度试听混音 |
| 全画幅镜头需要方向和速度交接 | [节拍与尺度冲击](beat-marking.md#对-bgm-剪辑流程的补充)、`keyframes`（位置/缩放/旋转与 `motion_easing`） | 入画、到位和出画速度不同；切点接住观看重心；放大/旋转留足边缘 |
| 希望动作有加速或倒放 | [主剪辑与多轨时间轴](capabilities.md#filmcraft-editing)（`speed` `0.05..32.0` 与 `reverse`） | 源范围和声音政策明确；音画同步段保持一致速度策略 |
| 一幅画面应成为下一镜的过渡载体 | [混合模式与合成](capabilities.md#filmcraft-blend)（`composites[]`）与 [动效配方库](recipes.md) / [透明叠层](capabilities.md#effectcraft-overlays) | 视频和字按共同中心运动；整组旋转/缩放或 JSX 3D 透视叠层后由下一镜接管 |
| 窗口出现但缺乏推进 | [分层补齐](layered-reveal.md#reveal)和[渐进分屏](layered-reveal.md#split) | 以短时积累、主次变换、展开和释放连成过程；不能堆完小窗后长时间停着 |

先回看原始教学画面再选方法，仅将跨软件的构图、分层与节奏思路映射到 FilmCraft / EffectCraft 开源实现；商业宿主昔日的 VIP/收费效果清单已全面废弃，所有特效、遮罩、混合与多色高亮文字均可通过 [动效配方库](recipes.md) 或原生参数直接调用。
