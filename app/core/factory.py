"""龙虾工厂方法：随机命名、创建玩家龙虾、生成野生对手、生成种子人机。

- create_lobster：真人玩家入坑时创建
- make_wild_opponent：临时野生对手（PvP 没匹配到真人时降级用）
- create_bot_lobster：常驻种子人机（is_bot=True，长期存留在数据库与排行榜）
"""

from __future__ import annotations

import logging
import random
import secrets
from typing import List, Optional

from .. import content
from .lobster import Lobster

logger = logging.getLogger(__name__)


def random_name() -> str:
    return random.choice(content.NAME_PREFIXES) + random.choice(content.NAME_SUFFIXES)


def create_lobster(user_id: str, name: Optional[str] = None) -> Lobster:
    """根据 user_id 生成一只随机龙虾。"""
    final_name = name or random_name()
    skills = random.sample(content.INITIAL_SKILLS, k=2)
    lobster = Lobster(
        user_id=user_id,
        name=final_name,
        breed=random.choice(content.BREEDS),
        personality=random.choice(content.PERSONALITIES),
        claw=random.randint(4, 8),
        shell=random.randint(4, 8),
        speed=random.randint(4, 8),
        stamina=random.randint(4, 8),
        luck=random.randint(3, 9),
        morale=random.randint(60, 85),
        skills=skills,
    )
    logger.info(
        "create_lobster: name=%s user_id=%s 钳力=%d 速度=%d 技能=%s",
        lobster.name, user_id[:8], lobster.claw, lobster.speed, skills,
    )
    return lobster


def _bot_user_id() -> str:
    """种子人机的 user_id 命名空间：seed-XXXXXXXX。

    与 wild-NNNNN（临时野生）和真实微信 user_id（o9cq80...）都区分开，
    便于 grep 排查、统计 npc 数量。
    """
    return f"seed-{secrets.token_hex(4)}"


def create_bot_lobster(reference_lobsters: Optional[List[Lobster]] = None) -> Lobster:
    """创建一只常驻种子人机龙虾。

    设计：
    - 玩家视角看不出与真人的区别（同名空间 / 同属性 / 同 player_card）
    - 内部用 is_bot=True + bot_kind="seed" 标记，便于运维统计与 PvP 路由
    - 属性参考 reference_lobsters（推荐传入排行榜前 20 的真人虾）：
      从中随机选一只作为基准，每个属性 ±2 抖动；level 同样 ±2 抖动；
      初始 fame 取基准 fame 的 20%~60%（让新 bot 落入榜单中游/下游而非垫底，
      避免新 bot 一上来就被秒杀，玩家也乐意挑战）
    - 完全冷启动（reference 为空）时回退到默认范围 4-8

    fame / wins / losses 是同步关系（拿了 X 分 fame 大致打了 X/5 场胜仗）：
    - wins = fame // 5
    - losses = wins // 3
    保持榜单上看到的数字内在一致，玩家不会觉得"这只 bot 没打过架但 fame 一堆"。
    """
    user_id = _bot_user_id()
    name = random_name()
    skills = random.sample(content.INITIAL_SKILLS, k=2)

    if reference_lobsters:
        ref = random.choice(reference_lobsters)
        claw = max(1, min(99, ref.claw + random.randint(-2, 2)))
        shell = max(1, min(99, ref.shell + random.randint(-2, 2)))
        speed = max(1, min(99, ref.speed + random.randint(-2, 2)))
        stamina = max(1, min(99, ref.stamina + random.randint(-2, 2)))
        luck = max(1, min(99, ref.luck + random.randint(-2, 2)))
        level = max(1, ref.level + random.randint(-2, 2))
        fame = max(0, int(ref.fame * random.uniform(0.2, 0.6)))
        morale = random.randint(60, 85)
        ref_hint = f"ref={ref.name}(L{ref.level} fame={ref.fame})"
    else:
        claw = random.randint(4, 8)
        shell = random.randint(4, 8)
        speed = random.randint(4, 8)
        stamina = random.randint(4, 8)
        luck = random.randint(3, 9)
        level = random.randint(1, 5)
        fame = 0
        morale = random.randint(60, 85)
        ref_hint = "ref=cold-start"

    wins = fame // 5
    losses = wins // 3

    bot = Lobster(
        user_id=user_id,
        name=name,
        breed=random.choice(content.BREEDS),
        personality=random.choice(content.PERSONALITIES),
        level=level,
        claw=claw,
        shell=shell,
        speed=speed,
        stamina=stamina,
        luck=luck,
        morale=morale,
        fame=fame,
        wins=wins,
        losses=losses,
        skills=skills,
        is_bot=True,
        bot_kind="seed",
    )
    logger.info(
        "create_bot_lobster: name=%s uid=%s L%d 钳%d 速%d fame=%d wins=%d %s",
        bot.name, user_id, level, claw, speed, fame, wins, ref_hint,
    )
    return bot


def make_wild_opponent(player_level: int) -> Lobster:
    """造一只野生对手，与玩家等级接近。

    标记 is_bot=True、bot_kind="wild"，后续被 PvP 通知/人机统计逻辑识别。
    """
    opp_level = max(1, player_level + random.randint(-1, 2))
    opp = create_lobster(user_id=f"wild-{random.randint(10000, 99999)}")
    opp.is_bot = True
    opp.bot_kind = "wild"
    opp.level = opp_level
    boost = (opp_level - 1) * 1
    opp.claw += boost
    opp.shell += boost
    opp.speed += boost
    opp.stamina += boost
    opp._clamp()
    return opp
