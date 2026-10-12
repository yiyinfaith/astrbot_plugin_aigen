# AstrBot AIGen 多模态生成器

`astrbot_plugin_aigen` 为 AstrBot 4.16+ 提供图片和视频生成、独立关键词预设、可配置帮助与使用统计。当前版本 **v1.1.0**，支持热重载，不需要重启 AstrBot。不设置个人或群组次数限制。

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

图片保留 `openai_image`、`openai_chat`、`openai_response`、`gemini_official`、`custom_endpoint` 五种请求格式。`openai_image` 对应 OpenAI Images API（无图 `/v1/images/generations`、有图 `/v1/images/edits`）；`openai_response` 对应官方 Responses API 的 `/v1/responses` 和 `image_generation` 工具；`gemini_official` 对应 Gemini 官方 `generateContent`。`openai_chat` 只发送标准 Chat Completions 字段，图片输出依赖兼容服务自身，不再混入 Gemini 专用 `image_config` 等扩展字段。

## 视频使用和自动路由

视频命令采用材料收集：

1. 发送 `/生视频`、`/生成视频` 或任一配置的视频自定义前缀。也可在启动消息里写提示词或明确的路由关键词。
2. Bot 回复：`请发送材料，最后发送“/生成”开始生成视频`。
3. 依次发送文字、图片、QQ 语音、视频或对应的媒体文件。多段文字按发送顺序用空格合并成一个提示词；媒体按同类素材的接收顺序保留，重复发送也按不同参数计数。收集期间不逐条回复。
4. 发送 `/生成`。先校验所有参数与所选模型；有效时只发送一次 `共收到x图片，x音频，x视频，生成中`，再创建任务。
5. 成功后只发送视频。失败时发送一段原因说明；生成已成功但下载/发送失败时保留任务和公网地址，可取回结果，不重新生成。

`/取消` 清空当前材料；重新发送视频启动指令会开始新一轮收集。材料缺少或参数有误时保留当前材料，可补充后再次 `/生成`；要删除多余材料，用 `/取消` 后重新开始。连续 30 分钟没有材料则收集过期。会话按平台实例、群聊/私聊和发送人隔离，群内其他人以及该用户的其他聊天互不混入。快速连续发送材料和 `/生成` 时，会等待此前材料读取完毕；重复 `/生成` 不会重复创建收费任务。

**普通 `/生视频` 收到的所有图片都作为参考图，与图片数量无关。** 仅显式输入 `首尾帧`、`首帧`、`尾帧`、`动作迁移`、`音频同步` 这些关键词时才触发对应特殊类别；文字参数之间用空格分隔。关键词可放在启动消息里，也可作为收集的第一段文字。以下表格表示整个收集会话的合并参数，所有情况最后都需 `/生成`：

| 合并参数 | 路线 | 默认模型 |
| --- | --- | --- |
| 提示词，无媒体 | 文生视频 | `minimax_h3_lightx2v_no_pic` |
| 提示词 + 1–9 图（全部参考图，含两图） | 图生视频 | `minimax_h3_lightx2v_v5`，超过 10 秒用 `minimax_h3_lightx2v_v5_15s` |
| `首尾帧` + 提示词 + 恰好两图 | 首尾帧生视频 | `minimax_h3_lightx2v` |
| `首帧` + 提示词 + 恰好一图 | 首帧生视频 | 留空，管理员配置支持该用途的模型 |
| `尾帧` + 提示词 + 恰好一图 | 尾帧生视频 | 留空，管理员配置支持该用途的模型 |
| 提示词 + 1–3 音频，无图/视频 | 音频生视频 | `minimax_h3_image_audio_to_video_v2`，超过 10 秒用对应 `_15s` |
| 提示词 + 1–9 图 + 1–3 音频，无视频 | 图片＋音频生视频 | 同上 |
| `动作迁移` + 恰好一图一视频，不能有提示词或音频 | 动作迁移（图片+视频生视频） | `wan2.2-animate-move` |
| `音频同步` + 恰好一图一音频，不能有提示词或视频 | 音频同步（自动对口型，图片+音频生视频） | `minimax_h3_image_audio_to_video` |
| 提示词 + 一个或多个视频 | 视频生视频 | 留空，管理员配置 |
| 提示词 + 一张或多张图 + 一个或多个视频 | 图片＋视频生视频 | 留空，管理员配置 |
| 提示词 + 一段或多段音频 + 一个或多个视频 | 音频＋视频生视频 | 留空，管理员配置 |
| 提示词 + 一张或多张图 + 一段或多段音频 + 一个或多个视频 | 图片＋音频＋视频生视频 | 留空，管理员配置 |

