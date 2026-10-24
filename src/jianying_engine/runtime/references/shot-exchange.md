# 多软件协作：Blender 镜头与 Kdenlive 时间轴

本入口只负责有界交接，不把“安装软件”计为能力完成。`shot-exchange` 经统一 CLI/API/MCP 调用；不启动现有图形会话，不改原计划、原片或旧工程。用户明确要求的多软件加工允许生成中间素材，原生可编辑参数保留在 `.blend`，回到剪映时使用渲染视频；不能在剪映编辑 Blender 图层。最终完整视听和成片导出仍由用户负责。

## 调用

```powershell
jianying-engine shot-exchange prepare --plan edit-plan.json --index 0 --head-frames 3 --tail-frames 3 --out shot-v1
jianying-engine shot-exchange build --handoff shot-v1 --blender '完整的blender.exe路径' --render
jianying-engine shot-exchange apply --handoff shot-v1 --render-file shot-v1/processed.mp4 --out return-v1
jianying-engine validate --plan return-v1/edit-plan.json
jianying-engine build --plan return-v1/edit-plan.json --out new-draft
```

`prepare` 只写 `handoff.json`、规范化源计划快照和固定生成器输出的 Blender 脚本，不声称已经生成原生工程。源计划按字节、素材按 SHA256、镜头按整数帧绑定；源片、原计划、快照或选择变化须准备新版本。余量不够直接拒绝，不悄悄裁掉。

`build` 必须显式提供已安装的 Blender；当前实际验证版本 5.2.2 LTS。新建独立 `--background --factory-startup` 进程，生成可编辑 VSE 源视频条、`.blend` 和完成回执；可加 `--render` 生成静音 H.264 MP4 中间片。脚本必须由当前受信生成器及已验证参数重新生成比对，不信任清单自报哈希，不接受任意加工脚本。已有工程/回执/输出拒绝覆盖。可选 `prepare --end-scale 1.1` 在内容首尾建立原生缩放关键帧，范围 1–2；默认 1 不增加效果。

修改高级镜头时在独立 Blender 工程中编辑并另存新版本；由用户/调用者提供与交接契约匹配的静音渲染文件。`apply --project 另存.blend` 可关联该工程，必须未压缩；这里只核文件头与附件哈希，不证明任意外部文件可在 Blender 重开，也不证明外部视频确由该工程渲染。生成器自己的原生运行证据另看 build 回执。

`apply` 全解码返回视频，核帧数/时间戳/帧率/尺寸/无音频/已知色彩标签，源片保持独立；只换指定片段路径和 `source_in=head_frames/fps`，保留时间轴位置、时长、其他轨道与声音。输出为新目录中的 `edit-plan.json` 和 `return.json`，再用既有 validate/build 生成草稿。回填完成不自动发布到用户草稿区。

## 当前受限范围

- 计划中恰选一个普通主轨视频：原声关闭、speed=1，选中片段无特效、滤镜、变换、蒙版、关键帧或转场；有 composites/tasks/素材缺口的计划拒绝。
- MP4/MOV，源8位YUV420，方形像素、逐行、SDR Rec709或未标注，源与画布尺寸/帧率相同；24/25/30/50/60整数fps，最多1920×1080像素、含余量最多20秒。
- 源入点/时间轴/时长和余量均落整帧；使用带解码余量的实际帧时间戳核验，不能仅凭平均FPS判定CFR。VFR、分数帧率、HDR、透明/多轨合成及颜色转换不在本轮范围。
- 返回可为8位YUV420或10位YUV422静音视频；保留源的已知颜色标签，标签检查不证明色彩外观。首次原生呈现只验证合成夹具和抽样时钟，不代表真实作品的画质、颜色或高级效果验收。

## Kdenlive 交接

```powershell
# 只需时间轴交换时安装引擎的 [exchange] extra；依赖 OpenTimelineIO 0.18.1。
jianying-engine shot-exchange timeline --plan plain-plan.json --out new-timeline.otio
```

仅导出连续的普通静音主轨视频剪切（源文件也必须无音频），同画布/同FPS、speed=1、无效果、无文字、无独立声音。含其它内容直接拒绝，不静默丢弃或误把源原声恢复。Kdenlive 使用“文件 → OpenTimelineIO 导入”；OTIO 本身不携带媒体或渲染尺寸，所有源引用保持本机绝对 file URI，首片尺寸必须正确。OTIO库写读校验与Kdenlive实际导入/播放/保存是分开的证据；没有原生证据时状态保持 `unverified`。

本入口没有 PR、AE、达芬奇或 Natron 适配；也不把安装开源替代品解释为这些商业软件的开源版。现有剪映工程及已认可作品不迁移、不覆盖。长驻 MCP 若缓存旧白名单，使用新连接验证，不能中断别的共享任务强行重启。

## 官方依据

- [Blender Python API](https://docs.blender.org/api/current/)，实际版本接口另以安装的 bpy RNA 与真实后台输出核对。
- [Kdenlive OpenTimelineIO 导入/导出](https://docs.kdenlive.org/en/user_interface/menu/file_menu.html#opentimelineio-import)。
- [OpenTimelineIO](https://github.com/AcademySoftwareFoundation/OpenTimelineIO)，Apache-2.0；只做剪辑信息交换，不承诺特效互通。
