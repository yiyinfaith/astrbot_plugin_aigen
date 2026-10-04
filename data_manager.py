import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from astrbot.api import logger
from .utils import norm_id


class DataManager:
    def __init__(self, data_dir: Path, config: Any):
        self.data_dir = Path(data_dir)
        self.config = config

        self.user_counts_file = self.data_dir / "user_counts.json"
        self.group_counts_file = self.data_dir / "group_counts.json"
        self.daily_stats_file = self.data_dir / "daily_stats.json"
        self.preset_images_file = self.data_dir / "preset_images.json"
        self.user_prompts_file = self.data_dir / "user_prompts.json"
        self.preset_images_dir = self.data_dir / "preset_images"
        self.fonts_dir = self.data_dir / "fonts"

        # [Fix] 确保数据目录存在
        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)

        if not self.preset_images_dir.exists():
            self.preset_images_dir.mkdir(parents=True, exist_ok=True)

        if not self.fonts_dir.exists():
            self.fonts_dir.mkdir(parents=True, exist_ok=True)

        self.user_counts: Dict[str, int] = {}
        self.group_counts: Dict[str, int] = {}
        self.daily_stats: Dict[str, Any] = {}
        self.preset_images: Dict[str, str] = {}
        self.user_prompts: Dict[str, str] = {}
        self.prompt_map: Dict[str, str] = {}

    async def initialize(self):
        await self._load_json(self.user_counts_file, "user_counts")
        await self._load_json(self.group_counts_file, "group_counts")
        await self._load_json(self.user_prompts_file, "user_prompts")
        if not self.daily_stats_file.exists():
            self.daily_stats = {"date": "", "users": {}, "groups": {}}
        else:
            await self._load_json(self.daily_stats_file, "daily_stats")

        await self._load_json(self.preset_images_file, "preset_images")
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
        # 内置预设
        base_cmd_map = {
            "手办化": "figurine_1",
            "手办化2": "figurine_2",
            "手办化3": "figurine_3",
            "手办化4": "figurine_4",
            "手办化5": "figurine_5",
            "手办化6": "figurine_6",
            "Q版化": "q_version",
            "痛屋化": "pain_room_1",
            "痛屋化2": "pain_room_2",
            "痛车化": "pain_car",
            "cos化": "cos",
            "cos自拍": "cos_selfie",
            "孤独的我": "clown",
            "第三视角": "view_3",
            "鬼图": "ghost",
            "第一视角": "view_1",
        }
        for k in base_cmd_map.keys():
            self.prompt_map[k] = "[内置预设]"

        # 配置中的 prompts (兼容旧版)
        prompts_cfg = self.config.get("prompts", {})
        if isinstance(prompts_cfg, dict):
            for k, v in prompts_cfg.items():
                if isinstance(v, dict) and "default" in v:
                    self.prompt_map[k] = v["default"]
                elif isinstance(v, str):
                    self.prompt_map[k] = v

        # Prompt List (Config)
        prompt_list = self.config.get("prompt_list", [])
        if isinstance(prompt_list, list):
            for item in prompt_list:
                if ":" in item:
                    k, v = item.split(":", 1)
                    self.prompt_map[k.strip()] = v.strip()

        # User Prompts (Persistence) - 优先级最高，覆盖前面的
        for k, v in self.user_prompts.items():
            self.prompt_map[k] = v

    def get_prompt(self, key: str) -> Optional[str]:
        return self.prompt_map.get(key)

    async def add_user_prompt(self, key: str, prompt: str):
        """添加或更新用户预设，并持久化保存"""
        self.user_prompts[key] = prompt
        await self._save_json(self.user_prompts_file, self.user_prompts)
        self.reload_prompts()

    async def remove_user_prompt(self, key: str) -> bool:
        """Remove a user-defined prompt preset and persist the change."""
        if key not in self.user_prompts:
            return False
        del self.user_prompts[key]
        await self._save_json(self.user_prompts_file, self.user_prompts)
        self.reload_prompts()
        return True

    # --- 积分相关 ---
    def get_user_count(self, uid: str) -> int:
        return self.user_counts.get(norm_id(uid), 0)

    async def decrease_user_count(self, uid: str, amount: int = 1):
        uid = norm_id(uid)
        count = self.get_user_count(uid)
        if amount <= 0 or count <= 0:
            return
        self.user_counts[uid] = count - min(amount, count)
        await self._save_json(self.user_counts_file, self.user_counts)

    async def add_user_count(self, uid: str, amount: int):
        uid = norm_id(uid)
        self.user_counts[uid] = self.get_user_count(uid) + amount
        await self._save_json(self.user_counts_file, self.user_counts)

    def get_group_count(self, gid: str) -> int:
        return self.group_counts.get(norm_id(gid), 0)

    async def decrease_group_count(self, gid: str, amount: int = 1):
        gid = norm_id(gid)
        count = self.get_group_count(gid)
        if amount <= 0 or count <= 0:
            return
        self.group_counts[gid] = count - min(amount, count)
        await self._save_json(self.group_counts_file, self.group_counts)

    async def add_group_count(self, gid: str, amount: int):
        gid = norm_id(gid)
        self.group_counts[gid] = self.get_group_count(gid) + amount
        await self._save_json(self.group_counts_file, self.group_counts)

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

    # --- 预设图片管理 ---
    async def save_preset_image(self, preset_key: str, image_bytes: bytes):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{preset_key}_{timestamp}.png"
            filepath = self.preset_images_dir / filename
            await asyncio.to_thread(filepath.write_bytes, image_bytes)

            if preset_key in self.preset_images:
                old_f = self.preset_images_dir / self.preset_images[preset_key]
                if old_f.exists():
                    await asyncio.to_thread(old_f.unlink)

            self.preset_images[preset_key] = filename
            await self._save_json(self.preset_images_file, self.preset_images)
        except Exception as e:
            logger.error(f"Save preset img error: {e}")

    def get_preset_image_path(self, preset_key: str) -> Optional[str]:
        if preset_key not in self.preset_images:
            return None
        f_path = self.preset_images_dir / self.preset_images[preset_key]
        return str(f_path) if f_path.exists() else None

    # [新增] 统计与清理功能
    async def cleanup_old_presets(self, days: int) -> int:
        count = 0
        now = datetime.now()
        for k, v in list(self.preset_images.items()):
            p = self.preset_images_dir / v
            if p.exists():
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                if (now - mtime).days > days:
                    await asyncio.to_thread(p.unlink)
                    del self.preset_images[k]
                    count += 1
            else:
                del self.preset_images[k]  # Clean broken link
        if count > 0:
            await self._save_json(self.preset_images_file, self.preset_images)
        return count

    def get_preset_stats(self) -> Tuple[int, float]:
        """返回 (数量, MB大小)"""
        total_size = 0
        count = 0
        for v in self.preset_images.values():
            p = self.preset_images_dir / v
            if p.exists():
                total_size += p.stat().st_size
                count += 1
        return count, total_size / (1024 * 1024)
