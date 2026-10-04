import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from astrbot.api import logger
from .utils import norm_id


class DataManager:
    def __init__(self, data_dir: Path, config: Any):
        self.data_dir = Path(data_dir)
        self.config = config

        self.daily_stats_file = self.data_dir / "daily_stats.json"

        # [Fix] 确保数据目录存在
        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)

        self.daily_stats: Dict[str, Any] = {}
        self.prompt_map: Dict[str, str] = {}

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
        except Exception as e:
            logger.error(f"Failed to load {file_path}: {e}")

    async def _save_json(self, file_path: Path, data: Any):
        try:
            content = json.dumps(data, indent=4, ensure_ascii=False)
            await asyncio.to_thread(file_path.write_text, content, "utf-8")
        except Exception as e:
            logger.error(f"Failed to save {file_path}: {e}")

    def reload_prompts(self):
        self.prompt_map.clear()
        prompt_list = self.config.get("prompt_list", [])
        if isinstance(prompt_list, list):
            for item in prompt_list:
                if ":" in item:
                    k, v = item.split(":", 1)
                    self.prompt_map[k.strip()] = v.strip()

    def get_prompt(self, key: str) -> Optional[str]:
        return self.prompt_map.get(key)

    async def record_usage(self, uid: str, gid: Optional[str]):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_stats.get("date") != today:
            self.daily_stats = {"date": today, "users": {}, "groups": {}}

        uid = norm_id(uid)
        self.daily_stats["users"][uid] = self.daily_stats["users"].get(uid, 0) + 1
        if gid:
            gid = norm_id(gid)
            self.daily_stats["groups"][gid] = self.daily_stats["groups"].get(gid, 0) + 1
        await self._save_json(self.daily_stats_file, self.daily_stats)
