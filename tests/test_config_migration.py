import ast
import copy
import json
import logging
import tempfile
import unittest
from pathlib import Path

from config_manager import media_config, migrate_config, schema_defaults

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent.parent / "AstrBot" / "astrbot/core/config/astrbot_config.py"


def loader_class():
    """Exercise the actual installed AstrBot loader, including pre-init pruning."""
    if not CORE.exists():
        raise unittest.SkipTest("本机 AstrBot 源码不可用，跳过宿主加载器集成测试")
    tree = ast.parse(CORE.read_text("utf-8"))
    tree.body = [
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "AstrBotConfig"
    ]
    namespace = {
        "json": json,
        "os": __import__("os"),
        "logger": logging.getLogger("migration-test"),
        "ASTRBOT_CONFIG_PATH": "unused",
        "DEFAULT_CONFIG": {},
        "DEFAULT_VALUE_MAP": {
            "string": "",
            "text": "",
            "bool": False,
            "int": 0,
            "float": 0.0,
            "object": {},
            "list": [],
            "dict": {},
            "template_list": [],
        },
    }
    exec(compile(tree, str(CORE), "exec"), namespace)  # noqa: S102
    return namespace["AstrBotConfig"]


class ConfigMigrationTest(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((ROOT / "_conf_schema.json").read_text("utf-8"))

    def test_actual_loader_preserves_112_complete_prompts_and_all_old_settings(self):
        prompts = [
            f"自定义关键词{i}:珍贵的完整提示词\nretain all text {i}:details "
            + "长文本" * 100
            for i in range(112)
        ]
        old = {
            **copy.deepcopy(schema_defaults(self.schema)["global_settings"]),
            **copy.deepcopy(schema_defaults(self.schema)["image_settings"]),
        }
        old.update(
            prompt_list=prompts,
            api_keys="private-test-value",
            base_url="https://private.test/v1",
            help_text="我的自定义帮助\n全部保留",
            custom_prompt_need_prefix=False,
            use_proxy=True,
            timeout=67,
            llm_cooldown_seconds=0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(old, ensure_ascii=False), "utf-8")
            conf = loader_class()(str(path), schema=self.schema)
            self.assertEqual(conf["prompt_list"], prompts)
            self.assertTrue(migrate_config(conf, Path(tmp) / "backups"))
            image = media_config(conf)
            for key, value in old.items():
                if key != "llm_show_progress":
                    self.assertEqual(image[key], value, key)
            self.assertEqual(conf["image_settings"]["prompt_list"], prompts)
            backup = next((Path(tmp) / "backups").glob("*.json"))
            self.assertEqual(
                json.loads(backup.read_text("utf-8"))["prompt_list"], prompts
            )
            conf["image_settings"]["prompt_list"].append("新增:after migration")
            conf.save_config()
            reload = loader_class()(str(path), schema=self.schema)
            self.assertFalse(migrate_config(reload, Path(tmp) / "backups"))
            self.assertEqual(len(reload["image_settings"]["prompt_list"]), 113)
            self.assertEqual(len(list((Path(tmp) / "backups").glob("*.json"))), 1)

    def test_empty_prompt_list_is_not_replaced_by_44_defaults(self):
        conf = schema_defaults(self.schema)
        conf["prompt_list"] = []
        with tempfile.TemporaryDirectory() as tmp:
            migrate_config(conf, Path(tmp))
        self.assertEqual(conf["image_settings"]["prompt_list"], [])

    def test_new_install_uses_grouped_defaults_without_legacy_copy(self):
        conf = schema_defaults(self.schema)
        conf["image_settings"]["prompt_list"] = ["新用户:只读新的配置"]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(migrate_config(conf, Path(tmp)))
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual(media_config(conf)["prompt_list"], ["新用户:只读新的配置"])
        self.assertTrue(media_config(conf, "video")["enable_llm_tool"])
        self.assertEqual(
            len(self.schema["video_settings"]["items"]["interface_mode"]["options"]), 6
        )

    def test_failed_save_rolls_back_memory_and_retains_backup(self):
        class Broken(dict):
            def save_config(self):
                raise OSError("disk full")

        conf = Broken(schema_defaults(self.schema))
        conf["prompt_list"] = ["恢复:very precious"]
        before = copy.deepcopy(conf)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OSError):
                migrate_config(conf, Path(tmp))
            self.assertEqual(conf, before)
            self.assertTrue(list(Path(tmp).glob("*.json")))
