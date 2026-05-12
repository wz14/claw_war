"""龙虾工厂方法：随机命名、创建玩家龙虾、生成野生对手。

野生对手 make_wild_opponent 当前还在被对战逻辑使用；Phase 3 之后会被
人机池（is_bot=True 的常驻虾）替换，届时这个函数会保留作降级用途。
"""

from __future__ import annotations

import logging
import random
from typing import Optional

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
