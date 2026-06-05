"""
群友识别插件 - 外号智能提取模块
利用大语言模型分析群聊记录，智能提取群友的常用外号/别名
"""

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Optional

from astrbot.api import logger

from .data_manager import DataManager


class NicknameExtractor:
    """外号提取器：调用LLM分析聊天记录，提取群友外号"""

    # 外号提取的提示词模板
    EXTRACTION_PROMPT = """你是一个群聊分析助手。请分析以下群聊记录，提取群友之间的常用外号、别名和特征称呼。

## 规则
1. 识别每个发言者的QQ号和昵称，分析其他人是如何称呼TA的（@提及、直接叫名字、绰号等）
2. 外号必须是群聊中实际使用的称呼，不要凭空编造
3. 排除不相关或过于通用的称呼（如"大家""群主""管理员"等）
4. 不同发言者可能有不同的称呼偏好，都要收录
5. 注意区分不同QQ号对应的人物

## 已记录的群友身份（供参考）
{member_list}

## 已有的外号记录（供参考，请基于新聊天记录更新）
{existing_nicknames}

## 最近的群聊记录
{chat_history}

## 输出格式
请以JSON格式输出，只输出JSON，不要包含其他内容：
```json
{{
  "nicknames": [
    {{
      "qq": "QQ号",
      "current_nickname": "当前昵称",
      "nicknames": ["外号1", "外号2", ...],
      "reason": "提取理由（简短说明为什么认为这是他的外号）"
    }}
  ]
}}
```

如果聊天记录中没有发现新的外号，请返回空的 nicknames 列表。
仅对本次聊天记录中出现的人物进行分析。"""

    CONSOLIDATION_PROMPT = """你是一个群聊分析助手。请将以下多轮分析结果合并，去重并优化外号列表。

## 规则
1. 合并同一QQ号的多个外号，去重
2. 如果同一外号有多种变体（如"老王""王哥""王总"），合并为最具代表性的版本
3. 移除明显不合理或冒犯性的外号
4. 保留每个外号的来源说明

## 已有外号数据
{existing_nicknames}

## 本轮新提取的外号
{new_nicknames}

## 输出格式
请以JSON格式输出合并后的完整外号数据，只输出JSON：
```json
{{
  "merged_nicknames": [
    {{
      "qq": "QQ号",
      "nicknames": ["外号1", "外号2", ...]
    }}
  ]
}}
```"""

    def __init__(self, data_manager: DataManager, config: dict):
        self._dm = data_manager
        self._config = config
        self._extraction_lock = asyncio.Lock()  # 防止并发提取

    async def extract_nicknames(self, group_id: str, context) -> Optional[dict]:
        """
        执行外号提取
        返回: {qq_id: [nicknames]} 或 None（提取失败时）
        """
        async with self._extraction_lock:
            try:
                # 再次检查是否可以提取（双重锁检查）
                if not self._dm.can_extract(group_id):
                    remaining = self._config.get("nickname_extraction_interval", 3600) - (
                        time.time() - self._dm._last_extraction_time.get(str(group_id), 0)
                    )
                    logger.info(f"[群友识别] 提取间隔未到，剩余 {int(remaining)} 秒")
                    return None

                # 获取聊天记录
                messages = self._dm.get_recent_messages(group_id)
                if len(messages) < 20:
                    logger.info(f"[群友识别] 聊天记录不足 (当前 {len(messages)} 条)，跳过提取")
                    return None

                # 构建成员列表文本
                members = self._dm.get_group_members(group_id)
                member_list_text = self._format_member_list(members)

                # 构建已有外号文本
                existing = self._dm.get_all_nicknames(group_id)
                existing_text = self._format_existing_nicknames(existing)

                # 构建聊天记录文本
                chat_text = self._format_chat_history(messages)

                # 构建提示词
                prompt = self.EXTRACTION_PROMPT.format(
                    member_list=member_list_text,
                    existing_nicknames=existing_text,
                    chat_history=chat_text
                )

                logger.info(f"[群友识别] 开始LLM外号提取: 群={group_id}, 消息数={len(messages)}")

                # 调用LLM
                llm_result = await self._call_llm(context, prompt)

                if not llm_result:
                    logger.warning(f"[群友识别] LLM 返回空结果")
                    return None

                # 解析LLM结果
                extracted = self._parse_extraction_result(llm_result)
                if not extracted:
                    return None

                # 合并到已有外号数据库
                result = {}
                changed_count = 0
                for item in extracted:
                    qq_id = str(item.get("qq", ""))
                    new_nicks = item.get("nicknames", [])
                    if qq_id and new_nicks:
                        changed = await self._dm.update_nicknames(group_id, qq_id, new_nicks)
                        if changed:
                            changed_count += 1
                        result[qq_id] = self._dm.get_nicknames(group_id, qq_id)

                # 重置消息计数器
                self._dm.reset_message_counter(group_id)

                logger.info(f"[群友识别] 外号提取完成: 群={group_id}, "
                            f"涉及 {len(extracted)} 人, {changed_count} 人有更新")
                return result

            except Exception as e:
                logger.error(f"[群友识别] 外号提取异常: {e}", exc_info=True)
                return None

    async def _call_llm(self, context, prompt: str) -> Optional[str]:
        """调用LLM接口"""
        provider_id = self._config.get("llm_provider_id_override", "") or None
        if not provider_id:
            logger.error(
                "[群友识别] 未配置 LLM 提供商ID。请在插件配置中设置「LLM提供商ID覆写」字段。\n"
                "   可在 AstrBot WebUI → 插件管理 → 群友识别 → 配置 中查看已配置的 LLM 提供商。"
            )
            return None

        try:
            llm_resp = await context.llm_generate(prompt=prompt, chat_provider_id=provider_id)
            if llm_resp and hasattr(llm_resp, "completion_text"):
                return llm_resp.completion_text
            elif isinstance(llm_resp, str):
                return llm_resp
            else:
                logger.warning(f"[群友识别] LLM返回格式未知: {type(llm_resp)}")
                return str(llm_resp) if llm_resp else None
        except Exception as e:
            logger.error(f"[群友识别] LLM调用失败: {e}")
            return None

    def _parse_extraction_result(self, llm_text: str) -> list:
        """解析LLM返回的外号提取结果"""
        try:
            # 尝试提取JSON块
            json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', llm_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1).strip()
            else:
                # 尝试直接解析整个文本
                json_str = llm_text.strip()

            data = json.loads(json_str)
            nicknames = data.get("nicknames", [])

            if not isinstance(nicknames, list):
                logger.warning(f"[群友识别] LLM返回格式异常: nicknames 不是列表")
                return []

            # 验证每一项
            validated = []
            for item in nicknames:
                if not isinstance(item, dict):
                    continue
                qq = item.get("qq")
                nicks = item.get("nicknames")
                if qq and isinstance(nicks, list) and len(nicks) > 0:
                    validated.append({
                        "qq": str(qq),
                        "nicknames": [str(n).strip() for n in nicks if str(n).strip()]
                    })

            return validated

        except json.JSONDecodeError as e:
            logger.warning(f"[群友识别] LLM结果JSON解析失败: {e}\n原始文本: {llm_text[:500]}")
            return []
        except Exception as e:
            logger.error(f"[群友识别] 解析提取结果异常: {e}")
            return []

    def _format_member_list(self, members: dict) -> str:
        """格式化群友列表"""
        lines = []
        for qq_id, info in members.items():
            nickname = info.get("nickname", "未知")
            lines.append(f"  QQ: {qq_id}  |  昵称: {nickname}")
        return "\n".join(lines) if lines else "（暂无记录）"

    def _format_existing_nicknames(self, nicknames: dict) -> str:
        """格式化已有外号"""
        if not nicknames:
            return "（暂无已有外号记录）"
        lines = []
        for qq_id, info in nicknames.items():
            nicks = info.get("nicknames", [])
            if nicks:
                lines.append(f"  QQ {qq_id}: {', '.join(nicks)}")
        return "\n".join(lines) if lines else "（暂无已有外号记录）"

    def _format_chat_history(self, messages: list) -> str:
        """格式化聊天记录"""
        lines = []
        for i, msg in enumerate(messages[-200:], 1):  # 最多200条
            nickname = msg.get("nickname", "未知")
            qq = msg.get("qq", "未知")
            content = msg.get("message", "")
            msg_time = datetime.fromtimestamp(msg.get("time", 0)).strftime("%H:%M:%S")
            lines.append(f"[{msg_time}] {nickname}({qq}): {content}")
        return "\n".join(lines)

    async def extract_single_member(
        self, group_id: str, qq_id: str, context
    ) -> Optional[list]:
        """针对单个群友提取外号"""
        messages = self._dm.get_recent_messages(group_id)

        # 筛选包含该群友的消息（被@或被提及）
        member_info = self._dm.get_member(group_id, qq_id)
        if not member_info:
            return None

        nickname = member_info.get("nickname", "")
        related_msgs = []
        for msg in messages:
            content = msg.get("message", "")
            if msg.get("qq") == str(qq_id) or nickname in content or qq_id in content:
                related_msgs.append(msg)

        if len(related_msgs) < 10:
            logger.info(f"[群友识别] 群友 {qq_id} 相关消息不足，跳过提取")
            return None

        prompt = self.EXTRACTION_PROMPT.format(
            member_list=self._format_member_list({qq_id: member_info}),
            existing_nicknames=self._format_existing_nicknames(
                {qq_id: self._dm._nicknames.get(str(group_id), {}).get(str(qq_id), {})}
            ),
            chat_history=self._format_chat_history(related_msgs)
        )

        llm_result = await self._call_llm(context, prompt)
        if not llm_result:
            return None

        extracted = self._parse_extraction_result(llm_result)
        for item in extracted:
            if str(item.get("qq")) == str(qq_id):
                await self._dm.update_nicknames(group_id, qq_id, item["nicknames"])
                return item["nicknames"]

        return []
