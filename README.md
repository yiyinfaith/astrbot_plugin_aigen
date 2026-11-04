# AstrBot AIGen 多模态生成器

<p align="center"><img src="https://raw.githubusercontent.com/yiyinfaith/astrbot_plugin_aigen/main/logo.png" alt="AIGen Logo" width="160" height="160" /></p>

AstrBot 的生图、生视频、生音频插件：按输入材料自动选择路线与模型，支持多种上游协议、按模型计费、余额与卡密充值，并提供 `generate_image` / `generate_video` / `generate_audio` 函数工具。

💡 **生视频 API 推荐：** [AutoDL](https://www.autodl.art/large-model/comfyui)，成本较低，几分钱/秒。（非广告）

🔌 **AutoDL 接入 NewAPI 插件推荐：** [new-api-plugin-autodl](https://github.com/yiyinfaith/new-api-plugin-autodl)。

## 快速开始

1. 安装插件后进入 **AstrBot WebUI → 插件 → AIGen → 配置**。
2. 在 🎨 生图 / 🎬 生视频 / 🎵 生音频 设置中选择接口格式，填写接口地址和 API Keys（生音频地址与 Keys 留空时跟随生视频）。
3. 检查各「自动路由模型」中的模型 ID，留空的路线不会提交任务。
4. 需要收费时，进入 **插件 → AIGen → 管理中心** 设置模型价格并生成卡密；不设置价格则全部免费。

## 用户命令

| 命令 | 说明 |
| --- | --- |
| `/生图 <提示词>`、`/画图 <提示词>` | 无图文生图，带图（发送、引用或 @头像）图生图，支持多图 |
| `/<图片预设关键词>` | 使用预设提示词，只取一张图：引用图 > 发送图 > @用户头像 > 发送者头像 |
| `/生视频`、`/生成视频`、`/视频` | 开始录入视频材料 |
| `/生音频 朗读\|提示词 [类型]`、`/音频 ...` | 开始录入音频材料，类型见[音频](#音频) |
| `/生成` | 提交已录入的材料 |
| `/设置`、`/设置重置` | 录入中修改 / 清除本次详细参数 |
| `/清除失败项` | 清除读取失败的材料，保留已成功的部分 |
| `/取消` | 清空材料并退出录入 |
| `/AI余额`、`/AI账单 [页码]` | 查询余额、自己的生成账单 |
| `/兑换AI卡密 <卡密>` | 充值，建议私聊 |
| `/AI帮助`、`/AI菜单` | 显示管理员配置的帮助文本（QQ 以合并转发发送） |

`/ai余额`、`/ai账单`、`/兑换ai卡密`、`/ai帮助`、`/ai菜单` 为小写别名。

## 使用说明

### 录入流程（视频、音频）

1. 发送启动指令，Bot 提示开始录入及剩余秒数。
2. 依次发送文字、图片、QQ 语音、视频或文件（也可引用），期间不逐条回复；多段文字按顺序合并为一个提示词。
3. 需要时发送 `/设置`，按菜单回复要修改的选项编号（空格分隔，如 `5 8`），`0` 退出。
4. 发送 `/生成`，Bot 汇总材料（如 `共收到1图片，0音频，0视频，2段提示词，生成中`）后提交。成功只发送成品。

- 整个录入从启动起共用一个时限（默认 120 秒），`/设置` 和类型询问不暂停计时，超时自动取消。
- 群内多人可同时录入，每人只收集自己的消息，互不混入。
- 材料读取失败或参数不合法时不会提交，可补充或 `/清除失败项` 后再 `/生成`。
- 下载或发送失败不会重新生成，会返回任务 ID 和上游公网链接。

### 图片

- 自定义生图：`/生图 <提示词>`（或 `/画图 <提示词>`）；预设生图：直接发送预设关键词，如 `手办化`。
- 无图走「文生图」，有图走「图生图」路线。指令使用管理员默认参数，不从提示词中解析比例或画质。
- 图片传入方法包括：与指令一起发送、引用图片、@别人。优先级：引用图 > 发送图 > @用户头像，不自动加入发送者头像。
- 预设关键词按完整词匹配规则：前后须为消息边界、空白或 QQ @片段。`手办化`、`@小明 手办化` 可触发，`手办化123` 不触发；重叠时取最长关键词。
- 一次返回多张图片时全部发送，单张失败不影响其他图片。

### 视频

普通 `/生视频` 收到的所有图片都作为参考图。只有在启动消息或第一段文字中写明 `首尾帧`、`首帧`、`尾帧`、`动作迁移`、`音频同步` 时才走对应特殊路线。

| 启动方式 | 材料 | 路线 | 默认模型 |
| --- | --- | --- | --- |
| `/生视频` | 提示词 | 文生视频 | `minimax_h3_lightx2v_no_pic` |
| `/生视频` | 提示词 + 1–9 图 | 图生视频 | `minimax_h3_lightx2v_v5`（>10 秒 `_15s`） |
| `/生视频` | 提示词 + 1–3 音频（可加 1–9 图） | 音频 / 图片＋音频生视频 | `minimax_h3_image_audio_to_video_v2`（>10 秒 `_15s`） |
| `/生视频` | 提示词 + 视频（可加图片、音频） | 视频生视频及其组合 | 需配置 |
| `/生视频 首尾帧` | 提示词 + 2 图（按顺序为首帧、尾帧） | 首尾帧 | `minimax_h3_lightx2v` |
| `/生视频 首帧` | 提示词 + 1 图 | 首帧 | 需配置 |
| `/生视频 尾帧` | 提示词 + 1 图 | 尾帧 | 需配置 |
| `/生视频 动作迁移` | 1 图 + 1 视频，无提示词 | 动作迁移 | `wan2.2-animate-move` |
| `/生视频 音频同步` | 1 图 + 1 音频，无提示词 | 对口型 | `minimax_h3_image_audio_to_video` |

时长、比例、画质可在录入时发送 `/设置` 选择，或在文字中写 `--duration 5`（提示词里的“5秒”等描述不会被当作参数）。插件不会丢弃多余媒体、截断时长或降分辨率；材料不符合模型要求时直接提示，不提交收费任务。

### 音频

第一个参数必填：`朗读`（原文转语音 TTS）或 `提示词`（按描述生成声音 TTA）。第二个参数可选，未指定且带音频时 `/生成` 后会询问类型，直接回复类型名即可。

| 启动方式 | 材料 | 路线 | 默认模型 |
| --- | --- | --- | --- |
| `/生音频 提示词` | 提示词 | 提示词生音频 TTA | 需配置 |
| `/生音频 朗读` | 原文 | 文字生音频 TTS | 需配置 |
| `/生音频 朗读 音色克隆` | 原文 + 1–3 段参考音频 | 音色克隆 | `IndexTTS-2` |
| `/生音频 提示词 参考音频生成` | 提示词 + 1–3 段参考音频 | 音频条件 TTA | 需配置 |
| `/生音频 提示词 音频编辑` | 编辑指令 + 1 段原音频 | 音频编辑 | 需配置 |
| `/生音频 朗读 语音编辑` | 修改后的文字 + 1 段原语音 | 语音编辑 | 需配置 |
| `/生音频 朗读 音色转换` | 2 段音频：原语音、目标音色，无文字 | 音色转换 | 需配置 |

音频预设只用于音频条件 TTA：发送 `/<关键词>` 后再发参考音频并 `/生成`。

## 配置教程

### 触发前缀与预设

- **前缀开关**：「需要 AstrBot 前缀」开启时须发 `/生图 猫` 或 @机器人；关闭后直接发 `生图 猫` 也能触发。
- **预设列表**：每行一条 `关键词:完整提示词`，例如 `手办化:将图片中的人物制作成精致的 PVC 手办，放在桌面上`。图片、视频、音频的预设分开保存。
- **自定义前缀**：在 `extra_prefix` 中添加，如 `画画`、`做视频`；内置的 `/生图`、`/生视频`、`/生音频` 始终可用。

### 计费、余额与卡密

在 **插件 → AIGen → 管理中心** 中管理：

- **模型定价**：填写模型 ID 和价格表达式，实时校验并预览各档位价格，可一键套用示例。
- **卡密管理**：按面额生成 1–100 张卡密、筛选、导出、批量删除（不影响已兑换记录）。
- **用户与记录**：查看/修改余额（可填写原因）、兑换记录、生成账单和管理员操作日志。
- **液态玻璃**：页面右上角的开关可开启 WebGL 液态玻璃特效，默认关闭（使用与 AstrBot 一致的简洁样式）；特效较耗性能，低配设备建议保持关闭。

规则：

- 未配置价格或价格为 0 的模型免费。价格按最终路由到的模型 ID 精确匹配，长时模型需单独定价。
- 余额按「平台 + 用户 ID」保存，群聊与私聊共用。单位由管理员自定（如元），保留 6 位小数。
- 余额 ≤ 0 时不能使用收费模型；固定价格的模型要求余额不少于该价格。按实际用量计费的模型在完成后扣费，余额可能变为负数。
- 同一用户同一时间只能有一个收费任务。生成失败、取消或无法核验用量时不扣费。
- 表达式不合法时，该模型会提示「价格设置不合法，请联系管理员配置模型价格。」并停止提交。

### 价格表达式教程

表达式语法与 NewAPI 计费表达式一致，可加 `v1:` 前缀。最外层用 `tier("档位名", 金额)` 包住，档位名会显示在价格预览和账单中。

**1. 按次收费**

```javascript
tier("base", fixed(0.2))
```

**2. 按张收费**（图片实际成功张数）

```javascript
tier("base", u("image_count") * 0.1)
```

**3. 按成品秒数收费**（视频/音频以成品实测时长为准，无法测量时不收费）

```javascript
tier("base", u("actual_seconds") * 0.1)
```

`u("seconds")` 与之相同，但对 `IndexTTS-2` 不足 10 秒按 10 秒计（与 AutoDL 一致）。

**4. 按 Token 收费**（单位是每百万 Token，这里输入 2、输出 8）

```javascript
tier("base", p * 2 + c * 8)
```

**5. 按字符收费**（每千字符 0.02）

```javascript
tier("base", characters * 0.02 / 1000)
```

**6. 条件档位**：`条件 ? 满足时 : 否则`，可嵌套。

```javascript
u("resolution") == "768p"
  ? tier("high", u("seconds") * 0.2)
  : tier("base", u("seconds") * 0.1)
```

```javascript
param("quality") == "high" ? tier("high", 0.3) : tier("base", 0.15)
```

**7. 分时段价格**（北京时间 0–8 点每秒 0.03，其余时间 0.04；按提交时刻计）

```javascript
hour("Asia/Shanghai") < 8
  ? tier("off_peak", u("seconds") * 0.03)
  : tier("peak", u("seconds") * 0.04)
```

**8. 按块计费**（每开始 5 秒计 0.2）

```javascript
tier("base", ceil(u("seconds") / 5) * 0.2)
```

**9. 长上下文阶梯**（缓存读取单独定价）

```javascript
len <= 32000
  ? tier("short", p * 2 + c * 8 + cr * 0.2)
  : tier("long", p * 4 + c * 12 + cr * 0.4)
```

> 每个代码块是一条完整表达式，单独粘贴到输入框；数字仅为示例。

可用内容速查：

| 类别 | 写法 | 含义 |
| --- | --- | --- |
| 用量 `u()` | `u("requests")` | 请求次数（1） |
| | `u("seconds")`、`u("actual_seconds")` | 成品秒数（前者含 IndexTTS-2 的 10 秒下限） |
| | `u("image_count")` | 实际成功图片数 |
| | `u("resolution")`、`u("quality")` | 本次分辨率、画质 |
| | `u("orientation")` | `landscape` / `portrait` / `square` |
| | `u("input_tokens")`、`u("output_tokens")`、`u("total_tokens")` | 上游返回的 Token 数 |
| Token 变量 | `p`、`c`、`len` | 输入、输出、完整输入长度（每百万计） |
| | `cr`、`cc`、`cc1h` | 缓存读取、缓存创建、1 小时缓存创建 |
| | `img`、`img_cr`、`img_o`、`ai`、`ao` | 图片输入/缓存/输出，音频输入/输出 |
| 其他变量 | `characters` | 文本字符数 |
| 请求参数 | `param("duration")`、`param("quality")`、`param("voice")` 等 | 本次生效的生成参数 |
| | `has(param("size"), "1024")` | 字符串包含判断 |
| 时间 | `hour("Asia/Shanghai")`，同理 `minute`、`weekday`、`month`、`day` | 括号内为 IANA 时区名，按提交时刻计算；`weekday` 周日为 0 |
| 数学 | `min(a,b)`、`max(a,b)`、`abs`、`ceil`、`floor` | |
| 运算 | `+ - * / %`、`== != < <= > >=`、`&& \|\| !`、`? :` | |

注意：引用了缓存或图片/音频子类 Token 时，会自动从 `p`/`c` 中扣除，避免重复收费。不支持循环、自定义函数和高级 GJSON 查询，不支持的写法会直接报错。

### 生成参数默认值（JSON）

优先级：**函数工具参数或本次 `/设置` > 路线默认值 > 板块默认值 > 上游默认值**。只填当前模型支持的字段，留空即可不使用。

```json
{"duration": 10, "resolution": "768p"}
```

音频示例：`{"speed": 1.2}`。可让 Bot 调用 `get_generation_capabilities` 函数工具查看各路线支持的参数、范围和默认值（不含地址与 Key）。

### 自定义视频接口

`interface_mode` 选 `custom_endpoint` 后配置：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `custom_create_path` | `/v1/videos` | 创建路径，可含 `{model}` |
| `custom_query_path` | `/v1/videos/{task_id}` | 查询路径，必须含 `{task_id}` |
| `custom_body_template` | OpenAI 风格 | 请求 JSON 模板 |
| `custom_headers` | `{}` | 额外请求头，Key 由插件以 Bearer 添加 |
| `custom_id_path` / `custom_status_path` / `custom_url_path` / `custom_error_path` | `id` / `status` / `url` / `error.message` | 响应字段的点路径，支持数组下标如 `output.results.0.url` |
| `custom_success_status` / `custom_failure_status` | `completed,succeeded,success` / `failed,failure,cancelled,canceled,expired` | 逗号分隔，忽略大小写 |

模板占位符：`{model}`、`{prompt}`、`{duration}`、`{resolution}`、`{ratio}`、`{size}`、`{reference_mode}`、`{image}`、`{images}`、`{audios}`、`{videos}`、`{stream}`。整个值只有一个占位符时保留 JSON 类型（数字、数组等）：

```json
{"model": "{model}", "prompt": "{prompt}", "seconds": "{duration}", "images": "{images}"}
```

### 自定义音频接口

在路线或板块的 `parameter_defaults` 中写 `adapter` 对象，各项均可省略；同步接口把 `query_path` 设为 `""`：

```json
{
  "speed": 1.2,
  "adapter": {
    "create_path": "/v2/video_generation",
    "query_path": "/v2/query/video_generation/{task_id}",
    "body_template": {"model": "{model}", "prompt": "{prompt}", "audios": "{audios}", "speed": "{speed}"},
    "headers": {},
    "id_path": "id",
    "status_path": "status",
    "url_path": "url",
    "error_path": "error.message",
    "base64_path": "audio_base64",
    "success_status": "done,finished",
    "failure_status": "aborted"
  }
}
```

占位符：`{model}`、`{text}`/`{prompt}`、`{audios}`、`{audio}`、`{source_audio}`、`{target_audio}`、`{route}`，以及 `{voice}`、`{format}`、`{speed}` 等生成参数。

## 函数工具

| 工具 | 说明 |
| --- | --- |
| `generate_image` | 生图；可设尺寸、比例、画质、背景、格式、数量、`seed`、`temperature` 等，`use_message_media` 控制是否使用消息中的图片 |
| `generate_video` | 生视频；可设时长、比例、分辨率、参考模式、种子、声音、水印及 DashScope / Seedance 专有参数 |
| `generate_audio` | 生音频；参数 `text`、`mode`、`audio_type`，可设音色、语速、格式、语言及 MiniMax / IndexTTS-2 情感参数 |
| `get_generation_capabilities` | 查询已配置路线、模型、支持参数与默认值，`media_type` 为 `image` / `video` / `audio` / `all`，只查询不生成 |

工具成功时只发送成品，冷却时间由 `llm_cooldown_seconds` 控制。

## 交流与反馈

使用问题、Bug 反馈和功能建议欢迎加群交流：

| 吟枢苑✨插件交流群 | 吟枢苑✨吟窝（娱乐群） |
| :---: | :---: |
| 群号：529924533 | 群号：1059351824 |
| <img src="https://raw.githubusercontent.com/yiyinfaith/astrbot_plugin_aigen/main/assets/qq-group-plugin.jpg" alt="插件交流群二维码" width="240" /> | <img src="https://raw.githubusercontent.com/yiyinfaith/astrbot_plugin_aigen/main/assets/qq-group-chat.jpg" alt="娱乐群二维码" width="240" /> |

## 参考与许可

项目：[yiyinfaith/astrbot_plugin_aigen](https://github.com/yiyinfaith/astrbot_plugin_aigen)。配置、消息组件和函数工具依照 AstrBot 当前开发文档实现。视频适配参考 [AutoDL API](https://github.com/yiyinfaith/new-api-plugin-autodl/blob/main/API.md) 和 [Seedance 创建任务文档](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh)。遵守本仓库许可证及上游服务的使用要求。
