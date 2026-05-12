"""底层动作执行函数（被 agent.tools 包装成 LangChain Tool 调用）。

这一层依然保证「胜负 / 数值变化」的判定权在系统手里，AI 没法绕过；
它只能"说"，不能"判"。

Phase 5 新增动作族：商店 / 购买 / 装备 / 卸下 / 技能升级 / 查看装备面板。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Dict

from .. import content
from . import battle, factory, render, shop
from .lobster import Lobster

logger = logging.getLogger(__name__)


def _cooldown_text(seconds: int, action_cn: str) -> str:
    teasers = [
        f"{action_cn} 还在冷却，剩 {seconds} 秒。它现在比你还累。",
        f"等 {seconds} 秒。{action_cn} 这事它不想再来一次。",
        f"再等 {seconds} 秒，它正在用钳子调整呼吸。",
    ]
    return random.choice(teasers)


# ===== 状态查看 =====


def handle_status(lobster: Lobster, all_lobsters: Dict[str, Lobster]) -> str:
    """返回完整 player_card（含名气排名 + 分享链接）。

    需要 all_lobsters 才能算排名，所以从 tools._status 透传。
    """
    return render.render_player_card(lobster, all_lobsters)


# ===== 养成动作 =====


def handle_train(lobster: Lobster) -> str:
    remain = lobster.in_cooldown("train")
    if remain is not None:
        return _cooldown_text(remain, "训练")
    desc, change = lobster.train()
    extras = lobster.maybe_level_up() or ""
    new_titles = lobster.refresh_titles()
    title_msg = f"\n🏷️ 新称号：{' / '.join(new_titles)}" if new_titles else ""
    return f"🥊【训练】{lobster.name}{desc}\n变化：{change}{extras}{title_msg}"


def handle_feed(lobster: Lobster) -> str:
    remain = lobster.in_cooldown("feed")
    if remain is not None:
        return _cooldown_text(remain, "喂食")
    desc, change = lobster.feed()
    return f"🥩【喂食】{lobster.name}{desc}\n变化：{change}"


def handle_explore(lobster: Lobster) -> str:
    remain = lobster.in_cooldown("explore")
    if remain is not None:
        return _cooldown_text(remain, "探险")
    desc, change = lobster.explore()
    return f"🧭【探险】{lobster.name}{desc}\n变化：{change}"


def handle_rest(lobster: Lobster) -> str:
    remain = lobster.in_cooldown("rest")
    if remain is not None:
        return _cooldown_text(remain, "休息")
    desc, change = lobster.rest()
    suffix = ""
    if lobster.rest_count >= 5 and lobster.train_count <= lobster.rest_count // 2:
        suffix = "\n⚠️ 它已经躺得有点过分了。"
    return f"😴【休息】{lobster.name}{desc}\n变化：{change}{suffix}"


def handle_work(lobster: Lobster) -> str:
    remain = lobster.in_cooldown("work")
    if remain is not None:
        return _cooldown_text(remain, "打工")
    desc, change = lobster.work()
    return f"💼【打工】{lobster.name}{desc}\n变化：{change}"


# ===== 对战 =====


def handle_battle(lobster: Lobster) -> str:
    remain = lobster.in_cooldown("battle")
    if remain is not None:
        return _cooldown_text(remain, "挑战")
    opponent = factory.make_wild_opponent(lobster.level)
    result = battle.simulate(lobster, opponent)
    extras = battle.apply_result_to_player(lobster, opponent, result)
    lobster.last_action_at["battle"] = time.time()
    return result.narration + extras


# ===== 排行榜 / 帮助 =====


def handle_leaderboard(all_lobsters: Dict[str, Lobster]) -> str:
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


# ===== 商店 / 装备 / 技能升级（Phase 5）=====


def handle_open_shop(lobster: Lobster, kind: str) -> str:
    """打开商店面板。kind ∈ {weapon, item, skill}。

    每 2 小时全服刷新一次（同一时刻所有玩家看到的 catalog 相同）。
    """
    kind = (kind or "weapon").strip().lower()
    if kind in ("weapon", "weapons", "武器"):
        return shop.render_weapons_shop(lobster)
    if kind in ("item", "items", "道具"):
        return shop.render_items_shop(lobster)
    if kind in ("skill", "skills", "技能"):
        return shop.render_skill_shop(lobster)
    raise ValueError(f"商店分类「{kind}」不存在，可选：weapon / item / skill")


def handle_buy(lobster: Lobster, name_or_id: str) -> str:
    """购买商品。校验失败抛 ValueError，由上层转成 AI 文案。"""
    name_or_id = (name_or_id or "").strip()
    if not name_or_id:
        raise ValueError("买什么？请告诉我商品名，例如「买 牙签长矛」")
    return shop.buy(lobster, name_or_id)


def handle_equip(lobster: Lobster, name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("装备什么？请告诉我商品名，例如「装备 牙签长矛」")
    return shop.equip(lobster, name)


def handle_unequip(lobster: Lobster, slot: str) -> str:
    slot = (slot or "").strip()
    if not slot:
        raise ValueError("要卸下哪个槽位？可选：主钳 / 副钳 / 背甲 / 鞋")
    return shop.unequip(lobster, slot)


def handle_upgrade_skill(lobster: Lobster, skill_name: str) -> str:
    skill_name = (skill_name or "").strip()
    if not skill_name:
        raise ValueError("升级哪个技能？例如「升级 蒜蓉觉醒」")
    return shop.upgrade_skill(lobster, skill_name)


def handle_show_loadout(lobster: Lobster) -> str:
    """查看装备 + 技能等级面板。"""
    return shop.render_loadout(lobster)