音频允许 0–3、图片允许 0–9 的泛化场景中，没有对应素材时按实际组合选择图生或文生类别。包含视频的普通组合不额外规定数量上限；具体模型的能力仍会在提交前校验。任何路线都不会丢弃超出的媒体、截断时长或降低分辨率。空模型会指出应配置的类别，不提交收费任务。

所有自动路由类别默认 **MiniMax V2**。短时/长时模型、对应接口格式、分辨率和比例均可独立配置；长时接口选择“跟随本路线格式”时继承本路线的短时格式。没有长时模型则继续使用短时模型，并校验真实的时长范围。当前 AutoDL New API 只提供 OpenAI Videos 和 MiniMax V2；首帧、尾帧、普通视频重绘等类别保留给实际支持这些输入的服务，需要管理员设置模型。插件保留的六种请求格式不会让上游凭空具备未提供的工作流。

图片、音频、视频既可直接以 QQ 对应消息发送，也可作为文件上传或引用。当前/引用的文件 ID 会通过 OneBot 查询；适配器漏掉文件组件时尝试恢复原消息，失败会阻止提交，避免漏材料。QQ 语音使用 AstrBot 的 `Record.convert_to_file_path()` 转成 WAV，支持宿主的 SILK/AMR 解码；本地媒体编码为标准 Data URL。材料文件不可读、格式不支持或转码失败会在最后说明，不把未读取的媒体当作不存在。普通视频收集不会自动加入 @用户头像或发送者头像。

### 提示词与参数

每段文字按发送顺序追加，统一解析后提交；以下参数可以在任一文字材料里填写：

```text
/生视频
一只猫在雪地里跑
镜头平稳跟随
--duration 1 --resolution 480p --ratio 16:9
/生成
```

```text
/生视频 首尾帧
一段自然的镜头过渡
（依次发送两张图片）
/生成
```

```text
/生视频 动作迁移
（发送一张图片和一个视频，不发送提示词）
/生成
```

时长、分辨率和比例也可写成 `1秒`、`480p`、`16:9` 等自然语言参数。特殊类别仅通过 `首尾帧`、`首帧`、`尾帧`、`动作迁移`、`音频同步` 这些明确关键词触发。显式参数优先于配置，不支持时在付费提交前报错。图片、音频和视频须来自消息组件或引用，不接受文字中的媒体 URL。

视频与图片的“自定义提示词前缀”均为列表，每项一个。例如图片配置 `生图`、`画画`；视频配置 `生视频`、`做视频`。匹配完整前缀，重叠时最长者优先；内置 `/生图`、`/画图`、`/生视频`、`/生成视频` 始终可用。对应的前缀触发开关和预设关键词开关保持可配置；视频预设也会启动材料收集。旧版单个前缀会迁移为一项列表，现有预设与密钥保留。v1.1.0 升级会先备份配置，把旧图片＋视频路线中的默认动作迁移模型移至新增的动作迁移类别；已有其他服务的自定义模型与格式保留。

### 六种视频接口

| 格式 | 创建路径 | 查询路径 |
| --- | --- | --- |
| OpenAI Videos | `/v1/videos` | `/v1/videos/{task_id}` |
| MiniMax V2 | `/v2/video_generation` | `/v2/query/video_generation/{task_id}` |
| AutoDL | `/api/v1/comfyui/comfyui_workflow/{model}` | `/api/v1/comfyui/comfyui_workflow/result/{task_id}` |
| DashScope Wan | `/api/v1/services/aigc/image2video/video-synthesis` | `/api/v1/tasks/{task_id}` |
| Seedance | `/api/v3/contents/generations/tasks` | `/api/v3/contents/generations/tasks/{task_id}` |
| 自定义路径 | 配置 `custom_create_path` | 配置 `custom_query_path`，必须含 `{task_id}` |

各模式使用各自协议请求体，不混写字段。音频同步在已核对的线上 AutoDL v1.0.27 MiniMax 解码器中使用 `audio_duration`，OpenAI 使用 `seconds`；API 文档同时出现过两种 MiniMax 时长写法，插件以实际解码器可接受的请求为准。六种模式均接受管理员填写的模型 ID；插件不设置模式与模型的白名单。DashScope Wan 会按提示词、图片、音频、视频以及 reference/first_frame/first_last_frame 组合生成 input 字段；上游未开放的组合会由服务端返回明确错误。AutoDL New API 使用工作流 ID，已移除 `MiniMax-H3` / `MiniMax-H3-Max` 旧别名；独立接入其他服务时使用该服务实际支持的模型。

