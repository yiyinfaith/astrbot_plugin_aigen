# AstrBot AIGen 多模态生成器

`astrbot_plugin_aigen` 为 AstrBot 4.16+ 提供图片和视频生成、独立关键词预设、可配置帮助与使用统计。当前版本 **v1.0.1**，支持热重载，不需要重启 AstrBot。不设置个人或群组次数限制。

## 安装和升级

在 AstrBot 插件管理中使用仓库 URL 安装，已有安装点击本插件的“更新”即可从 GitHub 获取代码并热重载：

https://github.com/yiyinfaith/astrbot_plugin_aigen

首次从 v1.0.0 升级时，插件会把原有配置迁移到新的分组，**完整保留图片预设的顺序和所有提示词文字**，也保留接口、Key、帮助文案与开关值。迁移前会在插件数据目录 `config_backups/` 保存一份原配置；以后热重载不重新迁移，不用默认预设覆盖你的列表。仓库默认 44 条只用于新安装。

AstrBot 在插件初始化前会清理 Schema 以外的配置字段，因此升级所需的旧字段暂时以隐藏快照保留，页面只显示三个新分组；运行时仅使用分组内的设置。如新旧预设同时存在且内容不同，插件停止自动迁移并保留备份，保护两份内容。

## 配置界面

插件使用 AstrBot 带边框的分组，模型路由采用可折叠卡片：

| 分组 | 内容 |
| --- | --- |
| 🌐 全局设置 | 代理、HTTP 超时、流式请求、调试、普通回复显示、帮助文案、函数工具冷却 |
| 🎨 生图设置 | 图片专用的接口格式、地址、Key 池、模型、画质、触发规则、预设和图片下载 |
| 🎬 生视频设置 | 视频专用的六种接口格式、地址、Key 池、自动路由模型、时长、画质、触发规则、预设、任务轮询和视频缓存 |

图片和视频的接口地址、Key、模型、预设分别配置。视频 API 地址填写基础地址（例如 `https://api.example.com`），不能把图片接口路径用作视频接口。

流式开关适用于全部接口类型。图片保留相应接口的流式行为；标准视频接口按文档创建异步任务并轮询，支持解析 JSON/SSE 响应，不额外添加协议禁止的 `stream` 请求体字段。自定义视频模板可以使用 `{stream}`。

## 图片使用

- `/生图 <提示词>`、`/画图 <提示词>`：无图片是文生图，传图片是图生图，支持多图。
- 预设关键词可出现在消息任意位置，例如 `@小明手办化`，关键词以外的文字追加到预设提示词。重叠关键词选最长的。
- 自定义提示词默认需要 AstrBot 前缀；关闭对应开关后 `生图 <提示词>` 可以触发。
- 预设默认无需 AstrBot 前缀；开启对应开关后需要前缀或唤醒机器人。
- 图片预设只取一图，优先 **引用图片 > 同时发送的图片 > @用户头像 > 发送者头像**。
- 自定义提示词按消息组件顺序收集引用图片、发送图片、@用户头像，不自动添加发送者头像。
- `/ai生成帮助`、`/生图帮助`、`/画图帮助`、`/生图菜单`、`/画图菜单` 显示配置的帮助文字。

图片保留 `openai_image`、`openai_chat`、`openai_response`、`gemini_official`、`custom_endpoint` 五种请求格式。

## 视频使用和自动路由

`/生视频 <提示词及参数>`，也支持 `/生成视频`。提示词、图片、音频、视频共同决定任务类型，工具、指令、视频预设使用同一路由。打开“根据输入媒体自动路由”后，在“自动路由模型”中按类型配置模型、接口格式、可选分辨率和比例。每种路线可再指定 **10 秒以上使用的模型**。关闭自动路由后使用固定模型，仍会校验已知工作流的输入约束。

