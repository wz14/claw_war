"""AI 战斗点评测试：用两只配置好的龙虾跑一场战斗，再让 AI 点评。

目的：
- 看 AI 是否能正确识别流派协同、关键回合、装备效果
- 看 AI 是否会编造数值 / 复述每一行（噪声）
- 看 AI 的语气是否符合兽场解说的人设

测试场景：
1. 力量流 vs 速度流：经典 RPS 对决
2. 肉盾流 vs 力量流：被力量爆破的肉盾
3. 高级 vs 低级：以下犯上场景

用法：
    # 需要先 export OPENAI_API_KEY 或 .env 配好
    python scripts/ai_review_test.py [--scenario 1|2|3|all]

不写 fallback：缺 key、LLM 报错都直接抛出。
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import os
import sys
from typing import Tuple

print = functools.partial(print, flush=True)  # noqa: A001

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from app.agent.prompts import SYSTEM_PROMPT  # noqa: E402
from app.core import battle as bt  # noqa: E402
from app.core.lobster import Lobster  # noqa: E402
from scripts.battle_simulate import power_build, speed_build, tank_build, _reset_for_battle  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("app").setLevel(logging.WARNING)


# ============ 场景 ============


def scenario_power_vs_speed() -> Tuple[Lobster, Lobster, str]:
    a = power_build("蒜蓉暴君", level=6)
    b = speed_build("夜场闪电", level=6)
    return a, b, "经典 RPS：力量流 vs 速度流"


def scenario_power_vs_tank() -> Tuple[Lobster, Lobster, str]:
    a = power_build("钳皇遗孤", level=7)
    b = tank_build("不锈钢盆", level=7)
    return a, b, "力量爆破肉盾：看 AI 是否能识别破甲剧情"


def scenario_upset() -> Tuple[Lobster, Lobster, str]:
    a = speed_build("外卖逃逸", level=4)
    b = power_build("夜市钳神", level=8)
    return a, b, "以下犯上：低级速度流 vs 高级力量流"


SCENARIOS = {
    "1": scenario_power_vs_speed,
    "2": scenario_power_vs_tank,
    "3": scenario_upset,
}


# ============ AI 点评核心 ============


async def review_battle(narration: str, label: str) -> None:
    """把战报喂给 LLM，让它以兽场解说人设点评。"""
    llm = ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL", "deepseek-chat"),
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
        temperature=float(os.environ.get("LLM_TEMPERATURE", "0.8")),
        timeout=60,
    )

    user_prompt = (
        f"以下是刚才系统给出的【战报原文】（这是系统判定的真实结果，胜负数值都不能改）：\n\n"
        f"{narration}\n\n"
        f"---\n"
        f"请你以兽场解说的口吻点评这一场。规则：\n"
        f"- 直接原样转述战报开头部分（双方属性卡），不重画\n"
        f"- 然后加 2-3 句你的解说，聚焦在「流派协同 / 关键回合转折 / 装备贡献 / "
        f"  谁该买什么装备」等懂行的点评\n"
        f"- 不要复述每一行回合，回合战报本身已经给玩家了，复述只会刷屏\n"
        f"- 不能编造战报里没有的暴击/技能/装备\n"
        f"- 微信看，禁止用 Markdown 加粗/表格"
    )

    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_prompt)]
    response = await llm.ainvoke(messages)

    print(f"\n{'='*72}")
    print(f"🎙️  AI 点评（{label}）")
    print(f"{'='*72}")
    print(response.content)
    print()


async def run_scenario(key: str) -> None:
    factory = SCENARIOS.get(key)
    if factory is None:
        raise ValueError(f"未知场景 {key}，可选：{list(SCENARIOS.keys())}")

    a, b, label = factory()
    _reset_for_battle(a)
    _reset_for_battle(b)
    result = bt.simulate(a, b)

    print(f"\n{'#'*72}")
    print(f"# 场景：{label}")
    print(f"# {a.name}(Lv.{a.level}) vs {b.name}(Lv.{b.level})")
    print(f"{'#'*72}")
    print("\n📜 系统战报原文：\n")
    print(result.narration)

    await review_battle(result.narration, label)


async def main() -> None:
    parser = argparse.ArgumentParser(description="AI 战斗点评测试")
    parser.add_argument(
        "--scenario", "-s", default="all", choices=["1", "2", "3", "all"],
        help="跑哪个场景（默认 all）",
    )
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OPENAI_API_KEY 未配置，请先 export 或写到 .env")

    keys = ["1", "2", "3"] if args.scenario == "all" else [args.scenario]
    for k in keys:
        await run_scenario(k)


if __name__ == "__main__":
    asyncio.run(main())
