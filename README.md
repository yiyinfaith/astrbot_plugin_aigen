# AstrBot AIGen 图片生成器

这是一个面向当前 AstrBot 插件加载机制的图片生成插件。它只保留生产环境需要的功能：

- `/画图 <自定义提示词>`（`/文生图` 兼容别名）支持纯文生图；附带图片、引用图片或 `@` 用户时支持图生图。
- 发送配置中的预设关键词触发预设提示词，例如 `#手办化`、`#三视图` 或自定义关键词。
- `/画图帮助`、`/发图帮助`、`/手办化帮助`、`/lm帮助` 发送插件配置里的 `help_text`。
- 用户/群组次数计费、查询和管理员增加次数。
- 保留 `openai_image`、`openai_chat`、`openai_response`、`gemini_official` 和 `custom_endpoint` 五种请求模式，以及模型、专用文生图接口、代理、分辨率和比例设置。

批量生成、PDF、上下文记忆、LLM 工具、人设拍照、签到、叛逆回复和预设参考图管理等旧功能已从运行代码中移除，避免旧版事件处理器重复触发。

## 配置和数据安全

插件使用 AstrBot 的配置文件 `data/config/astrbot_plugin_aigen_config.json`。从旧版迁移时只需把原配置中的 `prompt_list` 原样复制到新配置；现有 44 个预设提示词不会被重写或删除。通过 `/lm添加` 新建的预设保存到 AstrBot 数据目录的 `user_prompts.json`，通过 `/lm删除` 只能删除这类用户预设，配置文件里的原始预设不会被误删。

运行时计数和预设文件全部位于 AstrBot 数据目录，不写入插件仓库。仓库的 `.gitignore` 也会阻止这些运行时文件进入 Git。

## 开发和热重载

```bash
python -m py_compile main.py api_manager.py data_manager.py image_manager.py
python -m unittest discover -s tests -v
```

在 AstrBot WebUI 的插件管理中选择“重载插件”即可应用修改，无需重启 AstrBot。插件实现了 `terminate()`，热重载时会关闭旧的 HTTP 会话，避免旧实例残留。

## 借鉴和许可

项目地址：[yiyinfaith/astrbot_plugin_aigen](https://github.com/yiyinfaith/astrbot_plugin_aigen)。本项目借鉴了早期图片生成插件的请求适配思路，并按 AstrBot 当前插件开发文档重构。请按仓库中的许可证要求使用和发布。
