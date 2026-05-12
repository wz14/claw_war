"""种子人机龙虾的维护：冷启动补足下限 + 每日新增。

设计：
- 玩家视角看不到 bot / 真人区别（compute_rank、leaderboard、PvP 一视同仁）
- bot 仅由 is_bot=True 标记，不占用任何 ilink bot 凭证
- 维护循环（bot_maintenance_loop）跑在 FastAPI lifespan 启动时：
    1. 立刻 ensure_minimum_bots（启动期把 bot 补到 MIN_BOT_COUNT）
    2. 之后每 24h 跑一次：先 daily_add_bots(2)（基于 top-N 真人虾抽样），
       再 ensure_minimum_bots（保险，万一 bot 在未来 PvP 中被消耗）
- 所有新建 bot 立刻通过 dao.save_lobster 持久化，避免重启丢失

阈值常量集中在文件头，便于调整。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, List

from ..persistence import dao
from . import factory
from .lobster import Lobster

if TYPE_CHECKING:
    from ..api.main import AppState

logger = logging.getLogger(__name__)


# Bot 数量下限：低于此数会自动补足
MIN_BOT_COUNT = 10
# 每日新增 bot 数量
DAILY_ADD_COUNT = 2
# 取排行榜前 N 真人虾作为新 bot 的属性参考
REFERENCE_TOP_N = 20
# 维护循环的周期：24 小时
MAINTENANCE_INTERVAL_SECONDS = 86400
# 启动后第一次跑 daily_add 的延迟（避免启动一上来就生成 bot，给别的初始化让路）
INITIAL_DAILY_ADD_DELAY_SECONDS = 60


def _count_bots(lobsters: dict) -> int:
    """统计当前 STATE.lobsters 中 is_bot=True 的虾数。"""
    return sum(1 for l in lobsters.values() if l.is_bot)


def _top_real_lobsters(lobsters: dict, top_n: int = REFERENCE_TOP_N) -> List[Lobster]:
    """取真人虾按 (fame, wins, level) 排序的前 top_n 只，用作 bot 属性参考。

    排除 is_bot=True 的虾——bot 不能参考自己生成自己（否则会产生数据漂移：
    bot 越多越倾向于 bot 自身的属性分布，慢慢偏离真人玩家水平）。
    """
    real = [l for l in lobsters.values() if not l.is_bot]
    real.sort(key=lambda l: (l.fame, l.wins, l.level), reverse=True)
    return real[:top_n]


async def ensure_minimum_bots(state: "AppState", threshold: int = MIN_BOT_COUNT) -> int:
    """如果 bot 数量低于 threshold，补足到 threshold。返回新建数量。

    新 bot 的属性参考 top-N 真人虾分布（如果当前没有真人虾，回退到默认范围）。
    """
    current = _count_bots(state.lobsters)
    if current >= threshold:
        logger.info("bot_manager: 当前 bot=%d ≥ 下限 %d，无需补足", current, threshold)
        return 0

    need = threshold - current
    reference = _top_real_lobsters(state.lobsters)
    logger.info(
        "bot_manager: bot=%d < 下限 %d，将补足 %d 只（参考 %d 只真人虾）",
        current, threshold, need, len(reference),
    )

    created = 0
    for _ in range(need):
        bot = factory.create_bot_lobster(reference_lobsters=reference)
        state.lobsters[bot.user_id] = bot
        await dao.save_lobster(bot.user_id, bot.to_dict())
        created += 1
    logger.info("bot_manager: ensure_minimum_bots 完成，新建 %d 只", created)
    return created


async def daily_add_bots(state: "AppState", count: int = DAILY_ADD_COUNT) -> int:
    """每日例行新增 count 只 bot 龙虾。返回新建数量。

    属性参考当前排行榜前 REFERENCE_TOP_N 真人虾，让 bot 池跟随玩家整体水平演进。
    没有真人虾时回退到默认范围（不阻塞）。
    """
    reference = _top_real_lobsters(state.lobsters)
    logger.info(
        "bot_manager: 每日新增 %d 只 bot（参考 top-%d 真人虾, 实际拿到 %d 只）",
        count, REFERENCE_TOP_N, len(reference),
    )
    for _ in range(count):
        bot = factory.create_bot_lobster(reference_lobsters=reference)
        state.lobsters[bot.user_id] = bot
        await dao.save_lobster(bot.user_id, bot.to_dict())
    logger.info("bot_manager: 每日新增完成")
    return count


async def bot_maintenance_loop(state: "AppState") -> None:
    """常驻维护协程：启动立即补下限，之后每 24h 跑一次 daily_add + ensure。

    被 lifespan create_task 启动，shutdown 时 cancel。
    """
    logger.info("bot_manager: 维护循环启动")
    try:
        await ensure_minimum_bots(state)
    except Exception as exc:
        logger.error("bot_manager: 启动期 ensure_minimum_bots 异常: %s", exc, exc_info=True)

    await asyncio.sleep(INITIAL_DAILY_ADD_DELAY_SECONDS)

    while True:
        try:
            await daily_add_bots(state)
            await ensure_minimum_bots(state)
        except asyncio.CancelledError:
            logger.info("bot_manager: 维护循环被取消")
            raise
        except Exception as exc:
            logger.error("bot_manager: 维护周期异常: %s", exc, exc_info=True)

        try:
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("bot_manager: 维护循环被取消（sleep 阶段）")
            raise
