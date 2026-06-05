"""
群友识别插件 - 主入口
基于 AstrBot 平台，实现群友身份映射、外号智能提取、管理员交互修正
"""

import asyncio
import json
import os
import re
from datetime import datetime
from typing import Optional, Union

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import At, Plain

from .data_manager import DataManager
from .nickname_extractor import NicknameExtractor
from .admin_handler import AdminHandler


PLUGIN_NAME = "astrbot_plugin_group_member"


class GroupMemberPlugin(Star):
    """群友识别插件主类"""

    def __init__(self, context: Context, config: Optional[Union[dict, str]] = None):
        super().__init__(context)
        self._config = self._parse_config(config)

        # 尝试从 AstrBot 配置系统加载插件配置
        try:
            plugin_config = self.context.get_config()
            if plugin_config:
                parsed = self._parse_config(plugin_config)
                self._config = {**self._config, **parsed}
        except Exception as e:
            logger.debug(f"[群友识别] 从 context 加载配置跳过: {e}")

        # 插件目录
        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # 初始化各模块
        self._data_manager = DataManager(self._plugin_dir, self._config)
        self._nickname_extractor = NicknameExtractor(self._data_manager, self._config)
        self._admin_handler = AdminHandler(self._data_manager, self._config)

        # 后台任务引用
        self._backup_task: Optional[asyncio.Task] = None
        self._auto_extract_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._tasks_started = False

        logger.info(f"[群友识别] 插件初始化完成")

        # 在事件循环中启动后台任务
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._start_background_tasks())
        except RuntimeError:
            # 还没有运行中的事件循环，延迟启动
            pass

    # ==================== 生命周期 ====================

    @staticmethod
    def _parse_config(config) -> dict:
        """安全解析配置，兼容 dict 和 JSON 字符串"""
        if config is None:
            return {}
        if isinstance(config, dict):
            return config
        if isinstance(config, str):
            try:
                return json.loads(config)
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"[群友识别] 无法解析配置字符串，使用默认配置")
                return {}
        # 其他类型，尝试转换为 dict
        try:
            return dict(config) if config else {}
        except (TypeError, ValueError):
            return {}

    async def _ensure_tasks_started(self):
        """确保后台任务已启动"""
        if self._tasks_started:
            return
        self._tasks_started = True
        # 定期备份任务
        self._backup_task = asyncio.create_task(self._backup_loop())
        # 自动外号提取任务（如果启用）
        if self._config.get("auto_extract_enabled", False):
            self._auto_extract_task = asyncio.create_task(self._auto_extract_loop())
            logger.info("[群友识别] 自动外号提取已启用")

    async def _start_background_tasks(self):
        """启动后台任务"""
        await self._ensure_tasks_started()

    async def _backup_loop(self):
        """定期备份循环"""
        await asyncio.sleep(30)  # 启动后等30秒再开始首次备份
        while not self._shutdown_event.is_set():
            try:
                await self._data_manager.backup()
            except Exception as e:
                logger.error(f"[群友识别] 备份任务异常: {e}")
            try:
                interval = self._config.get("backup_interval", 3600)
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

    async def _auto_extract_loop(self):
        """自动外号提取循环"""
        await asyncio.sleep(60)  # 启动后等60秒再开始检查
        while not self._shutdown_event.is_set():
            try:
                # 检查所有群组
                for group_id in list(self._data_manager._members.keys()):
                    if self._data_manager.get_auto_extract_threshold_reached(group_id):
                        if self._data_manager.can_extract(group_id):
                            logger.info(f"[群友识别] 群 {group_id} 触发自动外号提取")
                            await self._nickname_extractor.extract_nicknames(group_id, self.context)
            except Exception as e:
                logger.error(f"[群友识别] 自动提取任务异常: {e}")
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=120)
                break
            except asyncio.TimeoutError:
                pass

    async def terminate(self):
        """插件卸载/停用时调用"""
        logger.info("[群友识别] 正在停止插件...")
        self._shutdown_event.set()

        # 取消后台任务
        for task in (self._backup_task, self._auto_extract_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # 保存所有数据
        await self._data_manager.save_all()
        await self._data_manager.backup()
        logger.info("[群友识别] 插件已停止，数据已保存")

    # ==================== 辅助方法 ====================

    def _get_group_id(self, event: AstrMessageEvent) -> Optional[str]:
        """安全获取群组ID"""
        try:
            msg_obj = event.message_obj
            if msg_obj and msg_obj.group_id:
                return str(msg_obj.group_id)
        except Exception:
            pass
        return None

    def _get_at_targets(self, event: AstrMessageEvent) -> list:
        """从消息中提取被@的QQ号列表"""
        targets = []
        try:
            msg_obj = event.message_obj
            if msg_obj and msg_obj.message:
                for comp in msg_obj.message:
                    if hasattr(comp, 'qq') and comp.qq:
                        targets.append(str(comp.qq))
        except Exception:
            pass
        return targets

    def _format_member_info(self, group_id: str, qq_id: str) -> str:
        """格式化单个群友信息"""
        member = self._data_manager.get_member(group_id, qq_id)
        if not member:
            return f"❌ 未找到 QQ {qq_id} 的记录"

        nickname = member.get("nickname", "未知")
        updated = datetime.fromtimestamp(member.get("updated_at", 0)).strftime("%Y-%m-%d %H:%M:%S")
        nicks = self._data_manager.get_nicknames(group_id, qq_id)

        lines = [
            f"📋 群友信息",
            f"  QQ: {qq_id}",
            f"  昵称: {nickname}",
            f"  更新时间: {updated}",
        ]
        if nicks:
            lines.append(f"  外号/别名: {', '.join(nicks)}")
        else:
            lines.append(f"  外号/别名: （暂无）")

        return "\n".join(lines)

    # ==================== 消息监听（自动更新） ====================

    # 监听所有群聊消息，实时更新群友身份映射
    @filter.regex(r"[\s\S]*")
    async def on_group_message(self, event: AstrMessageEvent):
        """监听所有群聊消息，自动记录群友发言并更新昵称映射"""
        group_id = self._get_group_id(event)
        if not group_id:
            return

        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        message_text = event.message_str or ""

        if not sender_id or not sender_name:
            return

        # 惰性启动后台任务
        await self._ensure_tasks_started()

        # 0. 优先检查管理员修正确认（仅处理确认回复，不重复处理命令）
        result = await self._admin_handler.try_handle_confirmation(event, group_id, message_text)
        if result is not None:
            yield event.plain_result(result)
            return

        # 1. 更新群友身份映射（检测昵称变化）
        await self._data_manager.update_member(group_id, sender_id, sender_name)

        # 2. 缓存聊天记录（用于外号提取）
        await self._data_manager.cache_message(group_id, sender_id, sender_name, message_text)

    # ==================== 命令：帮助 ====================

    @filter.command("qy_help")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示群友识别插件帮助"""
        help_text = (
            "📖 **群友识别插件 使用帮助**\n\n"
            "🔹 **基础查询**\n"
            "  /qy_info [@某人|QQ号]  - 查看群友信息和外号\n"
            "  /qy_list [页码]          - 列出本群群友列表\n"
            "  /qy_search <关键词>      - 搜索群友（QQ号/昵称）\n\n"
            "🔹 **外号提取**（管理员）\n"
            "  /qy_extract              - 手动触发LLM外号提取\n"
            "  /qy_extract <QQ号>       - 提取指定群友的外号\n\n"
            "🔹 **管理员修正**\n"
            "  /qy_admin <指令>         - 进入管理员修正模式\n"
            "  修正 <QQ号> <新昵称>     - 修改群友昵称映射\n"
            "  添加外号 <QQ号> <外号>   - 手动添加外号\n"
            "  删除外号 <QQ号> <外号>   - 删除外号\n"
            "  查找 <关键词>            - 搜索群友\n"
            "  修正记录                 - 查看修正历史\n\n"
            "🔹 **系统管理**（管理员）\n"
            "  /qy_stat                 - 查看插件统计\n"
            "  /qy_backup               - 手动备份数据\n"
            "  /qy_admin 备份列表       - 查看备份列表\n"
            "  /qy_admin 恢复备份 <文件名> - 恢复备份\n"
        )
        yield event.plain_result(help_text)

    # ==================== 命令：群友信息 ====================

    @filter.command("qy_info")
    async def cmd_info(self, event: AstrMessageEvent):
        """查看群友信息和外号"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用")
            return

        message_text = event.message_str or ""
        # 提取命令参数（去除指令名本身）
        args = re.sub(r'^/?qy_info\s*', '', message_text.strip(), flags=re.IGNORECASE).strip().split()
        args = [a for a in args if a]  # 过滤空字符串

        # 检查被@的人
        at_targets = self._get_at_targets(event)
        target_qq = None

        if at_targets:
            target_qq = at_targets[0]
        elif args:
            # 尝试从参数中提取QQ号
            for arg in args:
                if re.match(r'^\d{5,15}$', arg):
                    target_qq = arg
                    break

        if target_qq:
            result = self._format_member_info(group_id, target_qq)
        else:
            # 显示自己的信息
            sender_id = event.get_sender_id()
            result = self._format_member_info(group_id, sender_id)

        yield event.plain_result(result)

    # ==================== 命令：群友列表 ====================

    @filter.command("qy_list")
    async def cmd_list(self, event: AstrMessageEvent):
        """列出本群群友"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用")
            return

        members = self._data_manager.get_group_members(group_id)
        if not members:
            yield event.plain_result("📋 本群暂无群友记录")
            return

        # 分页
        message_text = event.message_str or ""
        args = message_text.strip().split()
        page = 1
        for arg in args:
            if arg.isdigit():
                page = int(arg)
                break

        page_size = 15
        member_list = sorted(members.items(), key=lambda x: x[0])
        total_pages = max(1, (len(member_list) + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        page_items = member_list[start:start + page_size]

        lines = [f"📋 本群群友列表 (第 {page}/{total_pages} 页, 共 {len(members)} 人)"]
        for qq_id, info in page_items:
            nickname = info.get("nickname", "未知")
            nicks = self._data_manager.get_nicknames(group_id, qq_id)
            nick_str = f" [{', '.join(nicks)}]" if nicks else ""
            lines.append(f"  {qq_id} | {nickname}{nick_str}")

        if total_pages > 1:
            lines.append(f"--- 输入 /qy_list <页码> 翻页 ---")

        yield event.plain_result("\n".join(lines))

    # ==================== 命令：搜索 ====================

    @filter.command("qy_search")
    async def cmd_search(self, event: AstrMessageEvent):
        """搜索群友"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用")
            return

        message_text = event.message_str or ""
        # 去除指令名本身
        keyword = re.sub(r'^/?qy_search\s*', '', message_text.strip(), flags=re.IGNORECASE).strip()

        if not keyword:
            yield event.plain_result("❌ 用法: /qy_search <关键词>")
            return

        results = self._data_manager.search_members(group_id, keyword)
        if not results:
            yield event.plain_result(f"🔍 未找到与「{keyword}」匹配的群友")
            return

        lines = [f"🔍 搜索「{keyword}」的结果 ({len(results)} 条):"]
        for qq_id, info in results[:10]:
            nickname = info.get("nickname", "未知")
            nicks = self._data_manager.get_nicknames(group_id, qq_id)
            nick_str = f" | 外号: {', '.join(nicks)}" if nicks else ""
            lines.append(f"  QQ: {qq_id} | 昵称: {nickname}{nick_str}")

        if len(results) > 10:
            lines.append(f"  ... 还有 {len(results) - 10} 条，请缩小搜索范围")

        yield event.plain_result("\n".join(lines))

    # ==================== 命令：外号提取（管理员） ====================

    @filter.command("qy_extract")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_extract(self, event: AstrMessageEvent):
        """手动触发LLM外号提取（仅管理员）"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用")
            return

        message_text = event.message_str or ""
        # 去除指令名本身，提取参数
        param_str = re.sub(r'^/?qy_extract\s*', '', message_text.strip(), flags=re.IGNORECASE).strip()
        args = param_str.split() if param_str else []

        # 检查是否有指定QQ号
        target_qq = None
        if args:
            for arg in args:
                if re.match(r'^\d{5,15}$', arg):
                    target_qq = arg
                    break

        if target_qq:
            # 提取指定群友的外号
            yield event.plain_result(f"🔍 正在分析 QQ {target_qq} 的外号，请稍候...")
            try:
                result = await self._nickname_extractor.extract_single_member(
                    group_id, target_qq, self.context
                )
                if result:
                    yield event.plain_result(
                        f"✅ QQ {target_qq} 的外号提取完成:\n  {', '.join(result)}"
                    )
                else:
                    yield event.plain_result(f"❌ 未能提取到 QQ {target_qq} 的新外号")
            except Exception as e:
                logger.error(f"[群友识别] 单成员外号提取失败: {e}", exc_info=True)
                yield event.plain_result(f"❌ 外号提取失败: {e}")
            return

        # 检查提取间隔
        if not self._data_manager.can_extract(group_id):
            yield event.plain_result(
                "⏳ 外号提取间隔未到，请稍后再试\n"
                f"  当前间隔设置: {self._config.get('nickname_extraction_interval', 3600)} 秒"
            )
            return

        # 检查聊天记录量
        messages = self._data_manager.get_recent_messages(group_id)
        if len(messages) < 20:
            yield event.plain_result(
                f"📝 聊天记录不足 (当前 {len(messages)} 条，需要至少 20 条)，无法分析"
            )
            return

        yield event.plain_result(f"🤖 正在调用LLM分析群聊记录提取外号 (共 {len(messages)} 条消息)...")

        try:
            result = await self._nickname_extractor.extract_nicknames(group_id, self.context)

            if result:
                summary_lines = ["✅ 外号提取完成! 更新情况:"]
                for qq_id, nicks in result.items():
                    member = self._data_manager.get_member(group_id, qq_id)
                    name = member.get("nickname", "未知") if member else "未知"
                    summary_lines.append(f"  {name}({qq_id}): {', '.join(nicks)}")
                yield event.plain_result("\n".join(summary_lines))
            else:
                yield event.plain_result("ℹ️ 外号提取完成，未发现新的外号")
        except Exception as e:
            logger.error(f"[群友识别] 外号提取命令失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 外号提取失败: {e}")

    # ==================== 命令：管理员修正 ====================

    @filter.command("qy_admin")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_admin(self, event: AstrMessageEvent):
        """管理员修正入口（仅管理员）"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用")
            return

        message_text = event.message_str or ""
        # 移除指令前缀（AstrBot 可能已去掉 /，兼容两种格式）
        content = message_text.strip()
        content = re.sub(r'^/?qy_admin\s*', '', content, flags=re.IGNORECASE).strip()

        if not content:
            # 显示管理员帮助
            help_text = (
                "🔧 **管理员修正系统**\n\n"
                "支持以下自然语言指令:\n"
                "  修正 <QQ号> <新昵称>   - 修改身份映射\n"
                "  添加外号 <QQ号> <外号>  - 添加外号\n"
                "  删除外号 <QQ号> <外号>  - 删除外号\n"
                "  查找 <关键词>          - 搜索群友\n"
                "  修正记录 / 修正历史     - 查看修正历史\n"
                "  备份列表               - 查看备份文件\n"
                "  恢复备份 <文件名>       - 从备份恢复\n\n"
                "示例: /qy_admin 修正 123456 新昵称\n"
                "      /qy_admin 添加外号 123456 老王"
            )
            yield event.plain_result(help_text)
            return

        # 委托给 AdminHandler 处理
        result = await self._admin_handler.process_admin_command(event, group_id, content)
        if result:
            yield event.plain_result(result)
        else:
            yield event.plain_result(
                "❌ 无法识别的管理员指令。输入 /qy_admin 查看帮助\n"
                f"  你输入的是: {content}"
            )

    # ==================== 命令：统计 ====================

    @filter.command("qy_stat")
    async def cmd_stat(self, event: AstrMessageEvent):
        """查看插件统计信息"""
        group_id = self._get_group_id(event)

        stats = self._data_manager.get_statistics()

        lines = [
            "📊 **群友识别插件 统计**",
            f"  覆盖群组: {stats['group_count']} 个",
            f"  记录群友: {stats['total_members']} 人",
            f"  提取外号: {stats['total_nicknames']} 个",
            f"  缓存消息: {stats['cached_messages']} 条",
            f"  修正记录: {stats['corrections_count']} 条",
            f"  备份数量: {stats['backups_count']} 个",
            f"  上次备份: {stats['last_backup']}",
        ]

        if group_id:
            member_count = self._data_manager.get_member_count(group_id)
            lines.insert(2, f"  本群群友: {member_count} 人")

            # 显示本群外号最多的前5人
            all_nicks = self._data_manager.get_all_nicknames(group_id)
            if all_nicks:
                ranked = sorted(all_nicks.items(),
                                key=lambda x: len(x[1].get("nicknames", [])),
                                reverse=True)
                top = [(qq, info.get("nicknames", [])) for qq, info in ranked[:5]
                       if info.get("nicknames")]
                if top:
                    lines.append(f"\n  🏆 本群外号 TOP5:")
                    for qq, nicks in top:
                        member = self._data_manager.get_member(group_id, qq)
                        name = member.get("nickname", "未知") if member else "未知"
                        lines.append(f"    {name}({qq}): {', '.join(nicks)}")

        yield event.plain_result("\n".join(lines))

    # ==================== 命令：手动备份 ====================

    @filter.command("qy_backup")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_backup(self, event: AstrMessageEvent):
        """手动备份数据（仅管理员）"""
        yield event.plain_result("📦 正在备份数据...")

        backup_path = await self._data_manager.backup()
        if backup_path:
            stats = self._data_manager.get_statistics()
            yield event.plain_result(
                f"✅ 数据备份完成!\n"
                f"  文件: {os.path.basename(backup_path)}\n"
                f"  备份数量: {stats['backups_count']} 个"
            )
        else:
            yield event.plain_result("❌ 备份失败，请查看日志")
