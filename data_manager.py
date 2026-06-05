"""
群友识别插件 - 数据管理层
负责群友身份映射的存储、缓存、备份与恢复
"""

import asyncio
import json
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from astrbot.api import logger


class DataManager:
    """数据管理器：管理群友身份映射、外号数据库、聊天记录缓存"""

    def __init__(self, plugin_dir: str, config: dict):
        self._plugin_dir = Path(plugin_dir)
        self._config = config
        self._lock = asyncio.Lock()

        # 数据目录
        self._data_dir = self._plugin_dir / "data"
        self._backup_dir = self._data_dir / "backups"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        # 数据文件路径
        self._member_file = self._data_dir / "members.json"       # 群友身份映射
        self._nickname_file = self._data_dir / "nicknames.json"   # 外号数据库
        self._chat_cache_file = self._data_dir / "chat_cache.json" # 聊天记录缓存
        self._correction_log_file = self._data_dir / "correction_log.json" # 修正日志

        # 内存中的数据
        self._members: dict = {}       # {group_id: {qq_id: {"nickname": str, "updated_at": float}}}
        self._nicknames: dict = {}     # {group_id: {qq_id: {"nicknames": [str], "last_extracted": float}}}
        self._chat_cache: dict = {}    # {group_id: [{"qq": str, "nickname": str, "message": str, "time": float}]}
        self._correction_log: list = [] # [{timestamp, operator, group_id, qq_id, old_nickname, new_nickname, action}]

        # 外号提取统计
        self._message_counter: dict = {}  # {group_id: int} 用于自动提取的计数
        self._last_extraction_time: dict = {}  # {group_id: float}

        # 备份状态
        self._last_backup_time: float = 0.0

        # 加载已有数据
        self._load_all()

    # ==================== 数据加载/保存 ====================

    def _load_all(self):
        """加载所有持久化数据到内存"""
        self._members = self._load_json(self._member_file, {})
        self._nicknames = self._load_json(self._nickname_file, {})
        self._chat_cache = self._load_json(self._chat_cache_file, {})
        self._correction_log = self._load_json(self._correction_log_file, [])

        # 重建消息计数器
        for group_id, messages in self._chat_cache.items():
            self._message_counter[group_id] = len(messages)

        logger.info(f"[群友识别] 数据加载完成: {len(self._members)} 个群组, "
                     f"{sum(len(m) for m in self._members.values())} 个群友")

    def _load_json(self, filepath: Path, default):
        """安全加载JSON文件"""
        try:
            if filepath.exists():
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"[群友识别] 加载 {filepath.name} 失败: {e}，使用默认值")
        return default

    async def _save_json(self, filepath: Path, data) -> bool:
        """异步安全保存JSON文件（原子写入）"""
        async with self._lock:
            try:
                tmp_path = filepath.with_suffix(".tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                tmp_path.replace(filepath)  # 原子替换
                return True
            except IOError as e:
                logger.error(f"[群友识别] 保存 {filepath.name} 失败: {e}")
                return False

    async def save_all(self):
        """保存所有数据"""
        await self._save_json(self._member_file, self._members)
        await self._save_json(self._nickname_file, self._nicknames)
        await self._save_json(self._chat_cache_file, self._chat_cache)
        await self._save_json(self._correction_log_file, self._correction_log)

    # ==================== 群友身份映射 ====================

    async def update_member(self, group_id: str, qq_id: str, nickname: str) -> bool:
        """更新/新增群友身份映射"""
        group_id = str(group_id)
        qq_id = str(qq_id)

        if group_id not in self._members:
            self._members[group_id] = {}

        existing = self._members[group_id].get(qq_id)
        if existing and existing.get("nickname") == nickname:
            return False  # 未变化

        self._members[group_id][qq_id] = {
            "nickname": nickname,
            "updated_at": time.time()
        }
        await self._save_json(self._member_file, self._members)
        logger.debug(f"[群友识别] 更新群友: 群={group_id}, QQ={qq_id}, 昵称={nickname}")
        return True

    async def remove_member(self, group_id: str, qq_id: str):
        """移除群友记录（退群时调用）"""
        group_id = str(group_id)
        qq_id = str(qq_id)

        if group_id in self._members and qq_id in self._members[group_id]:
            del self._members[group_id][qq_id]
            await self._save_json(self._member_file, self._members)
            logger.info(f"[群友识别] 移除群友: 群={group_id}, QQ={qq_id}")

    def get_member(self, group_id: str, qq_id: str) -> Optional[dict]:
        """获取单个群友信息"""
        group_id = str(group_id)
        qq_id = str(qq_id)
        return self._members.get(group_id, {}).get(qq_id)

    def get_member_by_nickname(self, group_id: str, nickname: str) -> Optional[tuple]:
        """通过昵称（精确匹配）查找群友，返回 (qq_id, member_info)"""
        group_id = str(group_id)
        for qq_id, info in self._members.get(group_id, {}).items():
            if info.get("nickname") == nickname:
                return qq_id, info
        return None

    def search_members(self, group_id: str, keyword: str) -> list:
        """模糊搜索群友（匹配QQ号或昵称）"""
        group_id = str(group_id)
        results = []
        for qq_id, info in self._members.get(group_id, {}).items():
            nickname = info.get("nickname", "")
            if keyword.lower() in qq_id.lower() or keyword.lower() in nickname.lower():
                results.append((qq_id, info))
        return results

    def get_group_members(self, group_id: str) -> dict:
        """获取群组所有成员"""
        return self._members.get(str(group_id), {})

    def get_member_count(self, group_id: str) -> int:
        """获取群组成员数量"""
        return len(self._members.get(str(group_id), {}))

    # ==================== 外号数据库 ====================

    async def update_nicknames(self, group_id: str, qq_id: str, nicknames: list) -> bool:
        """更新群友外号列表，返回是否有变化"""
        group_id = str(group_id)
        qq_id = str(qq_id)

        if group_id not in self._nicknames:
            self._nicknames[group_id] = {}

        existing = self._nicknames[group_id].get(qq_id, {}).get("nicknames", [])
        new_set = set(n.strip() for n in nicknames if n.strip())
        old_set = set(existing)

        if new_set == old_set:
            return False

        self._nicknames[group_id][qq_id] = {
            "nicknames": sorted(new_set),
            "last_extracted": time.time()
        }
        self._last_extraction_time[group_id] = time.time()
        await self._save_json(self._nickname_file, self._nicknames)
        logger.info(f"[群友识别] 更新外号: 群={group_id}, QQ={qq_id}, 外号={sorted(new_set)}")
        return True

    async def add_nickname(self, group_id: str, qq_id: str, nickname: str):
        """手动添加单个外号"""
        group_id = str(group_id)
        qq_id = str(qq_id)
        nickname = nickname.strip()
        if not nickname:
            return

        if group_id not in self._nicknames:
            self._nicknames[group_id] = {}
        if qq_id not in self._nicknames[group_id]:
            self._nicknames[group_id][qq_id] = {"nicknames": [], "last_extracted": 0}

        nicks = self._nicknames[group_id][qq_id]["nicknames"]
        if nickname not in nicks:
            nicks.append(nickname)
            self._nicknames[group_id][qq_id]["last_extracted"] = time.time()
            await self._save_json(self._nickname_file, self._nicknames)

    async def remove_nickname(self, group_id: str, qq_id: str, nickname: str):
        """删除指定外号"""
        group_id = str(group_id)
        qq_id = str(qq_id)
        if group_id in self._nicknames and qq_id in self._nicknames[group_id]:
            nicks = self._nicknames[group_id][qq_id]["nicknames"]
            if nickname in nicks:
                nicks.remove(nickname)
                await self._save_json(self._nickname_file, self._nicknames)

    def get_nicknames(self, group_id: str, qq_id: str) -> list:
        """获取群友的所有外号"""
        return self._nicknames.get(str(group_id), {}).get(str(qq_id), {}).get("nicknames", [])

    def get_all_nicknames(self, group_id: str) -> dict:
        """获取群组所有外号"""
        return self._nicknames.get(str(group_id), {})

    def can_extract(self, group_id: str) -> bool:
        """检查是否可以执行外号提取（间隔检查）"""
        group_id = str(group_id)
        last = self._last_extraction_time.get(group_id, 0)
        interval = self._config.get("nickname_extraction_interval", 3600)
        return (time.time() - last) >= interval

    # ==================== 聊天记录缓存 ====================

    async def cache_message(self, group_id: str, qq_id: str, nickname: str, message: str):
        """缓存一条群聊消息"""
        group_id = str(group_id)

        if group_id not in self._chat_cache:
            self._chat_cache[group_id] = []

        entry = {
            "qq": str(qq_id),
            "nickname": nickname,
            "message": message[:500],  # 截断过长消息
            "time": time.time()
        }
        self._chat_cache[group_id].append(entry)

        # 保持缓存上限
        max_cache = self._config.get("max_chat_history", 200) * 3  # 留3倍余量用于LLM分析
        if len(self._chat_cache[group_id]) > max_cache:
            self._chat_cache[group_id] = self._chat_cache[group_id][-max_cache:]

        # 更新消息计数
        self._message_counter[group_id] = self._message_counter.get(group_id, 0) + 1

        # 定期异步保存（每100条）
        count = self._message_counter.get(group_id, 0)
        if count % 100 == 0:
            await self._save_json(self._chat_cache_file, self._chat_cache)

    def get_recent_messages(self, group_id: str, limit: int = None) -> list:
        """获取最近的聊天记录"""
        if limit is None:
            limit = self._config.get("max_chat_history", 200)
        messages = self._chat_cache.get(str(group_id), [])
        return messages[-limit:]

    def get_auto_extract_threshold_reached(self, group_id: str) -> bool:
        """检查是否达到自动提取阈值"""
        if not self._config.get("auto_extract_enabled", False):
            return False
        threshold = self._config.get("auto_extract_threshold", 500)
        return self._message_counter.get(str(group_id), 0) >= threshold

    def reset_message_counter(self, group_id: str):
        """重置消息计数器（提取完成后）"""
        self._message_counter[str(group_id)] = 0

    # ==================== 数据备份与恢复 ====================

    async def backup(self) -> Optional[str]:
        """创建数据备份，返回备份文件路径"""
        async with self._lock:
            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_name = f"backup_{timestamp}.zip"
                backup_path = self._backup_dir / backup_name

                # 收集所有数据
                backup_data = {
                    "timestamp": time.time(),
                    "members": self._members,
                    "nicknames": self._nicknames,
                    "chat_cache": self._chat_cache,
                    "correction_log": self._correction_log,
                    "message_counter": self._message_counter,
                    "last_extraction_time": self._last_extraction_time,
                }

                # 写入临时文件再移动
                tmp_path = backup_path.with_suffix(".tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(backup_data, f, ensure_ascii=False, indent=2)
                tmp_path.replace(backup_path)

                # 清理旧备份
                await self._cleanup_old_backups()

                self._last_backup_time = time.time()
                logger.info(f"[群友识别] 数据备份完成: {backup_name}")
                return str(backup_path)

            except Exception as e:
                logger.error(f"[群友识别] 备份失败: {e}")
                return None

    async def _cleanup_old_backups(self):
        """清理旧备份，保留最近N个"""
        max_count = self._config.get("max_backup_count", 10)
        backups = sorted(self._backup_dir.glob("backup_*.zip"), key=os.path.getmtime, reverse=True)
        for old in backups[max_count:]:
            try:
                old.unlink()
                logger.debug(f"[群友识别] 删除旧备份: {old.name}")
            except OSError:
                pass

    async def restore_from_backup(self, backup_filename: str) -> bool:
        """从备份恢复数据"""
        backup_path = self._backup_dir / backup_filename
        if not backup_path.exists():
            logger.error(f"[群友识别] 备份文件不存在: {backup_filename}")
            return False

        async with self._lock:
            try:
                with open(backup_path, "r", encoding="utf-8") as f:
                    backup_data = json.load(f)

                self._members = backup_data.get("members", {})
                self._nicknames = backup_data.get("nicknames", {})
                self._chat_cache = backup_data.get("chat_cache", {})
                self._correction_log = backup_data.get("correction_log", [])
                self._message_counter = backup_data.get("message_counter", {})
                self._last_extraction_time = backup_data.get("last_extraction_time", {})

                await self.save_all()
                logger.info(f"[群友识别] 从备份恢复成功: {backup_filename}")
                return True

            except Exception as e:
                logger.error(f"[群友识别] 备份恢复失败: {e}")
                return False

    async def periodic_backup(self):
        """定期备份循环，由后台任务调用"""
        while True:
            interval = self._config.get("backup_interval", 3600)
            await asyncio.sleep(interval)
            await self.backup()

    def list_backups(self) -> list:
        """列出所有备份文件"""
        backups = []
        for f in sorted(self._backup_dir.glob("backup_*.zip"), key=os.path.getmtime, reverse=True):
            mtime = os.path.getmtime(f)
            size_kb = f.stat().st_size / 1024
            backups.append({
                "filename": f.name,
                "time": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "size_kb": round(size_kb, 1)
            })
        return backups

    # ==================== 修正日志 ====================

    async def log_correction(self, operator_id: str, group_id: str, qq_id: str,
                             old_nickname: str, new_nickname: str, action: str):
        """记录一次修正操作"""
        entry = {
            "timestamp": time.time(),
            "operator": str(operator_id),
            "group_id": str(group_id),
            "qq_id": str(qq_id),
            "old_nickname": old_nickname,
            "new_nickname": new_nickname,
            "action": action  # "update_nickname", "add_nickname", "remove_nickname", "update_member"
        }
        self._correction_log.append(entry)
        # 保留最近1000条修正记录
        if len(self._correction_log) > 1000:
            self._correction_log = self._correction_log[-1000:]
        await self._save_json(self._correction_log_file, self._correction_log)

    def get_correction_history(self, limit: int = 20) -> list:
        """获取修正历史"""
        return self._correction_log[-limit:]

    # ==================== 统计信息 ====================

    def get_statistics(self) -> dict:
        """获取插件运行统计"""
        total_members = sum(len(m) for m in self._members.values())
        total_nicknames = sum(
            len(info.get("nicknames", [])) for group in self._nicknames.values()
            for info in group.values()
        )
        total_cached_messages = sum(len(msgs) for msgs in self._chat_cache.values())
        total_corrections = len(self._correction_log)

        return {
            "group_count": len(self._members),
            "total_members": total_members,
            "total_nicknames": total_nicknames,
            "cached_messages": total_cached_messages,
            "corrections_count": total_corrections,
            "backups_count": len(self.list_backups()),
            "last_backup": datetime.fromtimestamp(self._last_backup_time).strftime(
                "%Y-%m-%d %H:%M:%S") if self._last_backup_time else "从未备份"
        }
