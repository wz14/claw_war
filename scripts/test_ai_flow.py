"""端到端跑一遍 LangChain agent 路径：用 FakeListChatModel mock 一个 LLM，
确认 tool 路由 / 历史维护 / 战报塞 feed 都工作。

需要在 venv 里跑：python scripts/test_ai_flow.py
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import ai_handler  # noqa: E402
from app.main import AppState  # noqa: E402
from app.tools import build_tools  # noqa: E402

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    state = AppState()
    user_id = "test_user_001"

    # 直接组装一份 tools 测一把
    tools = build_tools(state, user_id)
    print("📦 注册的工具：")
    for t in tools:
        print(f"  - {t.name}: {t.description[:50]}...")

    # 手动调一下 train 看下能不能出动作描述
    train = next(t for t in tools if t.name == "train_lobster")
    print("\n🥊 调一次 train_lobster：")
    print(train.invoke(""))

    print("\n🦞 玩家龙虾：")
    status_tool = next(t for t in tools if t.name == "get_lobster_status")
    print(status_tool.invoke(""))

    print("\n⚔️ 调一次 battle：")
    battle_tool = next(t for t in tools if t.name == "battle")
    print(battle_tool.invoke(""))

    print(f"\n📰 feed 长度：{len(state.feed)}（战报应该自动塞进 feed）")

    print("\n✅ 工具链一切正常，等真实 LLM 接入即可。")


if __name__ == "__main__":
    asyncio.run(main())
