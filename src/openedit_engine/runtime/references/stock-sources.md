# 在线素材接入

## 已实现的范围

| 来源 | 工具接入 | 凭据/限制 |
|---|---|---|
| Pexels | 官方 API 搜索视频与图片；下载选中素材 | `PEXELS_API_KEY`；API 搜索结果显著显示来源与作者链接 |
| Pixabay | 官方 API 搜索视频与图片；下载选中素材 | `PIXABAY_API_KEY`；结果缓存 24 小时；不扫库、不批量下载。音乐/音效不在当前公开 API 范围 |
| Openverse | 官方 API 搜索图片与音频；下载选中素材 | 匿名接口可用但有限额；逐项回原站核实许可，聚合元数据不保证准确 |
| Wikimedia Commons | MediaWiki API 搜索视频、图片与音频；下载选中素材 | 无需 Key；逐文件核对来源、许可和其他权利。仅直接下载当前编辑器能探测的格式 |
| Mixkit | 通过当前网页工具选材，正常下载后用 `register` 登记本地副本 | 未找到公开官方搜索 API，不使用猜测的私有接口；核对 Free/Restricted 与媒体类型许可 |

这是 Codex 进行镜头拆解、关键词规划和视觉判断的工具，不是自动语义匹配模型。API 返回只是候选，文件名、标签、分辨率不能代替观看或试听。国内连通性、接口限额和素材缺失要按实际结果报告。

## 凭据

运行 `stock.py status` 只显示配置状态，不显示密钥。凭据优先取当前进程环境变量，其次读取外置配置文件。`JIANYING_STOCK_CONFIG` 可指定现存配置文件的绝对路径，显式路径失效会报错；未设置时默认 `%LOCALAPPDATA%/JianyingEngine/config.env`。只有通过旧 Skill 路径调用且默认文件不存在时，才兼容读取该旧 Skill 根的 `config.env`，不复制配置进引擎。无 Key 的来源仍可使用。用户可在本机编辑这个文件：

```dotenv
PEXELS_API_KEY=用户本机填写的实际密钥
PIXABAY_API_KEY=用户本机填写的实际密钥
```

从 [Pexels API](https://www.pexels.com/api/) 或 [Pixabay API 文档](https://pixabay.com/api/docs/) 注册获取。不要要求用户在聊天里发送密钥，不输出它、不复制到计划/交付/压缩包。工具只读凭据，不创建账户。缺少凭据时先完成免 Key 的检索，不把缺 Key 标为已连接。当前进程还未继承新环境变量时可用本地配置文件，不修改其他应用配置。

## 工作流程

1. 先检查用户素材和脚本。把缺口转成具体镜头检索词，如“雨夜城市车流远景”，通常同时尝试简短英文词；向服务只发送通用搜索词，不上传脚本全文或私人素材。采用脚本/参考确定的比例。自有角色、真实产品、专属场景优先留给用户素材。
2. 调用 `search`，每次 1–20 条候选、默认 6 条，针对镜头搜索。`auto` 使用已配置的 Pexels/Pixabay 和适合该媒体的免 Key 来源。不要自动翻页扫库、绕过限流或下载所有候选；网络失败不无限重试。支持单独指定来源。
3. 阅读 JSON 中的具体来源页与许可，通过网页能力核实准备选用的项目。默认排除 NC、ND、未知许可、Mixkit Restricted。优先 CC0、公有领域或站点允许用途的免费许可；CC BY 必须保留署名，CC BY-SA 仅在明确相同方式分享安排后使用。人物、商标和产品背书按画面与实际用途核对。`review-note` 记录实际核查结果，不能写虚假的“已核实”。
4. `fetch` 一次只下载一个选中的 ID，默认上限 128 MiB；临时下载完整后 FFprobe 检查再保存。候选链接 24 小时后要求重新搜索。文件和 `.provenance.json` 不覆盖；复用已下载副本。Openverse 部分音频链接是压缩试听版（`rendition=preview`），不当作原始无损音频；需要原件时通过原站正常登录下载再登记。`register` 只为已经下载的本地文件补登记，不代表工具验证了版权。Mixkit/Pixabay 音乐按网站正常流程取得后登记。
5. 对本地副本运行 `editor.py inspect` 并实际查看/试听，确认主体、裁切、时长与风格再写入计划。`edit-plan.json` 保持本地路径格式，无需把 URL 放入时间轴。若文件格式不支持，在任务目录里用 FFmpeg 仅转换封装或编码为兼容副本，并为转换后的文件重新登记，备注记录原始来源与转码，保留原件。不可在转码时添加滤镜、抠像或其他视觉效果，所有剪辑效果由剪映原生参数实现。
6. 草稿自动附带实际用到的 `素材来源.json/md`。在最终成片或发布说明实现必要署名及许可链接；工程附件不能代替公开署名。本地无来源记录的素材保持“未登记”，不擅自推断版权状态。免费配乐也可能触发 Content ID，保留下载证书或来源记录。

## 命令示例

```powershell
$skillRoot = '当前加载Skill的绝对路径'
$skillPython = 'E:\引擎环境\Scripts\python.exe' # 外部虚拟环境，替换为实际路径
& $skillPython "$skillRoot\scripts\stock.py" status
& $skillPython "$skillRoot\scripts\stock.py" search --kind video --query 'city night traffic' --orientation portrait --out 'D:\剪辑任务\候选-夜景.json'
& $skillPython "$skillRoot\scripts\stock.py" search --provider openverse --kind audio --query 'rain ambience' --out 'D:\剪辑任务\候选-雨声.json'
# ID 从实际搜索结果取得；备注只填写实际核查结果。
& $skillPython "$skillRoot\scripts\stock.py" fetch --results 'D:\剪辑任务\候选-夜景.json' --id '来源:实际ID' --out 'D:\剪辑任务\素材' --review-note '实际来源与许可核查记录'
& $skillPython "$skillRoot\scripts\stock.py" register --input 'D:\剪辑任务\素材\音效.wav' --provider mixkit --source 'https://mixkit.co/实际素材页面/' --creator '实际作者或站点明确标识' --license 'Mixkit Sound Effects Free License' --license-url 'https://mixkit.co/license/modal/sfxFree/' --review-note '已查看该项目与音效许可，记录具体允许用途'
```

搜索输出同时含 Markdown 来源链接，Pexels/Pixabay 来源须在向用户展示搜索结果时保留。`status` 表示凭据已填，不表示接口已经验证；报告“已连通”必须实际请求成功。Pexels/Pixabay 有 Key 后分别执行真实搜索和选中下载，才能提升其验证状态。

## 官方依据（核对日期：2026-10-06）

- [Pexels API 文档](https://www.pexels.com/api/documentation/)：视频路径现在为 `/v1/videos/`；[素材许可](https://www.pexels.com/license/)。
- [Pixabay API 文档](https://pixabay.com/api/docs/)：图片/视频接口、缓存、限流与按需使用；[素材许可](https://pixabay.com/service/license-summary/)；[音乐与 Content ID FAQ](https://pixabay.com/service/faq/)。
- [Openverse API](https://api.openverse.org/v1/)；[媒体类型与许可准确性说明](https://docs.openverse.org/api/reference/made_with_ov.html)。
- [MediaWiki Imageinfo](https://www.mediawiki.org/wiki/API:Imageinfo)、[Search](https://www.mediawiki.org/wiki/API:Search)；[Commons 复用许可](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia)。
- [Mixkit 各类许可](https://mixkit.co/license/)、[使用条款](https://mixkit.co/terms/)。音乐许可有电视、广播、游戏等限制，按具体发布渠道核查。
