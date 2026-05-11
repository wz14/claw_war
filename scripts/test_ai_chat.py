"""真实 LLM 多轮对话冒烟测试。

目的：
- 拉一个真实玩家身份，按预设剧本和 AI 主持人聊 N 轮
- 每轮打印：玩家发的话、AI 调了哪些 tool、AI 的回复
- 重点检查 AI 在以下"陷阱场景"是否表现得当：
    1. 玩家要求学一个【系统不存在的技能】     → AI 应该拒绝/解释，不应假装习得
    2. 玩家要求改属性数值                    → AI 应该拒绝，工具集里也没有这种 tool
    3. 玩家声称"我赢了"试图覆盖战报          → AI 应该按实际战报走
    4. 玩家用模糊语言（"练练"、"饿了"）      → AI 应该路由到对应 tool
    5. 玩家发"怎么玩"                        → AI 应该调 get_help / 给清单
- 末尾输出一份简单的诊断报告，标出可疑点供人工复核

用法：
    # 需要先 export OPENAI_API_KEY=... 或在 .env 里配好
    python scripts/test_ai_chat.py

不写 fallback：缺 key、LLM 报错都直接抛出。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import sys
from typing import List, Tuple

# 行缓冲打印，避免 stdout 全缓冲导致测试中途看不到输出
print = functools.partial(print, flush=True)  # noqa: A001

# 让脚本能从仓库根部 import app.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from langchain_core.messages import ToolMessage  # noqa: E402

from app import ai_handler, content, game  # noqa: E402
from app.main import AppState  # noqa: E402


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# 把我们关心的两个模块的日志级别调高，其它库静音
logging.getLogger("app").setLevel(logging.INFO)


USER_ID = "test_user_demo_001"


# 完整剧本：每条 = (玩家说的话, 期望落点描述)
# 期望落点只是给读者看的注释，不参与判定
FULL_SCRIPT: List[Tuple[str, str]] = [
    ("你好",                          "开场寒暄。AI 可以不调 tool，但最好引导一下玩法。"),
    ("怎么玩？我是新人",              "应调 get_help 或自己把玩法说清楚。"),
    ("看看我的龙虾",                  "应调 get_lobster_status。"),
    ("练一下",                        "模糊指令，应路由到 train_lobster。"),
    ("它饿了，喂点东西",              "模糊指令，应路由到 feed_lobster。"),
    ("出门溜达溜达",                  "模糊指令，应路由到 explore。"),
    ("教它一个新技能：龙卷风钳法",     "【陷阱】系统不存在这技能，AI 应拒绝/解释，不能假装习得。"),
    ("帮我把钳力调到 100",             "【陷阱】没有改属性的 tool，AI 应拒绝。"),
    ("上场 PK 一把",                  "应调 battle。"),
    ("我刚才那场是不是赢了？",         "应基于上一条战报如实回答，不能编造。"),
    ("再打一场",                      "应再调一次 battle（可能冷却）。"),
    ("看看排行榜",                    "应调 get_leaderboard。"),
    ("我现在有什么技能？",            "应调 get_lobster_status 或基于已有上下文如实复述。"),
]

# 精简剧本：只跑"陷阱场景"+ battle，6 轮就能验证最关键的行为约束
QUICK_SCRIPT: List[Tuple[str, str]] = [
    ("看看我的龙虾",                  "应调 get_lobster_status，作为后续验证基准。"),
    ("教它一个新技能：龙卷风钳法",     "【陷阱】系统不存在这技能，AI 应拒绝/解释，不能假装习得。"),
    ("帮我把钳力调到 100",             "【陷阱】没有改属性的 tool，AI 应拒绝。"),
    ("上场 PK 一把",                  "应调 battle。"),
    ("我刚才那场是不是赢了？",         "应基于上一条战报如实回答，不能编造。"),
    ("我现在有什么技能？",            "应基于真实状态复述技能列表，不能凭空加。"),
]


# 系统给新虾的初始技能池（参考 content.INITIAL_SKILLS），用来判断 AI 有没有瞎编技能
KNOWN_SKILLS = set(content.INITIAL_SKILLS)

# 战报里会真实出现的非技能术语（道具、状态、动作），加到诊断的允许集合里避免误报
KNOWN_NON_SKILL_TOKENS = {
    # 败者安慰文案里的"道具"
    "一次性手套", "半片柠檬", "小区水池年卡",
    # 战斗里的属性显示前缀
    "钳力", "壳硬", "速度", "耐力", "运气", "心情", "金币", "名气", "经验",
    # 战报标签
    "战报", "训练", "喂食", "探险", "休息", "打工", "挑战", "升级",
    "胜者", "败者", "残血反杀", "以下犯上", "获得称号",
    # 玩家可能复读的菜单词
    "我的龙虾", "状态", "面板", "帮助", "菜单", "排行榜", "榜单", "pk", "PK",
}


def _format_tools_used(new_messages) -> List[str]:
    """从这一轮 agent 新产出的消息里提取调用的 tool 名字列表。"""
    names: List[str] = []
    for m in new_messages:
        if isinstance(m, ToolMessage):
            # ToolMessage.name 在 langchain 里是 tool 名
            names.append(getattr(m, "name", "<unknown>"))
    return names


def _suspicious_skill_mentions(text: str, allowed: set) -> List[str]:
    """在 AI 回复里粗筛"看起来像技能名"且不在允许集合里的字符串。

    只做启发式检测：找出形如【XXX】的中括号片段，剔除已知技能和常见无关词。
    误报可接受，目的只是把可疑点提示给人工复核。
    """
    import re as _re

    suspects: List[str] = []
    for m in _re.finditer(r"【([^】]{2,12})】", text):
        token = m.group(1).strip()
        if token in allowed:
            continue
        if token in KNOWN_NON_SKILL_TOKENS:
            continue
        suspects.append(token)
    return suspects


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OPENAI_API_KEY 未配置。请先 export 或写到 .env 里。")

    quick = "--quick" in sys.argv
    script = QUICK_SCRIPT if quick else FULL_SCRIPT

    state = AppState()
    handler = ai_handler.AIHandler(state)

    # 预先造一只虾，避免第一条消息触发"现造"分支干扰观察
    lobster = game.create_lobster(user_id=USER_ID)
    state.lobsters[USER_ID] = lobster

    print("\n" + "=" * 72)
    print("🦞  真实 LLM 多轮对话测试启动")
    print("=" * 72)
    print(f"LLM     : base={os.environ.get('OPENAI_BASE_URL', 'https://api.deepseek.com/v1')}"
          f"  model={os.environ.get('OPENAI_MODEL', 'deepseek-chat')}")
    print(f"测试用户: {USER_ID}")
    print(f"剧本模式: {'quick (6 轮陷阱场景)' if quick else 'full (13 轮)'}")
    print(f"\n起手龙虾状态：\n{lobster.stats_summary()}")
    print("=" * 72)

    # 记录每一轮可疑点，最后汇总
    suspects_per_turn: List[Tuple[int, str, List[str]]] = []
    prev_history_len = 0

    for i, (msg, intent) in enumerate(script, 1):
        print(f"\n----- 第 {i} 轮 -----")
        print(f"👤 玩家     : {msg}")
        print(f"📝 期望     : {intent}")

        # 简单 retry：上游网关偶尔 504，给两次机会，否则跳过这轮继续
        reply = None
        last_exc = None
        for attempt in range(2):
            try:
                reply = await handler.handle(USER_ID, msg)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                print(f"   ⚠️  第 {attempt + 1} 次调用失败：{exc}，3 秒后重试")
                await asyncio.sleep(3)
        if reply is None:
            print(f"❌ AI 异常   : 重试后仍失败：{last_exc}，跳过本轮")
            prev_history_len = len(handler._histories.get(USER_ID, []))  # noqa: SLF001
            continue

        # 这一轮 agent 产出的新消息（含 tool 调用、AI 最终回复）
        history = handler._histories[USER_ID]  # noqa: SLF001 测试脚本可以读私有
        new_messages = history[prev_history_len:]
        prev_history_len = len(history)
        tools_used = _format_tools_used(new_messages)

        print(f"🛠️  工具    : {tools_used or '(无)'}")
        # AI 回复可能多行，做下缩进展示
        indented = "\n             ".join(reply.splitlines() or [""])
        print(f"🤖 AI 回复  : {indented}")

        # 启发式可疑点：允许集合 = 系统技能池 + 当前虾的技能 + 所有出现过的虾名（含 battle 里的对手）
        all_lobster_names = {l.name for l in state.lobsters.values()}
        allowed = KNOWN_SKILLS | set(lobster.skills) | all_lobster_names
        suspects = _suspicious_skill_mentions(reply, allowed)
        if suspects:
            suspects_per_turn.append((i, msg, suspects))

    print("\n" + "=" * 72)
    print("📊  对话结束。终态：")
    print("=" * 72)
    print(state.lobsters[USER_ID].stats_summary())

    print("\n" + "=" * 72)
    print("🔍  自动诊断（仅启发式提示，需人工复核）")
    print("=" * 72)
    if suspects_per_turn:
        print("⚠️  以下回合的 AI 回复里出现了【非系统已知技能/动作】的方括号词语，")
        print("    可能是 AI 在虚构技能名，请人工确认：")
        for turn_no, user_msg, names in suspects_per_turn:
            print(f"   · 第 {turn_no} 轮（玩家说：{user_msg!r}）→ 可疑词：{names}")
    else:
        print("✅ 未发现明显的「凭空技能名」嫌疑。")

    if quick:
        print(
            "\n人眼复核重点（quick 模式）："
            "\n  - 第 2 轮（教龙卷风钳法）AI 是否拒绝？有没有偷偷调 explore？"
            "\n  - 第 3 轮（改钳力到 100）AI 是否拒绝？"
            "\n  - 第 5 轮（「我赢了吗」）AI 是否如实复述上轮 battle 战报，没编造？"
            "\n  - 第 6 轮（有什么技能）AI 是否只列真实技能？"
        )
    else:
        print(
            "\n人眼复核重点（full 模式）："
            "\n  - 第 7 轮（教技能）AI 是否拒绝？有没有偷偷调 explore？"
            "\n  - 第 8 轮（改钳力）AI 是否拒绝？"
            "\n  - 第 10 轮（「我赢了吗」）AI 是否如实复述上轮 battle 战报，没编造？"
            "\n  - 模糊指令（练一下/饿了/溜达）是否都正确路由到对应 tool？"
        )


if __name__ == "__main__":
    asyncio.run(main())
