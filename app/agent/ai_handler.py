"""LangChain 驱动的龙虾斗兽场 AI 主持人。

设计：
- 每个用户独立一份对话历史（最多保留 N 轮，防止越塞越大）
- 每条用户消息都新建一个 ReAct agent（轻量，复用 LLM 实例）
- Tool 的 user_id 由后端绑死在 closure，AI 改不了
- 不写 fallback：API key 缺失或 LLM 调用失败，直接抛错让上层捕获

环境变量：
- OPENAI_API_KEY   必填
- OPENAI_BASE_URL  默认 https://api.deepseek.com/v1（DeepSeek 便宜、OpenAI 兼容）
- OPENAI_MODEL     默认 deepseek-chat
- LLM_TEMPERATURE  默认 0.8
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .prompts import SYSTEM_PROMPT
from .tools import build_tools

if TYPE_CHECKING:
    from ..api.main import AppState

logger = logging.getLogger(__name__)


# 历史最多保留这么多条消息（含 system），太长既费 token 又会乱
MAX_HISTORY = 30


# 微信不渲染 Markdown，把 LLM 常用的几种格式标记清掉，让回复在微信里更顺眼。
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)\*(?![*\w])")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)


def _strip_markdown_for_wechat(text: str) -> str:
    """把回复里的 Markdown 标记降级成微信能读的纯文本。

    保守处理，只清掉已知会变成乱码的几种：
    - **加粗** / *斜体* → 去掉星号
    - # 标题            → 去掉 #
    - | 表格行 |         → 整行删掉（微信里就是裸字符，不如不发）
    """
    if not text:
        return text
    cleaned = _MD_BOLD_RE.sub(r"\1", text)
    cleaned = _MD_ITALIC_RE.sub(r"\1", cleaned)
    cleaned = _MD_HEADING_RE.sub("", cleaned)
    cleaned = _MD_TABLE_LINE_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class AIHandler:
    """对每个用户做对话状态管理的入口。"""

    def __init__(self, state: "AppState"):
        self.state = state

        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY 未配置。请 export OPENAI_API_KEY=... "
                "（默认走 DeepSeek，base_url=https://api.deepseek.com/v1）"
            )

        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1").strip()
        model_name = os.environ.get("OPENAI_MODEL", "deepseek-chat").strip()
        temperature = float(os.environ.get("LLM_TEMPERATURE", "0.8"))

        self.llm = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            timeout=60,
            max_retries=2,
        )
        logger.info(
            "AIHandler: 已连接 LLM provider base=%s model=%s temp=%.2f",
            base_url, model_name, temperature,
        )

        # user_id -> 对话历史（按 LangChain BaseMessage 列表存）
        self._histories: Dict[str, List[BaseMessage]] = {}

    def _get_history(self, user_id: str) -> List[BaseMessage]:
        history = self._histories.get(user_id)
        if history is None:
            history = [SystemMessage(content=SYSTEM_PROMPT)]
            self._histories[user_id] = history
        return history

    def _trim_history(self, user_id: str) -> None:
        history = self._histories[user_id]
        if len(history) <= MAX_HISTORY:
            return
        self._histories[user_id] = [history[0]] + history[-(MAX_HISTORY - 1):]

    def reset(self, user_id: str) -> None:
        self._histories.pop(user_id, None)
        logger.info("ai_handler: 重置 %s 的对话历史", user_id[:8])

    async def handle(self, user_id: str, text: str) -> str:
        """处理一条玩家发来的文字消息，返回主持人回复。"""
        tools = build_tools(self.state, user_id)
        agent = create_react_agent(self.llm, tools)

        history = self._get_history(user_id)
        history.append(HumanMessage(content=text))

        logger.info(
            "ai_handler: invoke uid=%s history_len=%d msg=%r",
            user_id[:8], len(history), text[:50],
        )

        result: Dict[str, Any] = await agent.ainvoke({"messages": history})
        new_messages: List[BaseMessage] = result.get("messages", [])

        self._histories[user_id] = list(new_messages)
        self._trim_history(user_id)

        tool_calls = sum(1 for m in new_messages if isinstance(m, ToolMessage))
        logger.info(
            "ai_handler: done uid=%s tool_calls=%d reply_len=%d",
            user_id[:8], tool_calls,
            len(new_messages[-1].content) if new_messages else 0,
        )

        final = new_messages[-1]
        if not isinstance(final, AIMessage):
            raise RuntimeError(
                f"ai_handler: agent 最终输出不是 AIMessage（type={type(final).__name__}）",
            )

        reply = final.content
        if isinstance(reply, list):
            parts: List[str] = []
            for chunk in reply:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    parts.append(str(chunk.get("text") or ""))
                elif isinstance(chunk, str):
                    parts.append(chunk)
            reply = "\n".join(parts).strip()

        reply_text = str(reply).strip()
        return _strip_markdown_for_wechat(reply_text)