| 输入 | 路线 | 默认模型 |
| --- | --- | --- |
| 只有文字 | 文生视频 | `minimax_h3_lightx2v_no_pic` |
| 一张或多张图片 | 图生视频 | `minimax_h3_lightx2v_v5`，超过 10 秒用 `minimax_h3_lightx2v_v5_15s` |
| 明确指定两张首尾帧 | 首尾帧生视频 | `minimax_h3_lightx2v` |
| 明确指定首帧 | 首帧生视频 | 留空，由管理员配置支持首帧的 Seedance/自定义模型 |
| 音频 | 音频生视频 | `minimax_h3_image_audio_to_video_v2`，超过 10 秒用对应 `_15s` 模型 |
| 图片＋音频 | 图音生视频 | `minimax_h3_image_audio_to_video_v2`，超过 10 秒用对应 `_15s` 模型 |
| 图片＋视频 | 图片＋视频 | `wan2.2-animate-move`，默认使用 DashScope 格式 |
| 视频 / 音频＋视频 / 图音视频 | 相应多媒体路线 | 留空，由管理员配置支持该组合的模型，例如合适的 Seedance 模型 |

这些默认 ID 来自 [AutoDL API 文档](https://github.com/yiyinfaith/new-api-plugin-autodl/blob/main/API.md)。渠道必须允许所选模型并配置价格；有路线不代表上游一定支持。空模型、媒体数量不符、时长越界、不支持的分辨率等会在提交前明确报错，插件不丢弃参考媒体，也不改时长或降档。两张普通参考图不会自动变成首尾帧，需要 `--frames`。

直接发图片、引用图片和 @用户取头像都可作为视频参考图。视频预设仍只取一张图片并采用上述单图优先级；视频自定义提示词可收集多图并保持消息组件顺序，不自动使用发送者头像。可直接发送或引用音频/视频，也可填写媒体 URL。引用链为空时会尝试通过平台读取原消息；OneBot 语音文件 token 会尝试导出 MP3。不支持或无法转换的 SILK/AMR 会说明原因，请提供 WAV/MP3 等支持的音频文件。可读的媒体必须来自平台或有效地址。

### 命令参数

| 参数 | 用法 |
| --- | --- |
| `--duration 1` | 正整数秒；也可在提示词中写 `1秒` |
| `--resolution 480p` | 显式分辨率；也可写在提示词中 |
| `--ratio 16:9` | 比例，支持 16:9、9:16、1:1、4:3、3:4、21:9、adaptive |
| `--image URL` | 参考图片，可重复；也可用本地路径或 base64:// |
| `--audio URL` | 参考音频，可重复；支持 URL、标准 Data URL 和本地文件 |
| `--video URL` | 参考视频，可重复；支持 URL、标准 Data URL 和本地文件 |
| `--frames` | 恰好两张图片作为首尾帧，不允许混入音频/视频 |
| `--reference-mode reference` | 普通参考；另支持 first_frame、first_last_frame |
| `--seed -1` | 不发送种子；其他整数范围由模型决定 |
| `--generate-audio true` | Seedance 同时生成音频；另支持 false、auto |

显式参数优先于提示词中的画质/比例和配置默认值。带空格的路径用双引号括起来；参数错误会指出具体项目。图片＋视频路线通常不使用 prompt 或可控时长，可以仅发送 `/生视频` 并附带一图一视频。

```text
/生视频 一朵云慢慢飘过 --duration 1 --resolution 480p
/生视频 让人物挥手 --duration 1   （同时发图，或引用图片、@他人）
/生视频 人物唱歌 --audio https://media.example.com/song.wav --duration 5   （附带图片）
/生视频 一段自然的镜头过渡 --frames --image https://media.example.com/start.png --image https://media.example.com/end.png --duration 1
/生视频 --image https://media.example.com/person.png --video https://media.example.com/motion.mp4
```

视频拥有独立关键词预设、自定义前缀（默认“生视频”）、两个前缀触发开关，规则和图片相同；自定义默认需前缀、预设默认无需前缀。相同关键词在图片和视频都配置时，最长关键词优先，相同长度优先图片。`/生视频帮助`、`/生视频菜单` 也显示全局帮助。

### 六种视频接口

| 格式 | 创建路径 | 查询路径 |
| --- | --- | --- |
| OpenAI Videos | `/v1/videos` | `/v1/videos/{task_id}` |
| MiniMax V2 | `/v2/video_generation` | `/v2/query/video_generation/{task_id}` |
| AutoDL | `/api/v1/comfyui/comfyui_workflow/{model}` | `/api/v1/comfyui/comfyui_workflow/result/{task_id}` |
| DashScope Wan | `/api/v1/services/aigc/image2video/video-synthesis` | `/api/v1/tasks/{task_id}` |
| Seedance | `/api/v3/contents/generations/tasks` | `/api/v3/contents/generations/tasks/{task_id}` |
| 自定义路径 | 配置 `custom_create_path` | 配置 `custom_query_path`，必须含 `{task_id}` |

各模式使用各自协议请求体，不混写字段。六种模式均接受管理员填写的模型 ID；插件不设置模式与模型的白名单。DashScope Wan 会按提示词、图片、音频、视频以及 reference/first_frame/first_last_frame 组合生成 input 字段；上游未开放的组合会由服务端返回明确错误。MiniMax 两个官方别名仍遵守文档中 4/5 秒最低时长和档位限制，直接工作流 ID 可按自身能力请求 1 秒。

自定义模式还提供 JSON 请求模板、额外请求头、任务 ID/状态/视频 URL/错误字段的 JSON 点路径（支持数组索引），以及成功/失败状态列表。模板占位：`{model}`、`{prompt}`、`{duration}`、`{resolution}`、`{ratio}`、`{size}`、`{image}`、`{images}`、`{audios}`、`{videos}`、`{stream}`。占位独占一个 JSON 字符串时会保留列表、数字、布尔值类型，避免模板字符串破坏 JSON。

## LLM 函数工具

两个工具都可以由 AstrBot 的 LLM 调用，名称和参数名固定。工具成功时只返回生成的媒体，不附加进度、耗时或成功文字；错误会返回具体原因。

### `generate_image`

```json
{
  "prompt": "一只戴红围巾的猫，电影感光影",
  "image_url": "https://example.com/reference.png"
}
```

- `prompt`（`string`，必填）：提示词。
- `image_url`（`string`，可选）：一张参考图 URL 或标准 Data URL；省略或空字符串为文生图，传入后为图生图。
- 函数只接受这两个参数；多图、当前消息图片和引用图片由普通命令 `/生图`、`/画图` 处理。

### `generate_video`

工具只提供一个入口，插件按媒体参数自动选择配置中的路线和模型。所有参数都是 JSON 字段，不要把 `--duration` 这样的命令行写法传给工具。

```json
{
  "prompt": "人物在海边慢慢转身，镜头平稳推进",
  "image_url": "https://example.com/person.png",
  "audio_url": "",
  "video_url": "",
  "duration": 5,
  "resolution": "480p",
  "aspect_ratio": "16:9",
  "reference_mode": "reference",
  "seed": -1,
  "generate_audio": "auto",
  "use_message_media": true
}
```

| 参数 | 类型与取值 | 作用 |
| --- | --- | --- |
| `prompt` | `string`，可空 | 文生、图生和多媒体提示词；只做图片＋视频参考时可留空。 |
| `image_url` | `string`，可选；多张用空格分隔 | 图片 URL 或标准 Data URL。工具参数中的图片按空格拆分并保持顺序。 |
| `audio_url` | `string`，可选；多段用空格分隔 | 音频 URL 或标准 Data URL。 |
| `video_url` | `string`，可选；多段用空格分隔 | 视频 URL 或标准 Data URL。 |
| `duration` | `integer`，`0` 或正整数 | `0` 使用提示词/配置默认时长；正整数覆盖默认时长。 |
| `resolution` | `string`，如 `480p`、`768p` | 覆盖路线分辨率；留空使用路线配置。 |
| `aspect_ratio` | `string` | 支持 `16:9`、`9:16`、`1:1`、`4:3`、`3:4`、`21:9`、`adaptive`。 |
| `reference_mode` | `auto`、`reference`、`first_frame`、`first_last_frame` | `auto` 使用路线配置；`first_frame` 要求恰好一图；`first_last_frame` 要求恰好两图。 |
| `seed` | `integer` | `-2` 使用配置，`-1` 不发送种子，非负数发送给支持该字段的接口。 |
| `generate_audio` | `auto`、`true`、`false` | `auto` 使用配置；主要用于支持生成音频的模型。 |
| `use_message_media` | `boolean` | 默认 `true`。某类显式 URL 为空时读取当前消息、引用和 @用户头像；设为 `false` 忽略消息附带媒体。 |

媒体组合与自动路由如下；模型和接口格式均可在“生视频设置 → 自动路由模型”中改为管理员实际可用的值：

| 参数媒体 | 路由 | 默认模型 |
| --- | --- | --- |
| 无图片、音频、视频 | 文生视频 | `minimax_h3_lightx2v_no_pic` |
| 有图片，无音频、视频 | 图生视频 | `minimax_h3_lightx2v_v5`；时长超过 10 秒用 `minimax_h3_lightx2v_v5_15s` |
| `reference_mode=first_last_frame` | 首尾帧生视频 | `minimax_h3_lightx2v` |
| `reference_mode=first_frame` | 首帧生视频 | 使用管理员配置的模型 |
| 有音频，无视频 | 音频生视频或图音生视频 | `minimax_h3_image_audio_to_video_v2`；超过 10 秒用对应 `_15s` |
| 有视频，无图片、音频 | 视频生视频 | 使用管理员配置的模型 |
| 有图片＋视频（可再带音频） | 图片视频多媒体路线 | 默认 `wan2.2-animate-move`（DashScope Wan），也可改成任意已接入模型 ID |

工具媒体优先使用显式 `image_url`/`audio_url`/`video_url`；缺少的类型才从消息中补充。图片来自消息时沿用：引用 > 发送图片 > @用户头像；工具不自动加入发送者头像。每种协议仍使用自己的请求体，模型是否接受某种媒体组合由上游服务决定。

普通命令的参数写法：

```text
/生视频 海边日落 --duration 1 --resolution 480p
/生视频 人物挥手 --image https://example.com/person.png --duration 5
/生视频 首尾帧过渡 --frames --image https://example.com/start.png --image https://example.com/end.png
/生视频 人物唱歌 --image https://example.com/person.png --audio https://example.com/song.wav
/生视频 --image https://example.com/person.png --video https://example.com/motion.mp4
```

`--image`、`--audio`、`--video` 可重复；`--frames` 等于 `--reference-mode first_last_frame`。命令行还支持 `--ratio`、`--reference-mode`、`--seed`、`--generate-audio`。带空格的 URL 或路径请用引号。直接发送、引用图片/音频/视频和 @用户取头像都支持；引用链为空时插件会尝试读取原消息。

## 任务、计费与数据

每次调用只创建一次收费任务；POST 连接中断也不自动重新生成，避免重复扣费。后续仅轮询原任务，下载失败仅重试下载。等待或查询失败时显示任务 ID，任务记录保存在插件数据目录 `video/*.json`；视频完成但下载失败时保留结果地址，可从上游取回，不必再次付费。公开视频下载不附带 API Key。

费用由上游接口扣取，插件不额外收费、不设置次数额度。`daily_stats.json` 保留旧统计并新增 image/video 分类。MP4 缓存默认保留 24 小时，仅清理插件自己的文件；任务记录和配置备份保留。缓存大小、下载重试/超时、查询间隔和最大等待时间均可配置。热重载关闭 HTTP 会话和清理任务，不重启 AstrBot。

## 开发验证

```bash
python -m py_compile main.py api_manager.py data_manager.py image_manager.py generation_params.py config_manager.py video_router.py video_inputs.py video_api_manager.py utils.py
python -m unittest discover -s tests -v
ruff check main.py config_manager.py data_manager.py video_router.py video_inputs.py video_api_manager.py tests/test_video* tests/test_config_migration.py
```

测试覆盖六种协议请求结构、媒体路由、引用与头像、参数错误、工具输出、一次提交和轮询下载、配置/预设迁移。宿主配置加载器集成测试需要旁边的 AstrBot 源码；没有源码时该项自动跳过，其余测试独立运行。低成本真实测试仅提交一条 1 秒 480p 文生视频任务；其他模式用本机模拟服务验证，不需要逐模式产生费用。

## 参考与许可

项目：[yiyinfaith/astrbot_plugin_aigen](https://github.com/yiyinfaith/astrbot_plugin_aigen)。配置、消息组件和函数工具依照 AstrBot 当前开发文档实现。视频适配参考 [AutoDL API](https://github.com/yiyinfaith/new-api-plugin-autodl/blob/main/API.md) 和 [Seedance 创建任务文档](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh)。遵守本仓库许可证及上游服务的使用要求。

