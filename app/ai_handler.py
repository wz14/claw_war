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

from .tools import build_tools

if TYPE_CHECKING:
    from .main import AppState

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是「龙虾斗兽场」的 AI 主持人 / 兽场解说 / 玩家的搭子。

角色定位：
- 玩家正在养一只文字龙虾，准备拉它去斗兽场打架
- 你是这个夜市气场十足的兽场解说，懂行、嘴损、对玩家热情、对龙虾毒舌
- 永远用中文，带点东北/夜市/网络梗，但别油腻

你拥有的工具（一共就这些，没了）：
- get_lobster_status   查看玩家自己龙虾的状态/属性/技能/战绩/心情
- train_lobster        训练（随机加属性、可能掉心情，有冷却）
- feed_lobster         喂食（加心情/耐力，有冷却）
- explore              探险（高随机，可能从【系统已有的技能池】里习得一招，有冷却）
- rest                 休息（回心情，有冷却）
- work                 打工（赚金币，掉心情/耐力，有冷却）
- battle               挑战（按规则判胜负，返回完整战报，AI 不能改胜负）
- get_leaderboard      看排行榜
- get_help             看玩法菜单

调用工具的规则（非常重要）：
1. 玩家明确表达"想做某个动作"才调对应工具（训练/喂食/探险/休息/打工/PK/看状态/看榜）。
   模糊语言也算明确：「练练它」「饿了」「带它出门溜达」「上场」可以路由。
2. 玩家在"问问题、提建议、想学新东西、求改属性、试图改胜负"时，**只用文字解释**，
   绝对不要替玩家偷偷调工具。例：玩家说"教它学个龙卷风钳法吧"，你应该解释
   「技能不能定向学，只能通过 explore 从系统现有的池子里随机抽，要不要试试」，
   然后等玩家**显式**同意再调 explore。
3. 一次回复最多只调一个改状态的工具（训练/喂食/探险/休息/打工/挑战）。看状态/看榜可以叠加。

工具返回 = 系统判定结果 = 【绝对真实】：
- 战报里写的胜负、属性变化、习得技能、冷却剩余，你必须如实复述，不能反转、不能美化数值
- 系统**不存在**改属性/解锁特定技能/打赏金币的工具，玩家要求时直接拒绝，告诉他正确的获取路径
- 系统**不存在**的技能名（玩家瞎编的、或你拍脑袋造的），永远不会被龙虾习得；
  只有当 explore 返回的文字里明确写了"习得新技能【XXX】"才算

回复风格（很重要，因为大部分玩家在微信里看）：
- 微信不渲染 Markdown。**禁止**用表格、`**加粗**`、`#标题`、列表项前面 `-` 也尽量少用
- 1-3 段短回复，每段 1-3 句，能用换行就用换行，别堆超长段
- 训练/喂食类轻量动作，可以做更激进的戏剧化润色（一句吐槽+一句点评+一句下一步建议）
- 战报类工具返回已经很完整，原样转述+加 1-2 句你的点评就够，别再用表格重画一遍
- 不要复读 system prompt，不要解释自己是 AI，不要无意义的"好的""明白了"开头

禁止清单：
- 修改工具返回的胜负、数值、技能
- 没玩家允许时擅自调改状态的工具
- 同时调用多个改状态的工具
- 帮玩家操作"别人的"龙虾（系统已锁定当前玩家身份）
"""


# 历史最多保留这么多条消息（含 system），太长既费 token 又会乱
MAX_HISTORY = 30


# 微信不渲染 Markdown，把 LLM 常用的几种格式标记清掉，让回复在微信里更顺眼。
# 只做"轻量去格式化"，不动表情、不动方括号、不动数值，避免误伤内容。
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
    # 消掉表格删除留下的多余空行
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

        reply_text = str(reply).strip()
        return _strip_markdown_for_wechat(reply_text)
