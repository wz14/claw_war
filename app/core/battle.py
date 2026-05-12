"""对战引擎 + 戏剧化战报生成。

设计原则（很重要）：
- 胜负完全由规则判定，不让 AI 自由决定（防被 prompt 攻击）
- AI 只负责文字戏剧化（暂时用模板池随机）
- 残血反杀 / 高级别击杀 等特殊事件，规则照样判，但战报里会强调

Phase 5 会把这里改成多回合状态机；当前阶段先保留 3 回合实现，
但在文件位置上已搬到 core/ 子包，便于后续扩展。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Dict, List

from .. import content
from .lobster import Lobster

logger = logging.getLogger(__name__)


@dataclass
class BattleResult:
    """单场对战的结构化结果。"""

    winner: Lobster
    loser: Lobster
    is_clutch: bool          # 残血反杀
    is_upset: bool           # 以下犯上（击败比自己等级高的）
    winner_power: float
    loser_power: float
    narration: str           # 完整文字战报
    rewards: Dict[str, int]  # 胜方奖励
    consolation: str         # 败方安慰文案


def _pick(seq: List[str]) -> str:
    return random.choice(seq)


def _format_round(round_no: int, actor: Lobster, target: Lobster, hit: bool) -> str:
    """单回合文字。"""
    skill = _pick(actor.skills) if actor.skills else "普通攻击"
    flavor = _pick(content.FLAVORS_HIT) if hit else _pick(content.FLAVORS_MISS)
    move_tpl = _pick(content.ROUND_MOVES)
    line = move_tpl.format(actor=actor.name, skill=skill, flavor=flavor)
    return f"第{round_no}回合 · {actor.name} → {target.name}：\n  {line}"


def simulate(player: Lobster, opponent: Lobster) -> BattleResult:
    """模拟一场对战。

    流程：
    1. 计算双方加随机后战力
    2. 抽 3 回合戏剧化文字
    3. 高战力方胜
    4. 判定 clutch / upset
    5. 计算奖励
    """
    p_power = player.power(randomize=True)
    o_power = opponent.power(randomize=True)

    # 30% 概率出现"翻盘随机扰动"，让弱方有一线生机
    if random.random() < 0.3:
        flip_bonus = random.uniform(0, 8)
        if p_power < o_power:
            p_power += flip_bonus
        else:
            o_power += flip_bonus
        logger.debug("battle: 翻盘扰动 +%.2f", flip_bonus)

    is_player_win = p_power >= o_power
    winner = player if is_player_win else opponent
    loser = opponent if is_player_win else player

    is_upset = winner.level < loser.level
    is_clutch = winner.morale < 25 and abs(p_power - o_power) < 6

    lines: List[str] = []
    lines.append("【🦞 龙虾斗兽场 · 战报】")
    lines.append(f"场地：{_pick(content.VENUES)}  天气：{_pick(content.WEATHERS)}")
    lines.append(f"观众：{_pick(content.AUDIENCES)}")
    lines.append("———")
    lines.append(f"红方：{player.name}（Lv.{player.level}）")
    lines.append(f"  · 钳{player.claw} 壳{player.shell} 速{player.speed} 耐{player.stamina} 心情{player.morale}")
    lines.append(f"蓝方：{opponent.name}（Lv.{opponent.level}）")
    lines.append(f"  · 钳{opponent.claw} 壳{opponent.shell} 速{opponent.speed} 耐{opponent.stamina} 心情{opponent.morale}")
    lines.append("———")
    lines.append(_pick(content.OPEN_LINES).format(a=player.name, b=opponent.name))
    lines.append("")

    hit_rate_p = 0.55 + (player.luck - 5) * 0.02
    hit_rate_o = 0.55 + (opponent.luck - 5) * 0.02
    for i in range(1, 4):
        if i % 2 == 1:
            lines.append(_format_round(i, player, opponent, hit=random.random() < hit_rate_p))
        else:
            lines.append(_format_round(i, opponent, player, hit=random.random() < hit_rate_o))

    lines.append("———")
    if is_clutch:
        lines.append(f"💥【残血反杀】{winner.name} 在心情触底的情况下豪赌一击。")
    if is_upset:
        lines.append(f"⚡【以下犯上】Lv.{winner.level} 击败 Lv.{loser.level}，赛场震动。")
    lines.append(_pick(content.WIN_LINES).format(winner=winner.name, loser=loser.name))
    lines.append(f"📊 战力终值：{player.name} {p_power}  VS  {opponent.name} {o_power}")
    lines.append(f"🏆 胜者：{winner.name}")

    base_exp = 15 + (loser.level - 1) * 5
    base_coins = 8 + (loser.level - 1) * 3
    base_fame = 3
    if is_upset:
        base_exp += 10
        base_fame += 4
    if is_clutch:
        base_fame += 3
    rewards = {"exp": base_exp, "coins": base_coins, "fame": base_fame}
    consolation = _pick(content.LOSE_CONSOLATION)

    lines.append("———")
    if winner is player:
        lines.append(
            f"🎁 奖励：经验 +{rewards['exp']}  金币 +{rewards['coins']}  名气 +{rewards['fame']}"
        )
    else:
        lines.append(f"💔 你的龙虾输了。{consolation}")
        lines.append("（输了不掉东西，但心情会掉。建议先去喂食/休息恢复一下。）")

    return BattleResult(
        winner=winner,
        loser=loser,
        is_clutch=is_clutch,
        is_upset=is_upset,
        winner_power=p_power if is_player_win else o_power,
        loser_power=o_power if is_player_win else p_power,
        narration="\n".join(lines),
        rewards=rewards,
        consolation=consolation,
    )


def apply_result_to_player(player: Lobster, opponent: Lobster, result: BattleResult) -> str:
    """把战斗结果回填到玩家龙虾上，返回额外的"结算"文本（升级 / 称号）。"""
    extras: List[str] = []
    if result.winner is player:
        player.wins += 1
        player.win_streak += 1
        player.lose_streak = 0
        player.exp += result.rewards["exp"]
        player.coins += result.rewards["coins"]
        player.fame += result.rewards["fame"]
        if result.is_upset:
            player.beat_higher_level += 1
        if result.is_clutch:
            player.clutch_wins += 1
        player.morale = min(100, player.morale + 5)
    else:
        player.losses += 1
        player.lose_streak += 1
        player.win_streak = 0
        player.morale = max(0, player.morale - 8)
        player.exp += 5

    level_msg = player.maybe_level_up()
    if level_msg:
        extras.append(level_msg)

    new_titles = player.refresh_titles()
    if new_titles:
        extras.append(f"\n🏷️【获得称号】{' / '.join(new_titles)}")

    logger.info(
        "battle done: %s vs %s, winner=%s, upset=%s, clutch=%s",
        player.name, opponent.name, result.winner.name, result.is_upset, result.is_clutch,
    )
    return "".join(extras)
