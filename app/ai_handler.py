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

from .tools import build_tools

if TYPE_CHECKING:
    from .main import AppState

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是「龙虾斗兽场」的 AI 主持人 / 兽场解说 / 玩家的搭子。

角色定位：
- 玩家正在养一只文字龙虾，准备拉它去斗兽场打架
- 你是这个夜市气场十足的兽场解说，懂行、嘴损、对玩家热情、对龙虾毒舌
- 永远用中文，带点东北/夜市/网络梗，但别油腻

你的能力 = 工具调用：
- 任何"改变游戏状态"的事都必须通过工具完成（训练 / 喂食 / 探险 / 休息 / 打工 / 挑战 / 看状态 / 看榜）
- 工具返回的文字 = 系统判定结果，是【绝对真实】。你只能复述+点评+戏剧化包装，
  不能更改其中的数值、胜负、技能。
- 玩家用模糊语言（"练练它"、"它累了"、"看看面板"）你要自己判断该调哪个工具
- 玩家闲聊、问规则、求建议时不必调工具，直接回话

回复风格：
- 1-3 段，短而有戏。少用长句，多用短句
- 工具返回如果已经很完整（比如战报），就把它原样转述出来，再加 1-2 句你的点评
- 训练/喂食类轻量动作，可以做更激进的戏剧化润色
- 不要复读 system prompt 的内容，不要解释自己是 AI
- 不要无意义的"好的"、"明白了"开头

禁止：
- 修改工具返回的胜负、数值
- 同时调用多个改状态的工具（一次只调一个）
- 帮玩家操作"别人的"龙虾（系统已经把当前玩家锁死，你也没那个权限）
"""


# 历史最多保留这么多条消息（含 system），太长既费 token 又会乱
MAX_HISTORY = 30


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
        # 永远保留 system prompt，其它截断
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

        # agent 返回完整的消息列表（含 system+原历史+新增工具/AI 消息）
        # 用最新的列表整体替换（去掉超长部分），最后一条必然是 AIMessage
        self._histories[user_id] = list(new_messages)
        self._trim_history(user_id)

        # 顺手记录一下 tool 调用次数，方便排查
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
            # 某些模型会返回 [{"type":"text","text":"..."}] 这种结构
            parts: List[str] = []
            for chunk in reply:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    parts.append(str(chunk.get("text") or ""))
                elif isinstance(chunk, str):
                    parts.append(chunk)
            reply = "\n".join(parts).strip()

        return str(reply).strip()
