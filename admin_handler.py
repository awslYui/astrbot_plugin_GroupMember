"""
群友识别插件 - 管理员交互修正系统
提供自然语言交互界面，允许管理员修正群友身份映射和外号数据
"""

import asyncio
import re
from datetime import datetime
from typing import Optional

from astrbot.api import logger

from .data_manager import DataManager


class AdminHandler:
    """管理员交互修正处理器"""

    # 修正确认状态
    _pending_confirmations: dict = {}  # {session_key: {"action": ..., "data": {...}, "expires": float}}

    def __init__(self, data_manager: DataManager, config: dict):
        self._dm = data_manager
        self._config = config

    async def process_admin_command(self, event, group_id: str, message: str) -> str:
        """
        处理管理员自然语言修正指令
        返回响应文本
        """
        message = message.strip()

        # 修正确认响应处理
        session_key = f"{group_id}_{event.get_sender_id()}"
        if session_key in self._pending_confirmations:
            return await self._handle_confirmation(session_key, message)

        # 指令解析
        cmd = message.lower()

        # === 修正命令 ===
        # 格式: 修正 QQ号 新昵称 或 修正外号 QQ号 添加/删除 外号
        if cmd.startswith("修正 ") or cmd.startswith("correction ") or cmd.startswith("修改 "):
            return await self._handle_correction_command(event, group_id, message)

        # 格式: 添加外号 QQ号 外号
        if cmd.startswith("添加外号"):
            return await self._handle_add_nickname(event, group_id, message)

        # 格式: 删除外号 QQ号 外号
        if cmd.startswith("删除外号"):
            return await self._handle_remove_nickname(event, group_id, message)

        # 格式: 查找 QQ号
        if cmd.startswith("查找 ") or cmd.startswith("find ") or cmd.startswith("搜索 "):
            return await self._handle_search(event, group_id, message)

        # 格式: 查看修正记录
        if cmd in ("查看修正记录", "修正记录", "修正历史", "correction history"):
            return await self._handle_view_history()

        # 格式: 恢复备份 文件名
        if cmd.startswith("恢复备份 ") or cmd.startswith("restore "):
            return await self._handle_restore(event, message)

        # 格式: 列出备份
        if cmd in ("列出备份", "备份列表", "list backups"):
            return await self._handle_list_backups()

        return None  # 未匹配任何命令

    # ==================== 修正确认流程 ====================

    async def request_confirmation(self, event, group_id: str, action: str, data: dict) -> str:
        """请求管理员确认操作"""
        session_key = f"{group_id}_{event.get_sender_id()}"
        timeout = self._config.get("correction_confirm_timeout", 60)

        self._pending_confirmations[session_key] = {
            "action": action,
            "data": data,
            "expires": asyncio.get_event_loop().time() + timeout
        }

        # 生成确认提示
        if action == "update_member":
            return (
                f"⚠️ 确认修改以下群友身份？\n"
                f"  QQ: {data['qq_id']}\n"
                f"  原昵称: {data['old_nickname']}\n"
                f"  新昵称: {data['new_nickname']}\n\n"
                f"回复「确认」或「是」执行修改，回复「取消」放弃修改\n"
                f"({timeout}秒内有效)"
            )
        elif action == "add_nickname":
            return (
                f"⚠️ 确认为群友添加外号？\n"
                f"  QQ: {data['qq_id']}\n"
                f"  当前昵称: {data['current_nickname']}\n"
                f"  添加外号: {data['nickname']}\n\n"
                f"回复「确认」或「是」执行，回复「取消」放弃\n"
                f"({timeout}秒内有效)"
            )
        elif action == "remove_nickname":
            return (
                f"⚠️ 确认删除群友外号？\n"
                f"  QQ: {data['qq_id']}\n"
                f"  删除外号: {data['nickname']}\n\n"
                f"回复「确认」或「是」执行，回复「取消」放弃\n"
                f"({timeout}秒内有效)"
            )
        elif action == "restore_backup":
            return (
                f"⚠️ 确认从备份恢复数据？\n"
                f"  备份文件: {data['filename']}\n"
                f"  备份时间: {data['backup_time']}\n\n"
                f"⚠️ 当前数据将被覆盖！\n"
                f"回复「确认恢复」执行，回复「取消」放弃\n"
                f"({timeout}秒内有效)"
            )
        return "未知操作类型"

    async def _handle_confirmation(self, session_key: str, message: str) -> str:
        """处理用户的确认响应"""
        pending = self._pending_confirmations.get(session_key)
        if not pending:
            return None

        # 检查过期
        if asyncio.get_event_loop().time() > pending["expires"]:
            del self._pending_confirmations[session_key]
            return "⏰ 确认已超时，操作取消"

        message_lower = message.strip().lower()

        # 确认
        if message_lower in ("确认", "是", "yes", "y", "确认恢复"):
            del self._pending_confirmations[session_key]
            return await self._execute_confirmed_action(session_key, pending)

        # 取消
        if message_lower in ("取消", "否", "no", "n", "算了"):
            del self._pending_confirmations[session_key]
            return "❌ 操作已取消"

        return None  # 非确认相关的回复

    async def _execute_confirmed_action(self, session_key: str, pending: dict) -> str:
        """执行确认后的操作"""
        action = pending["action"]
        data = pending["data"]

        try:
            if action == "update_member":
                operator_id = session_key.split("_")[-1]
                group_id = data["group_id"]
                qq_id = data["qq_id"]
                old_nickname = data["old_nickname"]
                new_nickname = data["new_nickname"]

                await self._dm.update_member(group_id, qq_id, new_nickname)
                await self._dm.log_correction(
                    operator_id, group_id, qq_id,
                    old_nickname, new_nickname, "update_member"
                )
                return f"✅ 已更新: QQ {qq_id} 的昵称从「{old_nickname}」改为「{new_nickname}」"

            elif action == "add_nickname":
                operator_id = session_key.split("_")[-1]
                group_id = data["group_id"]
                qq_id = data["qq_id"]
                nickname = data["nickname"]

                await self._dm.add_nickname(group_id, qq_id, nickname)
                await self._dm.log_correction(
                    operator_id, group_id, qq_id,
                    "", nickname, "add_nickname"
                )
                return f"✅ 已为 QQ {qq_id} 添加外号: 「{nickname}」"

            elif action == "remove_nickname":
                operator_id = session_key.split("_")[-1]
                group_id = data["group_id"]
                qq_id = data["qq_id"]
                nickname = data["nickname"]

                await self._dm.remove_nickname(group_id, qq_id, nickname)
                await self._dm.log_correction(
                    operator_id, group_id, qq_id,
                    nickname, "", "remove_nickname"
                )
                return f"✅ 已删除 QQ {qq_id} 的外号: 「{nickname}」"

            elif action == "restore_backup":
                filename = data["filename"]
                success = await self._dm.restore_from_backup(filename)
                if success:
                    return f"✅ 已从备份 {filename} 恢复数据"
                else:
                    return f"❌ 备份恢复失败，请检查日志"

            return "✅ 操作完成"

        except Exception as e:
            logger.error(f"[群友识别] 执行确认操作失败: {e}", exc_info=True)
            return f"❌ 操作执行失败: {e}"

    # ==================== 修正命令处理 ====================

    async def _handle_correction_command(self, event, group_id: str, message: str) -> str:
        """处理修正命令"""
        # 移除命令前缀
        for prefix in ("修正 ", "correction ", "修改 "):
            if message.lower().startswith(prefix.lower()):
                content = message[len(prefix):].strip()
                break
        else:
            return "❌ 命令格式错误"

        # 尝试匹配: QQ号 新昵称
        match = re.match(r'(\d{5,15})\s+(.+)', content)
        if match:
            qq_id = match.group(1)
            new_nickname = match.group(2).strip()

            old_info = self._dm.get_member(group_id, qq_id)
            old_nickname = old_info.get("nickname", "（未记录）") if old_info else "（未记录）"

            return await self.request_confirmation(event, group_id, "update_member", {
                "group_id": group_id,
                "qq_id": qq_id,
                "old_nickname": old_nickname,
                "new_nickname": new_nickname
            })

        return (
            "❌ 命令格式不正确。\n"
            "用法: 修正 <QQ号> <新昵称>\n"
            "示例: 修正 123456789 新昵称"
        )

    async def _handle_add_nickname(self, event, group_id: str, message: str) -> str:
        """处理添加外号命令"""
        content = message[5:].strip()  # 移除"添加外号"

        match = re.match(r'(\d{5,15})\s+(.+)', content)
        if not match:
            return "❌ 格式错误。用法: 添加外号 <QQ号> <外号>"

        qq_id = match.group(1)
        nickname = match.group(2).strip()

        member_info = self._dm.get_member(group_id, qq_id)
        current_nickname = member_info.get("nickname", "未知") if member_info else "未知"

        return await self.request_confirmation(event, group_id, "add_nickname", {
            "group_id": group_id,
            "qq_id": qq_id,
            "nickname": nickname,
            "current_nickname": current_nickname
        })

    async def _handle_remove_nickname(self, event, group_id: str, message: str) -> str:
        """处理删除外号命令"""
        content = message[5:].strip()  # 移除"删除外号"

        match = re.match(r'(\d{5,15})\s+(.+)', content)
        if not match:
            return "❌ 格式错误。用法: 删除外号 <QQ号> <外号>"

        qq_id = match.group(1)
        nickname = match.group(2).strip()

        existing = self._dm.get_nicknames(group_id, qq_id)
        if nickname not in existing:
            return f"❌ QQ {qq_id} 不存在外号「{nickname}」"

        return await self.request_confirmation(event, group_id, "remove_nickname", {
            "group_id": group_id,
            "qq_id": qq_id,
            "nickname": nickname
        })

    async def _handle_search(self, event, group_id: str, message: str) -> str:
        """处理搜索命令"""
        for prefix in ("查找 ", "find ", "搜索 "):
            if message.lower().startswith(prefix.lower()):
                keyword = message[len(prefix):].strip()
                break
        else:
            return "❌ 格式错误"

        results = self._dm.search_members(group_id, keyword)
        if not results:
            return f"🔍 未找到与「{keyword}」匹配的群友"

        lines = [f"🔍 搜索「{keyword}」的结果 ({len(results)} 条):"]
        for qq_id, info in results[:10]:
            nickname = info.get("nickname", "未知")
            nicks = self._dm.get_nicknames(group_id, qq_id)
            nick_str = f" | 外号: {', '.join(nicks)}" if nicks else ""
            updated = datetime.fromtimestamp(info.get("updated_at", 0)).strftime("%m-%d %H:%M")
            lines.append(f"  QQ: {qq_id} | 昵称: {nickname}{nick_str} | 更新: {updated}")

        if len(results) > 10:
            lines.append(f"  ... 还有 {len(results) - 10} 条结果")

        return "\n".join(lines)

    async def _handle_view_history(self) -> str:
        """查看修正历史"""
        history = self._dm.get_correction_history(20)
        if not history:
            return "📋 暂无修正记录"

        lines = [f"📋 最近 {len(history)} 条修正记录:"]
        for entry in reversed(history):
            ts = datetime.fromtimestamp(entry["timestamp"]).strftime("%m-%d %H:%M:%S")
            action_map = {
                "update_member": "修改昵称",
                "add_nickname": "添加外号",
                "remove_nickname": "删除外号",
            }
            action_name = action_map.get(entry["action"], entry["action"])
            lines.append(
                f"  [{ts}] {action_name}: QQ={entry['qq_id']}, "
                f"旧={entry['old_nickname'] or '(空)'}, "
                f"新={entry['new_nickname'] or '(空)'}, "
                f"操作者={entry['operator']}"
            )

        return "\n".join(lines)

    async def _handle_restore(self, event, message: str) -> str:
        """处理恢复备份命令"""
        for prefix in ("恢复备份 ", "restore "):
            if message.lower().startswith(prefix.lower()):
                filename = message[len(prefix):].strip()
                break
        else:
            return "❌ 格式错误"

        backups = self._dm.list_backups()
        matched = [b for b in backups if b["filename"] == filename]
        if not matched:
            return f"❌ 未找到备份文件: {filename}"

        backup_info = matched[0]
        return await self.request_confirmation(event, "system", "restore_backup", {
            "filename": filename,
            "backup_time": backup_info["time"]
        })

    async def _handle_list_backups(self) -> str:
        """列出所有备份"""
        backups = self._dm.list_backups()
        if not backups:
            return "📁 暂无备份文件"

        lines = [f"📁 备份列表 ({len(backups)} 个):"]
        for b in backups[:15]:
            lines.append(f"  {b['filename']} | {b['time']} | {b['size_kb']} KB")

        if len(backups) > 15:
            lines.append(f"  ... 还有 {len(backups) - 15} 个备份")

        return "\n".join(lines)