自定义模式还提供 JSON 请求模板、额外请求头、任务 ID/状态/视频 URL/错误字段的 JSON 点路径（支持数组索引），以及成功/失败状态列表。模板占位：`{model}`、`{prompt}`、`{duration}`、`{resolution}`、`{ratio}`、`{size}`、`{reference_mode}`、`{image}`、`{images}`、`{audios}`、`{videos}`、`{stream}`。占位独占一个 JSON 字符串时会保留列表、数字、布尔值类型，避免模板字符串破坏 JSON。

## LLM 函数工具

两个工具都可以由 AstrBot 的 LLM 调用，名称和参数名固定。成功时插件只发送媒体，不发送进度、耗时或成功文字；失败原因和公网链接回退作为工具文字结果返回 LLM，由 bot 回复用户，不直接向聊天发送插件提示。普通命令保留直接反馈。

### `generate_image`

以下字段全部可选。省略字段时使用当前消息文字、参考图片、插件配置和所选接口的默认值；要做图生图，只需把图片随消息发送或引用，不填写图片 URL 字段。

```json
{
  "prompt": "一只戴红围巾的猫，电影感光影",
  "aspect_ratio": "16:9",
  "quality": "high",
  "background": "auto",
  "output_format": "png"
}
```

- `prompt`（`string`，可选）：提示词。省略时使用当前 QQ 消息文本；若消息只有参考图片，则使用通用图片编辑提示。没有提示词和参考图片时会返回用法提示。
- `resolution`（`string`，可选）：插件画质档位 `1K`、`2K`、`4K`；留空使用配置或提示词中的画质。
- `aspect_ratio`（`string`，可选）：比例，例如 `1:1`、`16:9`、`9:16`、`4:3`、`3:4`、`2:3`、`3:2`、`4:5`、`5:4`、`21:9`。OpenAI 模式会将它映射为 `size`，Gemini 官方模式发送为 `imageConfig.aspectRatio`。
- `size`（`string`，可选）：OpenAI 官方尺寸 `auto`、`256x256`、`512x512`、`1024x1024`、`1536x1024`、`1024x1536`、`1792x1024`、`1024x1792`，或 GPT Image 支持的自定义 `WIDTHxHEIGHT`；自定义尺寸要求宽高为 16 的倍数、比例在 1:3 到 3:1、总像素不超过 3840x2160。填写后优先于比例和画质档位生成 OpenAI `size`。
- `quality`（`string`，可选）：GPT Image/Responses 使用 `auto`、`low`、`medium`、`high`、`xhigh`、`max`；旧版 DALL-E 兼容接口可使用 `standard`、`hd`。留空由接口使用默认值。
- `background`（`string`，可选）：`auto`、`transparent`、`opaque`，发送为 OpenAI 的 `background`。
- `output_format`（`string`，可选）：`png`、`jpeg`、`webp`，发送为 OpenAI 的 `output_format`。
- `output_compression`（`number`，可选）：`1` 到 `100` 的压缩率；`0` 或省略不发送。
- `moderation`（`string`，可选）：`auto` 或 `low`，发送为 OpenAI 的 `moderation`。
- `style`（`string`，可选）：`vivid` 或 `natural`，主要适用于 DALL-E 3 兼容模型。
- `n`（`number`，可选）：生成数量；`0` 或省略使用默认值 `1`。插件目前只回复第一张图片。
- `max_num_results`（`number`，可选）：仅用于 OpenAI Responses 图片工具，生成数量范围 `1` 到 `50`；也可用 `n` 作为便捷别名。
- `input_fidelity`（`string`，可选）：编辑参考图的保真度，`low` 或 `high`；没有输入图片时不会发送。具体支持情况取决于 GPT Image 模型。
- `partial_images`（`number`，可选）：流式生成的中间图片数量，范围 `0` 到 `3`；设为 `1`、`2` 或 `3` 时发送该官方字段，插件仍只回复最终图片。
- `action`（`string`，可选）：仅适用于 OpenAI Responses 图片工具，可填 `auto`、`generate` 或 `edit`；留空由接口根据是否有输入图片自动判断。
- 函数不接受 `image_url` 等媒体 URL 参数。调用工具时，插件从当前消息和引用消息读取 QQ 图片、图片文件和 @用户头像；没有图片就是文生图，有图片就是图生图。图片来源顺序与普通自定义提示词相同，按消息组件顺序支持多图。

