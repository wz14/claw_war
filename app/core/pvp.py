"""PvP 路由 + 频控 + 在线对手推送通知（Phase 4）。

三种 PvP 入口（玩家视角统一称"玩家"，bot 与真人无差别）：
- random：从"玩家池"（所有 bot_kind != "boss" && uid != self）随机抽一只
- specific：按名字精确匹配（同名取等级最高）
- boss：从 boss_catalog 预设里挑一只（user_id 形如 boss-<id>）

⚠️ BOSS 不是玩家：
- 不进 random 池（要专门走 challenge_boss 入口）
- 不进 leaderboard / compute_rank
- 不会被 PvP 通知（boss 没有 BotPool session 可投递）

频控：
- 同一对手 30 分钟内最多 1 次（写入 challenger.last_pvp_targets[opponent_uid]）
- boss 不受频控限制（固定难度副本，鼓励反复挑战）

战斗历史落盘：
- 复用 dao.save_battle_sync(is_pvp=True)
- 同时把战报推到 STATE.feed 让前端 feed 流能看到

在线对手推送通知（关键设计）：
- 战斗结束后，如果对手在 BotPool 里有活着的 session，
  通过 STATE.main_loop 异步推送一条胜负 + 嘲讽文案 + 报仇引导给对手
- 普通 bot 没有 session，自然不会被通知（不需要额外 if is_bot 判断）
- LangChain sync tool 跑在 thread executor 里，不能直接 asyncio.create_task；
  必须用 run_coroutine_threadsafe(coro, main_loop)
- 推送失败只打日志，不抛——通知是副作用，主流程战报已经返回

不写隐式 fallback：
- challenger == opponent 直接抛 ValueError
- 频控未过抛 ValueError
- main_loop 没初始化抛 RuntimeError（让上层暴露 lifespan bug）
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Dict, List, Optional

from .. import content
from ..content import boss_catalog
from ..persistence import dao
from . import battle, render
from .lobster import Lobster

if TYPE_CHECKING:
    from ..api.main import AppState

logger = logging.getLogger(__name__)


# 同一对手 30 分钟内最多打 1 次（避免刷分 + 避免给真人对手刷屏）
PVP_COOLDOWN_PER_TARGET_SECONDS = 1800

# 列出真人玩家时最多展示几条
ACTIVE_PLAYERS_DISPLAY_LIMIT = 15


# ============ 选择对手 ============


def _player_pool(
    lobsters: Dict[str, Lobster], exclude_uid: str,
) -> List[Lobster]:
    """玩家视角的"对手池"：所有 bot_kind != "boss" 的龙虾，排除自己。

    玩家视角看不出 bot / 真人区别——bot 与真人都是玩家。
    BOSS 不在此池（要走独立 challenge_boss 入口）。
    """
    return [
        l for uid, l in lobsters.items()
        if l.bot_kind != "boss" and uid != exclude_uid
    ]


def select_random_opponent(
    lobsters: Dict[str, Lobster], challenger: Lobster,
) -> Lobster:
    """从玩家池随机抽一个 PvP 对手（不含 BOSS、不含自己）。

    没玩家可打抛 ValueError（仅在系统刚部署、所有人都没注册时才会触发）。
    """
    pool = _player_pool(lobsters, challenger.user_id)
    if not pool:
        raise ValueError("现在没有可挑战的玩家，等等再来")
    opp = random.choice(pool)
    logger.info(
        "pvp.select_random_opponent: 抽中 %s (uid=%s, bot_kind=%s) for %s",
        opp.name, opp.user_id[:8], opp.bot_kind or "-", challenger.name,
    )
    return opp


def find_lobster_by_name(
    lobsters: Dict[str, Lobster], name: str, *, exclude_uid: Optional[str] = None,
) -> Lobster:
    """按"中文名"精确匹配。同名时取等级最高的那一只。

    exclude_uid 不为 None 时把对应 uid 从候选里排除（防止挑战自己）。
    匹配不到抛 ValueError。
    """
    norm = (name or "").strip()
    if not norm:
        raise ValueError("挑战谁？请告诉我对方龙虾的名字")
    hits = [
        l for uid, l in lobsters.items()
        if l.name == norm and uid != exclude_uid
    ]
    if not hits:
        raise ValueError(
            f"没找到叫「{norm}」的龙虾。可能名字打错了，"
            f"或对方还没上场（发「列玩家」看看可挑战名单）"
        )
    if len(hits) > 1:
        hits.sort(key=lambda l: (l.level, l.fame, l.wins), reverse=True)
        logger.info(
            "pvp.find_lobster_by_name: 同名 %d 只，取等级最高 uid=%s",
            len(hits), hits[0].user_id[:8],
        )
    return hits[0]


def find_boss(lobsters: Dict[str, Lobster], name_or_id: str) -> Lobster:
    """按 boss 中文名/英文 id 找到 STATE.lobsters 里的 boss 实体。

    优先走 boss_catalog 验证名字合法，再用 boss_user_id 拼出 uid 去 lobsters 取——
    这保证了"玩家可挑战的 boss" = "已经被 ensure_bosses 注入到 lobsters 的 boss"。
    """
    norm = (name_or_id or "").strip()
    if not norm:
        raise ValueError("挑战哪只 boss？发「boss 列表」看看可选项")
    try:
        spec = boss_catalog.get_boss(norm)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc
    uid = boss_catalog.boss_user_id(spec["id"])
    boss = lobsters.get(uid)
    if boss is None:
        raise ValueError(
            f"boss「{spec['name']}」还没上场，等系统启动完成（或检查 ensure_bosses 是否成功）"
        )
    if boss.bot_kind != "boss":
        raise ValueError(
            f"uid={uid} 在 lobsters 里不是 boss（bot_kind={boss.bot_kind!r}），可能是数据漂移"
        )
    return boss


# ============ 频控 ============


def assert_pvp_cooldown(challenger: Lobster, opponent: Lobster) -> None:
    """如果 challenger 30 分钟内打过 opponent 抛 ValueError；boss 不受频控。

    boss 不受频控理由：boss 是预设难度副本，玩家应该可以反复刷；
    限制 boss 反而妨碍玩家快速验证 build 调整后的胜率。
    """
    if opponent.bot_kind == "boss":
        return
    last = challenger.last_pvp_targets.get(opponent.user_id)
    if last is None:
        return
    delta = time.time() - last
    if delta < PVP_COOLDOWN_PER_TARGET_SECONDS:
        remain_min = max(1, int((PVP_COOLDOWN_PER_TARGET_SECONDS - delta) / 60))
        raise ValueError(
            f"刚打过「{opponent.name}」，让人家喘口气；{remain_min} 分钟后再来"
        )


def stamp_pvp(challenger: Lobster, opponent: Lobster) -> None:
    """记录这次 PvP 时间戳到 challenger 侧（boss 也记，方便日后调试）。"""
    challenger.last_pvp_targets[opponent.user_id] = time.time()


# ============ 真人推送通知 ============


def _build_taunt_for_opponent(
    challenger: Lobster, opponent: Lobster, opponent_won: bool,
) -> str:
    """为真人对手生成一条简短的战斗提醒，含胜负 + 嘲讽 + 报仇引导。"""
    if opponent_won:
        teaser = random.choice(content.PVP_DEFENDER_WIN_TAUNTS)
        verdict = "你赢了"
    else:
        teaser = random.choice(content.PVP_DEFENDER_LOSE_TAUNTS)
        verdict = "你输了"
    return (
        "━━━━━━━━━━━━━━━━\n"
        f"⚔️【对战通知】{challenger.name} 挑战了你\n"
        "━━━━━━━━━━━━━━━━\n"
        f"📊 结果：{verdict}\n"
        f"💬 {teaser}\n"
        "———\n"
        f"想报仇？发「挑战 {challenger.name}」直接打回去\n"
        f"完整战报：{render.battle_history_url(opponent.user_id)}"
    )


def _schedule_opponent_notify(
    state: "AppState", challenger: Lobster, opponent: Lobster, opponent_won: bool,
) -> bool:
    """对手在 BotPool 里有活着的 session 时，把通知投递到主 loop。返回是否真的投递了。

    投递规则（不区分真人/bot——bot 没有 session 自然走不到这条路径）：
    - state.pool 里有这个 user_id 的 session
    - session.dead == False
    - state.main_loop 已就绪

    投递失败只打日志，不抛异常——通知是副作用。
    """
    if state.pool is None:
        logger.warning("pvp.notify: BotPool 未就绪，跳过通知 uid=%s", opponent.user_id[:8])
        return False
    session = state.pool.get_by_user(opponent.user_id)
    if session is None:
        logger.info(
            "pvp.notify: 对手没有活动 session uid=%s（bot 或冷启动未恢复的真人）",
            opponent.user_id[:8],
        )
        return False
    if session.dead:
        logger.info(
            "pvp.notify: 对手 session 已失活，跳过 uid=%s reason=%s",
            opponent.user_id[:8], session.dead_reason,
        )
        return False
    if state.main_loop is None:
        raise RuntimeError(
            "pvp.notify: state.main_loop 未初始化，lifespan 启动逻辑有 bug"
        )

    text = _build_taunt_for_opponent(challenger, opponent, opponent_won)

    async def _do_send() -> None:
        try:
            await state.pool.send(opponent.user_id, text)
            logger.info(
                "pvp.notify: 已通知对手 %s (uid=%s) 共 %d 字",
                opponent.name, opponent.user_id[:8], len(text),
            )
        except Exception as exc:
            logger.error(
                "pvp.notify: 推送失败 uid=%s err=%s",
                opponent.user_id[:8], exc, exc_info=True,
            )

    asyncio.run_coroutine_threadsafe(_do_send(), state.main_loop)
    return True


# ============ PvP 主流程 ============


def execute_pvp(
    state: "AppState",
    challenger: Lobster,
    opponent: Lobster,
    *,
    is_boss: bool,
    source_label: str,
) -> str:
    """模拟一场 PvP，应用结果到 challenger，落 battles，推送真人对手。

    Boss 战的特殊处理：
    - 不调 apply_result_to_player(opponent)——boss 战绩 / 心情固定
    - 不通知 boss（boss 没有 BotPool session）
    - 胜利奖励翻倍（在 narration 末尾叠加额外金币 / 名气）

    PvP 真人战的特殊处理：
    - 玩家与对手都不立刻 apply 给对手；只 apply 给 challenger，
      给真人对手的"被挑战"统计在通知里展示，避免双玩家并发改对手数据竞态
    - is_pvp=1 落 battles 表
    """
    if challenger.user_id == opponent.user_id:
        raise ValueError("不能跟自己打架")

    assert_pvp_cooldown(challenger, opponent)

    logger.info(
        "pvp.execute: %s(L%d) vs %s(L%d) source=%s is_boss=%s",
        challenger.name, challenger.level,
        opponent.name, opponent.level,
        source_label, is_boss,
    )

    result = battle.simulate(challenger, opponent)
    extras = battle.apply_result_to_player(challenger, opponent, result)
    full_narration = result.narration + extras

    # Boss 胜利额外奖励：金币 / 名气加成（写到玩家身上 + 在战报末尾追加一行）
    boss_bonus_text = ""
    if is_boss and result.winner is challenger:
        bonus_coins = 30 + opponent.level * 2
        bonus_fame = 8
        challenger.coins += bonus_coins
        challenger.fame += bonus_fame
        boss_bonus_text = (
            f"\n👑 BOSS 击杀奖励：金币 +{bonus_coins}  名气 +{bonus_fame}"
        )
        full_narration += boss_bonus_text
        logger.info(
            "pvp.execute: BOSS 击杀奖励 uid=%s boss=%s +%d 金币 +%d 名气",
            challenger.user_id[:8], opponent.name, bonus_coins, bonus_fame,
        )

    stamp_pvp(challenger, opponent)

    # 战斗历史入库（is_pvp=1）
    rewards_meta = {
        **result.rewards,
        "challenger_name": challenger.name,
        "opponent_name": opponent.name,
        "winner_name": result.winner.name,
        "loser_name": result.loser.name,
        "end_round": result.end_round,
        "source": source_label,
    }
    if is_boss and result.winner is challenger:
        rewards_meta["boss_bonus_coins"] = 30 + opponent.level * 2
        rewards_meta["boss_bonus_fame"] = 8

    dao.save_battle_sync(
        challenger_uid=challenger.user_id,
        opponent_uid=opponent.user_id,
        winner_uid=result.winner.user_id,
        narration=full_narration,
        rewards_meta=rewards_meta,
        is_pvp=True,
        is_clutch=result.is_clutch,
        is_upset=result.is_upset,
    )
    logger.info(
        "pvp.execute: battles 入库 uid=%s vs %s winner=%s rounds=%d",
        challenger.user_id[:8], opponent.user_id[:8],
        result.winner.name, result.end_round,
    )

    # 推 STATE.feed（前端 feed 流能看到）
    state.feed.append({
        "ts": time.time(),
        "player": challenger.name,
        "narration": full_narration,
    })

    # 在线对手（有 BotPool 活 session）推一条战斗通知；boss 永远没 session，自动跳过。
    # 普通 bot 玩家也走这条路径但同样没 session，会自动跳过。这里不再 if is_bot 判断。
    if not is_boss:
        opponent_won = result.winner.user_id == opponent.user_id
        delivered = _schedule_opponent_notify(
            state, challenger, opponent, opponent_won=opponent_won,
        )
        if delivered:
            full_narration += "\n📨 已通知对手「" + opponent.name + "」"

    return full_narration
