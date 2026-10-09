import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .utils import norm_id


class DataManager:
    def __init__(self, data_dir: Path, config: Any, video_config: Any = None):
        self.data_dir = Path(data_dir)
        self.config = config
        self.video_config = video_config or {}
        self._stats_lock = asyncio.Lock()

        self.daily_stats_file = self.data_dir / "daily_stats.json"

        # [Fix] 确保数据目录存在
        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)

        self.daily_stats: dict[str, Any] = {}
        self.prompt_map: dict[str, str] = {}
        self.video_prompt_map: dict[str, str] = {}

    async def initialize(self):
        if not self.daily_stats_file.exists():
            self.daily_stats = {"date": "", "users": {}, "groups": {}}
        else:
            await self._load_json(self.daily_stats_file, "daily_stats")

        self.reload_prompts()

    async def _load_json(self, file_path: Path, attr_name: str):
        if not file_path.exists():
            return
        try:
            content = await asyncio.to_thread(file_path.read_text, "utf-8")
            setattr(self, attr_name, json.loads(content))
        except (OSError, ValueError, TypeError) as e:
            logger.error(f"Failed to load {file_path}: {e}")

    async def _save_json(self, file_path: Path, data: Any):
        try:
            content = json.dumps(data, indent=4, ensure_ascii=False)

            def save():
                temporary = file_path.with_suffix(".tmp")
                temporary.write_text(content, "utf-8")
                temporary.replace(file_path)

            await asyncio.to_thread(save)
        except (OSError, ValueError, TypeError) as e:
            logger.error(f"Failed to save {file_path}: {e}")

    def reload_prompts(self):
        for config, prompt_map in (
            (self.config, self.prompt_map),
            (self.video_config, self.video_prompt_map),
        ):
            prompt_map.clear()
            prompt_list = config.get("prompt_list", [])
            if isinstance(prompt_list, list):
                for item in prompt_list:
                    if isinstance(item, str) and ":" in item:
                        k, v = item.split(":", 1)
                        if k.strip() and v.strip():
                            prompt_map[k.strip()] = v.strip()

    def get_prompt(self, key: str) -> str | None:
        return self.prompt_map.get(key)

    async def record_usage(self, uid: str, gid: str | None, kind: str = "image"):
        async with self._stats_lock:
            today = datetime.now().astimezone().strftime("%Y-%m-%d")
            if self.daily_stats.get("date") != today:
                self.daily_stats = {"date": today, "users": {}, "groups": {}}
            uid, gid = norm_id(uid), norm_id(gid)
            self.daily_stats["users"][uid] = self.daily_stats["users"].get(uid, 0) + 1
            if gid:
                self.daily_stats["groups"][gid] = (
                    self.daily_stats["groups"].get(gid, 0) + 1
                )
            media_stats = self.daily_stats.setdefault("by_media", {}).setdefault(
                kind, {"users": {}, "groups": {}}
            )
            media_stats["users"][uid] = media_stats["users"].get(uid, 0) + 1
            if gid:
                media_stats["groups"][gid] = media_stats["groups"].get(gid, 0) + 1
            await self._save_json(self.daily_stats_file, self.daily_stats)
