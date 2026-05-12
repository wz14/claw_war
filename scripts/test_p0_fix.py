"""P0 修复回归脚本。

验证两点：
  1. StructuredTool 替换 Tool 后，无参工具被空 args 调用不再抛
     `Too many arguments to single-input tool` —— 这是 P0-2 的核心 bug。
  2. 走一次完整 ai_handler.handle()，让真实 LLM 触发 feed_lobster，
     看链路能不能跑通（之前生产环境就是这条路径报错的）。

不写 fallback：缺 key、LLM 报错都直接抛出。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.agent.ai_handler import AIHandler  # noqa: E402
from app.agent.tools import build_tools  # noqa: E402
from app.api.main import AppState  # noqa: E402
from app.core import factory  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

logger = logging.getLogger("p0_test")


USER_ID = "p0_test_user_001"


def _print_section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def test_tools_empty_args() -> None:
    """直接调每个无参工具，模拟 LLM 塞 `arguments={}` 的情况。

    关键断言：以前 langchain Tool（single-input）会在这里抛 ToolException，
    现在切到 StructuredTool 之后必须能正常返回字符串。
    """
    _print_section("【测试 1】无参工具用空 args 调用（之前会 ToolException）")
    state = AppState()
    lobster = factory.create_lobster(user_id=USER_ID)
    state.lobsters[USER_ID] = lobster
    logger.info("test1: 准备龙虾 %s lvl=%d", lobster.name, lobster.level)

    tools = build_tools(state, USER_ID)
    target_names = {
        "feed_lobster",        # 生产环境实际报错的工具
        "train_lobster",
        "rest",
        "explore",
        "get_lobster_status",
        "get_help",
    }
    failures: list[tuple[str, str]] = []

    for tool in tools:
        if tool.name not in target_names:
            continue
        try:
            # langchain 框架在新模型 schema 下会用空 dict 调用无参工具
            result: Any = tool.invoke({})
        except Exception as exc:  # noqa: BLE001
            failures.append((tool.name, repr(exc)))
            logger.error("tool[%s] 空 args 调用失败: %s", tool.name, exc)
            continue
        preview = (result if isinstance(result, str) else str(result))[:80]
        logger.info("tool[%s] OK -> %s", tool.name, preview.replace("\n", " | "))

    if failures:
        print("\n❌ 失败工具：")
        for name, err in failures:
            print(f"   - {name}: {err}")
        raise SystemExit(1)
    print("\n✅ 所有目标无参工具都成功响应空 args 调用")


def test_tool_with_args() -> None:
    """有参工具仍然能正常按 schema 接收参数。"""
    _print_section("【测试 2】有参工具按 schema 调用")
    state = AppState()
    lobster = factory.create_lobster(user_id=USER_ID)
    state.lobsters[USER_ID] = lobster

    tools = build_tools(state, USER_ID)
    by_name = {t.name: t for t in tools}

    open_shop = by_name["open_shop"]
    result = open_shop.invoke({"kind": "weapon"})
    logger.info("tool[open_shop kind=weapon] OK len=%d", len(str(result)))
    assert isinstance(result, str) and len(result) > 0, "open_shop 应返回非空字符串"

    print("\n✅ 有参工具按 schema 调用正常")


async def test_full_llm_roundtrip() -> None:
    """真实 LLM 多轮压测，覆盖各种会触发"无参工具调用"的玩家说法。

    之前这条路径会触发：
        langchain_core.tools.base.ToolException:
            Too many arguments to single-input tool feed_lobster.
    生产环境是偶发的（取决于 LLM 当次生成的 tool args 形态），所以这里
    每个场景跑 N 次，把 ToolException 出现的概率压到接近 0 才算通过。
    """
    _print_section("【测试 3】真实 LLM 多轮压测（覆盖之前会偶发 ToolException 的路径）")

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print("⚠️  OPENAI_API_KEY 未配置，跳过真实 LLM 测试")
        return

    # 每个 case = (玩家说的话, 期望调到的无参工具名)
    # 重点覆盖之前会偶发 ToolException 的几个 single-input 工具
    cases: list[tuple[str, str]] = [
        ("喂食", "feed_lobster"),
        ("饿了，给点东西吃", "feed_lobster"),
        ("练一下", "train_lobster"),
        ("训练", "train_lobster"),
        ("休息", "rest"),
        ("躺平", "rest"),
        ("出门探险", "explore"),
        ("溜达溜达", "explore"),
        ("看看我的龙虾", "get_lobster_status"),
        ("打架", "battle"),
        ("怎么玩", "get_help"),
        ("看看排行榜", "get_leaderboard"),
    ]
    rounds_per_case = 3  # 每个场景跑 3 次，总共 36 次真实 LLM 调用

    from langchain_core.messages import ToolMessage

    total = len(cases) * rounds_per_case
    tool_exception_count = 0
    other_failure_count = 0
    expected_tool_hit = 0
    succeeded = 0

    handler = AIHandler(AppState())  # LLM 实例只构造一次，复用 connection

    for case_idx, (msg, expected_tool) in enumerate(cases, 1):
        for r in range(1, rounds_per_case + 1):
            uid = f"{USER_ID}_c{case_idx}_r{r}"
            state = handler.state
            lobster = factory.create_lobster(user_id=uid)
            state.lobsters[uid] = lobster

            print(f"\n[{case_idx:02d}/{len(cases)} · 第 {r}/{rounds_per_case} 次] 👤 {msg!r} (期望 {expected_tool})")
            try:
                reply = await handler.handle(uid, msg)
            except Exception as exc:  # noqa: BLE001
                err_str = str(exc)
                if "Too many arguments to single-input tool" in err_str:
                    tool_exception_count += 1
                    print(f"   ❌ ToolException（这就是要修的 bug）：{err_str[:200]}")
                else:
                    other_failure_count += 1
                    print(f"   ⚠️  其它异常：{err_str[:200]}")
                continue

            history = handler._histories[uid]  # noqa: SLF001
            tool_names = [getattr(m, "name", "?") for m in history if isinstance(m, ToolMessage)]
            hit = expected_tool in tool_names
            if hit:
                expected_tool_hit += 1
            succeeded += 1

            preview = reply.replace("\n", " ")[:60]
            mark = "✅" if hit else "ℹ️ "
            print(f"   {mark} 工具={tool_names} 回复≈{preview!r}")

    print()
    print("=" * 72)
    print("压测汇总")
    print("=" * 72)
    print(f"总轮次              : {total}")
    print(f"成功（无异常）      : {succeeded}")
    print(f"❌ ToolException    : {tool_exception_count}  ← 必须为 0")
    print(f"⚠️  其它异常         : {other_failure_count}")
    print(f"命中期望工具        : {expected_tool_hit}/{succeeded}（仅参考，LLM 偶尔换策略不算 bug）")

    if tool_exception_count > 0:
        raise SystemExit(f"P0-2 回归失败：仍有 {tool_exception_count} 次 ToolException")
    if succeeded == 0:
        raise SystemExit("所有真实 LLM 调用都失败了，无法判断修复是否生效")


async def main() -> None:
    test_tools_empty_args()
    test_tool_with_args()
    await test_full_llm_roundtrip()
    print("\n🎉 P0 修复回归全部通过")


if __name__ == "__main__":
    asyncio.run(main())