上述所有字段（包括 `prompt`）都是可选的；未传或传空时使用消息内容、图片配置和接口默认值。当前接口不支持的可选控制字段会被省略，让上游采用默认行为；媒体缺失、格式错误或路由约束不满足时会返回明确提示。`resolution`/`aspect_ratio` 是插件的跨接口抽象；OpenAI 官方 Images API 本身没有独立 `aspect_ratio` 字段，因此插件把它转换为 `size`。Gemini 官方请求只发送其原生的 `responseModalities: ["IMAGE"]`、`imageConfig.aspectRatio` 和 `imageConfig.imageSize` 字段；Chat 模式不发送这些 Gemini 专用字段。

### `generate_video`

工具只提供一个入口，插件按媒体参数自动选择配置中的路线和模型。下面列出的字段全部可选，省略字段时使用配置默认值；只传提示词、只传媒体或两者同时传都可以。工具参数是 JSON 字段，命令中的自然语言写法不需要复制到工具参数里。

```json
{
  "prompt": "人物在海边慢慢转身，镜头平稳推进",
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
| `prompt` | `string`，默认空字符串 | 文生、图生和多媒体提示词；仅动作迁移/音频同步模式可留空，其他类别需要提示词。 |
| `duration` | `integer`，默认 `0` | `0` 使用提示词中的时长或配置默认值；正整数覆盖默认时长。 |
| `resolution` | `string`，默认空字符串 | 例如 `480p`、`768p`；留空使用路线配置。 |
| `aspect_ratio` | `string`，默认空字符串 | 支持 `16:9`、`9:16`、`1:1`、`4:3`、`3:4`、`21:9`、`adaptive`；留空使用路线配置。 |
| `reference_mode` | `string`，默认 `auto` | `auto` 始终按参考图处理，不按数量推断帧模式。明确帧模式可用 `first_frame`、`last_frame`、`first_last_frame`；动作迁移用 `motion_transfer`，自动对口型用 `audio_sync`。特殊模式严格校验媒体数量和是否允许提示词。 |
| `seed` | `integer`，默认 `-2` | `-2` 使用配置，`-1` 不发送种子，非负数发送给支持该字段的接口；不支持的模型会忽略该字段或返回上游错误。 |
| `generate_audio` | `string`，默认 `auto` | `auto` 使用配置；也可填 `true` 或 `false`，主要用于支持生成音频的模型。 |
| `use_message_media` | `boolean`，默认 `true` | 读取当前消息、引用和 @用户头像中的媒体；设为 `false` 忽略消息附带媒体。工具没有媒体 URL 参数。 |

工具调用时，用户只需把提示词和媒体一起发给 QQ，或引用已经发送的媒体；不需要把 QQ 公网 URL 复制到参数中。图片、图片文件、QQ 语音、音频文件、QQ 视频和视频文件都会被转换为组件来源，有 QQ 公网 URL 时保持原值；AstrBot 返回本地缓存或 Base64 时，插件读取媒体并转换为 Data URL，不要求用户另外提供链接。工具会按当前消息和引用中的组件顺序收集图片、音频和视频，不自动加入发送者头像；没有图片时可用 @用户头像作为图片参考。无媒体时根据配置路由为文生视频。

所有字段都可省略；未传时使用上表中的默认值。`duration`、`seed` 等数值字段如果填写了不合法的值会返回具体错误，避免在参数不完整时提交收费任务；不适用于所选接口的可选字段会由协议适配层跳过。

函数工具直接调用生成，不启动命令材料收集。媒体组合与自动路由如下；模型和接口格式均可在“生视频设置 → 自动路由模型”中改为管理员实际可用的值：

| 参数媒体 | 路由 | 默认模型 |
| --- | --- | --- |
| 无图片、音频、视频 | 文生视频 | `minimax_h3_lightx2v_no_pic` |
| 有图片，无音频、视频 | 图生视频 | `minimax_h3_lightx2v_v5`；时长超过 10 秒用 `minimax_h3_lightx2v_v5_15s` |
| `reference_mode=first_last_frame` | 首尾帧生视频 | `minimax_h3_lightx2v` |
| `reference_mode=first_frame` | 首帧生视频 | 使用管理员配置的模型 |
| 有音频，无视频 | 音频生视频或图音生视频 | `minimax_h3_image_audio_to_video_v2`；超过 10 秒用对应 `_15s` |
| 有视频，无图片、音频 | 视频生视频 | 使用管理员配置的模型 |
| 有图片＋视频（可再带音频） | 普通多媒体路线 | 默认模型留空，需要管理员配置 |
| `reference_mode=motion_transfer` | 动作迁移，一图一视频、无提示词 | `wan2.2-animate-move` |
| `reference_mode=audio_sync` | 音频同步，一图一音频、无提示词 | `minimax_h3_image_audio_to_video` |
| `reference_mode=last_frame` | 尾帧，一图和提示词 | 使用管理员配置的模型 |

工具没有 `image_url`、`audio_url`、`video_url` 参数，也不会从提示词中的 URL 读取媒体。每种协议仍使用自己的请求体，模型是否接受某种媒体组合由上游服务决定。

函数工具和命令都不接受 `image_url`、`audio_url`、`video_url` 等媒体 URL 字段。插件从 QQ 消息组件、引用消息和 @用户头像读取媒体；引用链为空时会尝试读取原消息。无媒体时按文字路由为文生视频，有媒体时按图片、音频、视频组合路由；模型或媒体组合不符合要求会返回具体错误，不会静默丢弃媒体。

视频生成完成后，命令和关键词触发会先按“视频下载与发送尝试次数”发送本地 MP4 视频消息，确保 QQ 群聊或私聊收到真正的视频。全部尝试失败后才发送 AutoDL 返回的公网链接；回退消息会显示接口提供的失效时间。如果上游没有返回明确的过期信息，消息会明确标注“以 AutoDL 上游链接有效期为准”，不会虚构固定时长。`generate_video` 工具成功时只发送一条视频媒体，不发送插件进度或成功文字；媒体发送失败时，链接回退作为工具结果交给 Bot，由 Bot 自行组织回复。图片同样按“图片下载与发送尝试次数”重试，默认总尝试次数均为 2；`generate_image` 工具成功时只发送图片，失败回退同样只返回给 Bot。

## 任务、计费与数据

每次调用只创建一次收费任务；POST 连接中断也不自动重新生成，避免重复扣费。后续仅轮询原任务，下载失败仅重试下载，不会重新创建收费任务。等待或查询失败时显示任务 ID，任务记录保存在插件数据目录 `video/*.json`；视频完成但下载失败时保留结果地址，可从上游取回，不必再次付费。公开视频下载不附带 API Key。

费用由上游接口扣取，插件不额外收费、不设置次数额度。`daily_stats.json` 保留旧统计并新增 image/video 分类。MP4 缓存默认保留 24 小时，仅清理插件自己的文件；任务记录和配置备份保留。缓存大小、下载重试/超时、查询间隔和最大等待时间均可配置。热重载关闭 HTTP 会话和清理任务，不重启 AstrBot。

## 开发验证

```bash
python -m py_compile main.py api_manager.py data_manager.py image_manager.py generation_params.py config_manager.py video_router.py video_inputs.py video_api_manager.py utils.py
python -m unittest discover -s tests -v
ruff check main.py config_manager.py data_manager.py video_router.py video_inputs.py video_api_manager.py tests/test_video* tests/test_config_migration.py
```

测试覆盖六种协议请求结构、媒体路由、引用与头像、参数错误、工具输出、一次提交和轮询下载、配置/预设迁移。宿主配置加载器集成测试默认查找旁边的 AstrBot 源码，也可通过 `AIGEN_ASTRBOT_CONFIG_SOURCE` 指定宿主 `astrbot_config.py`；没有源码时该项自动跳过，其余测试独立运行。低成本真实测试仅提交一条 1 秒 480p 文生视频任务；其他模式用本机模拟服务验证，不需要逐模式产生费用。

## 参考与许可

项目：[yiyinfaith/astrbot_plugin_aigen](https://github.com/yiyinfaith/astrbot_plugin_aigen)。配置、消息组件和函数工具依照 AstrBot 当前开发文档实现。视频适配参考 [AutoDL API](https://github.com/yiyinfaith/new-api-plugin-autodl/blob/main/API.md) 和 [Seedance 创建任务文档](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh)。遵守本仓库许可证及上游服务的使用要求。
