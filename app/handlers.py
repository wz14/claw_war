"""底层动作执行函数。

设计变更：
- 命令路由（parse_command / dispatch）已经被 LangChain agent 取代，整个文件
  现在只剩「动作的实际效果」函数。每个函数：
  - 入参：Lobster（以及一些上下文，比如全榜单）
  - 出参：可读的中文动作描述（包含数值变化），交给上层 AI 主持人继续戏剧化

- 这一层依然保证「胜负 / 数值变化」的判定权，AI 没法绕过；它只能"说"，不能"判"。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Dict

from . import battle, content, game

logger = logging.getLogger(__name__)


def _cooldown_text(seconds: int, action_cn: str) -> str:
    teasers = [
        f"{action_cn} 还在冷却，剩 {seconds} 秒。它现在比你还累。",
        f"等 {seconds} 秒。{action_cn} 这事它不想再来一次。",
        f"再等 {seconds} 秒，它正在用钳子调整呼吸。",
    ]
    return random.choice(teasers)


# ===== 状态查看 =====


def handle_status(lobster: game.Lobster) -> str:
    return lobster.stats_summary()


# ===== 养成动作 =====


def handle_train(lobster: game.Lobster) -> str:
    remain = lobster.in_cooldown("train")
    if remain is not None:
        return _cooldown_text(remain, "训练")
    desc, change = lobster.train()
    extras = lobster.maybe_level_up() or ""
    new_titles = lobster.refresh_titles()
    title_msg = f"\n🏷️ 新称号：{' / '.join(new_titles)}" if new_titles else ""
    return f"🥊【训练】{lobster.name}{desc}\n变化：{change}{extras}{title_msg}"


def handle_feed(lobster: game.Lobster) -> str:
    remain = lobster.in_cooldown("feed")
    if remain is not None:
        return _cooldown_text(remain, "喂食")
    desc, change = lobster.feed()
    return f"🥩【喂食】{lobster.name}{desc}\n变化：{change}"


def handle_explore(lobster: game.Lobster) -> str:
    remain = lobster.in_cooldown("explore")
    if remain is not None:
        return _cooldown_text(remain, "探险")
    desc, change = lobster.explore()
    return f"🧭【探险】{lobster.name}{desc}\n变化：{change}"


def handle_rest(lobster: game.Lobster) -> str:
    remain = lobster.in_cooldown("rest")
    if remain is not None:
        return _cooldown_text(remain, "休息")
    desc, change = lobster.rest()
    suffix = ""
    if lobster.rest_count >= 5 and lobster.train_count <= lobster.rest_count // 2:
        suffix = "\n⚠️ 它已经躺得有点过分了。"
    return f"😴【休息】{lobster.name}{desc}\n变化：{change}{suffix}"


def handle_work(lobster: game.Lobster) -> str:
    remain = lobster.in_cooldown("work")
    if remain is not None:
        return _cooldown_text(remain, "打工")
    desc, change = lobster.work()
    return f"💼【打工】{lobster.name}{desc}\n变化：{change}"


# ===== 对战 =====


def handle_battle(lobster: game.Lobster) -> str:
    remain = lobster.in_cooldown("battle")
    if remain is not None:
        return _cooldown_text(remain, "挑战")
    opponent = game.make_wild_opponent(lobster.level)
    result = battle.simulate(lobster, opponent)
    extras = battle.apply_result_to_player(lobster, opponent, result)
    lobster.last_action_at["battle"] = time.time()
    return result.narration + extras


# ===== 排行榜 / 帮助 =====


def handle_leaderboard(all_lobsters: Dict[str, game.Lobster]) -> str:
    if not all_lobsters:
        return "排行榜空空如也。第一只龙虾就是你的位置，冲。"
    sorted_list = sorted(
        all_lobsters.values(),
        key=lambda l: (l.fame, l.wins, l.level),
        reverse=True,
    )[:10]
    lines = ["【🏆 全平台龙虾名气榜 TOP10】"]
    medals = ["🥇", "🥈", "🥉"] + ["🦞"] * 7
    for i, l in enumerate(sorted_list):
        prefix = medals[i] if i < len(medals) else "  "
        lines.append(
            f"{prefix} {l.name}  Lv.{l.level}  名气{l.fame}  战绩{l.wins}胜{l.losses}负"
        )
    return "\n".join(lines)


def handle_help() -> str:
    return content.HELP_TEXT
