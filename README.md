# AstrBot AIGen 多模态图片生成插件

`astrbot_plugin_aigen` 面向 AstrBot 4.x，提供一个统一的图片生成入口。插件保留普通生图、预设关键词、帮助文字和次数计费，并兼容五种请求模式：

- `openai_image`
- `openai_chat`
- `openai_response`
- `gemini_official`
- `custom_endpoint`

## 使用方式

- `/画图 <提示词>`：自定义提示词。`/文生图 <提示词>` 是兼容别名。
- 在消息中附带图片、引用图片或 `@` 用户后使用 `/画图 <提示词>`：使用统一入口进行图生图。
- 发送已配置的预设关键词：触发对应的预设提示词。匹配采用 memelite 同款的模糊规则，关键词出现在消息任意位置都可以触发，例如 `@小明手办化`；关键词前后的普通文字会追加到提示词末尾。
- `/画图帮助`、`/发图帮助`、`/手办化帮助`、`/lm帮助`：发送插件配置中的 `help_text`。
- `/lm列表`、`/lm查看 <关键词>`、`/lm添加 <关键词>:<提示词>`、`/lm删除 <关键词>`：管理用户自定义预设。
- `/画图查询次数`：查看个人次数限制状态和当前群组余额。个人次数限制已取消，历史用户次数文件仍会保留以兼容旧数据；群组计费余额继续生效。
- `/画图增加用户次数 <用户ID> <次数>`、`/画图增加群组次数 <次数>`：管理员维护旧数据或增加群组余额。
- `/切换API模式 <模式>`、`/切换模型 <模型名>`：管理员切换当前请求模式或默认模型。

## LLM 函数工具

插件按 AstrBot 当前函数工具规范注册 `generate_image`。它只有一个图片生成调用入口：

- `prompt` 必填，图片生成或编辑提示词。
- `image_url` 可选，可填写图片 URL、本地路径或 `base64://` 数据。参数为空时是文生图，传入后是图生图。

插件不会再根据上下文猜测文生图或图生图，也不会维护额外的上下文、人设、签到、叛逆或预设参考图功能。`enable_llm_tool`、`llm_show_progress` 和 `llm_cooldown_seconds` 仍可用于控制函数工具。

## 配置和数据安全

基础配置在 AstrBot 管理面板中填写：统一的 `base_url`、`api_keys`、`model`、`image_resolution`、代理、超时、`use_stream` 和 `help_text` 等。文生图与图生图共用同一套接口地址、模型和 Key 池；是否为图生图只由请求中是否带有图片决定。

`use_stream` 对所有请求模式开放。OpenAI Chat、Responses、Gemini 流式端点以及支持该选项的自定义/Images 接口会使用 SSE 或流式标记；上游不支持流式时由接口返回兼容错误，插件不会改变请求类型。

配置中的 `prompt_list` 会原样加载。当前仓库中的默认预设为 44 条，插件不会在启动、热重载或同步时重写它。通过 `/lm添加` 保存的用户预设位于 AstrBot 数据目录的 `user_prompts.json`，计费统计位于 `daily_stats.json`，历史余额文件 `user_counts.json` 和 `group_counts.json` 会继续读取。

## 开发和热重载

```bash
python -m py_compile main.py api_manager.py data_manager.py image_manager.py generation_params.py utils.py
python -m unittest discover -s tests -v
ruff check main.py api_manager.py data_manager.py image_manager.py generation_params.py utils.py tests
```

插件实现了 `terminate()`，热重载时会关闭 HTTP 会话。修改后在 AstrBot WebUI 的插件管理中重载本插件即可，不需要重启 AstrBot。

## 借鉴和许可

项目地址：[yiyinfaith/astrbot_plugin_aigen](https://github.com/yiyinfaith/astrbot_plugin_aigen)。本项目借鉴了早期图片生成插件的请求适配思路，并按照 AstrBot 当前插件开发文档重构。请遵守仓库许可证及上游项目的使用要求。
