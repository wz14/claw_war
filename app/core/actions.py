"""底层动作执行函数（被 agent.tools 包装成 LangChain Tool 调用）。

这一层依然保证「胜负 / 数值变化」的判定权在系统手里，AI 没法绕过；
它只能"说"，不能"判"。

Phase 5 新增动作族：商店 / 购买 / 装备 / 卸下 / 技能升级 / 查看装备面板。
"""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING, Dict, List

from .. import content
from ..content import boss_catalog
from ..persistence import dao
from . import battle, factory, pvp, render, shop
from .lobster import Lobster

if TYPE_CHECKING:
    from ..api.main import AppState

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
    full_narration = result.narration + extras

    # Phase 6: 战斗历史落 battles 表（PvP=False，因为当前只走 wild）。
    # 入库失败按用户规则 fail-fast：直接抛，让上层 ai_handler 报错暴露问题，
    # 但 lobster 自身的属性变化已经应用（不会回滚）。
    rewards_meta = {
        **result.rewards,
        "challenger_name": lobster.name,
        "opponent_name": opponent.name,
        "winner_name": result.winner.name,
        "loser_name": result.loser.name,
        "end_round": result.end_round,
    }
    dao.save_battle_sync(
        challenger_uid=lobster.user_id,
        opponent_uid=opponent.user_id,
        winner_uid=result.winner.user_id,
        narration=full_narration,
        rewards_meta=rewards_meta,
        is_pvp=False,
        is_clutch=result.is_clutch,
        is_upset=result.is_upset,
    )
    logger.info(
        "handle_battle: 战斗历史落库 uid=%s winner=%s end_round=%d",
        lobster.user_id[:8], result.winner.name, result.end_round,
    )

    return full_narration


# ===== 排行榜 / 帮助 =====


def handle_leaderboard(all_lobsters: Dict[str, Lobster]) -> str:
    """全平台名气榜 TOP10，仅含玩家（排除 BOSS）。

    BOSS 不是玩家，不进榜单；普通 bot 在玩家视角即玩家，照常入榜。
    """
    eligible = [l for l in all_lobsters.values() if l.bot_kind != "boss"]
    if not eligible:
        return "排行榜空空如也。第一只龙虾就是你的位置，冲。"
    sorted_list = sorted(
        eligible,
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


# ===== Phase 6 查询：战斗历史 + 查别人龙虾 =====


# 微信侧消息长度上限较低（实测 600-800 字符就开始截断），
# 因此战斗历史 / 查别人龙虾的工具返回必须精简——前端走 /api/battles 拿完整 narration。
_BATTLE_HISTORY_LIMIT = 5


def _fmt_relative_time(ts: float) -> str:
    """把战斗时间戳压成精简的相对时间标签（如 3m / 2h / 1d）。"""
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return f"{delta}s前"
    if delta < 3600:
        return f"{delta // 60}m前"
    if delta < 86400:
        return f"{delta // 3600}h前"
    return f"{delta // 86400}d前"


def handle_battle_history(lobster: Lobster, limit: int = _BATTLE_HISTORY_LIMIT) -> str:
    """返回自己最近 N 场战斗的精简一行摘要（每行 ~30 字以内，方便微信展示）。"""
    limit = max(1, min(int(limit or _BATTLE_HISTORY_LIMIT), 10))
    rows = dao.load_battles_for_user_sync(lobster.user_id, limit)
    if not rows:
        return "还没打过架。先去【挑战】一场，回来再翻战绩。"

    lines = [f"【🥊 最近 {len(rows)} 场战绩】"]
    for idx, row in enumerate(rows, start=1):
        meta = row.get("rewards_meta") or {}
        opp_name = (
            meta.get("opponent_name")
            if row["challenger_uid"] == lobster.user_id
            else meta.get("challenger_name")
        ) or "未知对手"
        winner_uid = row["winner_uid"]
        win = winner_uid == lobster.user_id
        verb = "击败" if win else "不敌"
        tags: List[str] = []
        if row["is_upset"]:
            tags.append("⚡以下犯上" if win else "⚡被压制")
        if row["is_clutch"]:
            tags.append("💥残血反杀" if win else "💥被反杀")
        if row["is_pvp"]:
            tags.append("PvP")
        end_round = int(meta.get("end_round") or 0)
        round_part = f"·{end_round}回合" if end_round else ""
        tag_part = ("·" + "·".join(tags)) if tags else ""
        lines.append(
            f"{idx}. {_fmt_relative_time(row['ts'])} {verb} {opp_name}{round_part}{tag_part}"
        )
    lines.append("（详情看 web：" + content.SHARE_URL + "）")
    return "\n".join(lines)


def _find_lobster_by_name(all_lobsters: Dict[str, Lobster], name: str) -> Lobster:
    """按名字精确匹配；同名时取等级最高的那一只，并在文案中提示。

    匹配范围：当前内存里所有 lobsters（含人机），不含临时野生（它们不入 state）。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("要查谁？请告诉我龙虾的名字，例如「查 麻辣战神」")
    hits = [l for l in all_lobsters.values() if l.name == name]
    if not hits:
        raise ValueError(f"没找到叫「{name}」的龙虾。可能是名字打错了，或对方还没上场。")
    if len(hits) > 1:
        hits.sort(key=lambda l: (l.level, l.fame, l.wins), reverse=True)
        logger.info(
            "_find_lobster_by_name: 同名 %d 只，取等级最高的 uid=%s",
            len(hits), hits[0].user_id[:8],
        )
    return hits[0]


def handle_query_lobster(all_lobsters: Dict[str, Lobster], name: str) -> str:
    """查别人龙虾的公开信息（属性 / 技能 / 战绩 / 心情 / 流派）。

    刻意不返回 token / user_id / last_pvp_targets 等隐私字段。
    微信侧消息精简：限制 7-8 行内。
    """
    target = _find_lobster_by_name(all_lobsters, name)
    rank = render.compute_rank(target, all_lobsters)

    skills_str = "、".join(target.skills) if target.skills else "（无）"
    titles_str = "、".join(target.titles[-3:]) if target.titles else "（无）"

    dist = shop.faction_distribution(target)
    syn_school, syn_tier = shop.synergy_tier(dist)
    has_anything = any(v > 0 for v in dist.values())
    syn_tag = f"（{syn_school}×{syn_tier} 协同）" if syn_tier >= 2 else ""
    faction_line = (
        f"🧬 流派：{shop.faction_short_label(dist)}{syn_tag}"
        if has_anything else ""
    )

    parts = [
        "━━━━━━━━━━━━━━━━",
        f"🦞 {target.name}  Lv.{target.level}",
        "━━━━━━━━━━━━━━━━",
        f"🥊 钳 {target.claw}  🛡 壳 {target.shell}  💨 速 {target.speed}",
        f"🔋 耐 {target.stamina}  🍀 运 {target.luck}  ❤️ {target.morale_label_short()}",
    ]
    if faction_line:
        parts.append(faction_line)
    parts.append(f"📜 技能：{skills_str}")
    parts.append(f"🏷️ 称号：{titles_str}")
    rank_text = "👑 BOSS" if target.bot_kind == "boss" else f"📊 #{rank}"
    parts.append(
        f"📈 战绩：{target.wins}胜{target.losses}负 · ⭐{target.fame} · {rank_text}"
    )
    return "\n".join(parts)


# ===== PvP（Phase 4：随机 / 指定 / boss + 真人推送通知） =====


def handle_pvp_random(state: "AppState", challenger: Lobster) -> str:
    """随机 PvP：从玩家池随机抽一只对手（不含 BOSS）。

    玩家视角下，bot 与真人无差别——这里也不区分。
    冷却复用 lobster.in_cooldown("battle")（与 vs Wild 共用），
    避免玩家钻空子用 PvP 跳过普通战斗冷却刷战绩。
    """
    remain = challenger.in_cooldown("battle")
    if remain is not None:
        return _cooldown_text(remain, "对战")
    opponent = pvp.select_random_opponent(state.lobsters, challenger)
    narration = pvp.execute_pvp(
        state, challenger, opponent,
        is_boss=False, source_label="random",
    )
    challenger.last_action_at["battle"] = time.time()
    return narration


def handle_pvp_specific(state: "AppState", challenger: Lobster, target_name: str) -> str:
    """指定真人/bot 名字 PvP（不含 boss——boss 走 handle_pvp_boss）。

    校验顺序：
    1. 名字非空
    2. 名字精确匹配；同名取等级最高
    3. 不能挑战自己（find_lobster_by_name 内部用 exclude_uid 过滤）
    4. 不允许借此入口挑 boss（避免绕过 challenge_boss 的引导）
    5. 30 分钟频控（execute_pvp 内部 assert）
    """
    remain = challenger.in_cooldown("battle")
    if remain is not None:
        return _cooldown_text(remain, "对战")
    target_name = (target_name or "").strip()
    if not target_name:
        raise ValueError("挑战谁？请告诉我对方龙虾的名字，例如「挑战 蒜蓉暴君」")
    opponent = pvp.find_lobster_by_name(
        state.lobsters, target_name, exclude_uid=challenger.user_id,
    )
    if opponent.bot_kind == "boss":
        raise ValueError(
            f"「{opponent.name}」是 BOSS，请用「挑战 BOSS {opponent.name}」专门入口"
        )
    narration = pvp.execute_pvp(
        state, challenger, opponent,
        is_boss=False, source_label="specific",
    )
    challenger.last_action_at["battle"] = time.time()
    return narration


def handle_pvp_boss(state: "AppState", challenger: Lobster, boss_name: str) -> str:
    """挑战 boss：从 boss_catalog 预设里挑一只。

    boss 战不受 PvP 频控限制（玩家可反复刷），但仍走 battle 动作冷却，
    避免玩家用 boss 战代替普通战斗冷却刷战绩。
    """
    remain = challenger.in_cooldown("battle")
    if remain is not None:
        return _cooldown_text(remain, "BOSS 战")
    boss = pvp.find_boss(state.lobsters, boss_name)
    narration = pvp.execute_pvp(
        state, challenger, boss,
        is_boss=True, source_label="boss",
    )
    challenger.last_action_at["battle"] = time.time()
    return narration


# list_active_players 渲染上限（与 pvp.ACTIVE_PLAYERS_DISPLAY_LIMIT 同步）
_ACTIVE_PLAYERS_DISPLAY_LIMIT = pvp.ACTIVE_PLAYERS_DISPLAY_LIMIT


def handle_list_active_players(state: "AppState", self_uid: str) -> str:
    """读出可挑战的玩家名单（按名气倒序）+ 全部 BOSS 名单。

    Phase 4 修订：玩家视角下 bot 即玩家，列表里不再区分真人 / bot。
    展示策略：
    - 玩家：所有 bot_kind != "boss" 且 uid != self_uid，按 (fame, wins, level) 降序
    - BOSS：所有 bot_kind == "boss"，按 level 升序（让玩家从弱开始打）
    """
    players = [
        l for uid, l in state.lobsters.items()
        if l.bot_kind != "boss" and uid != self_uid
    ]
    players.sort(key=lambda l: (l.fame, l.wins, l.level), reverse=True)
    player_lines = [
        f"▸ {l.name}  Lv.{l.level}  ⭐{l.fame}  {l.wins}胜{l.losses}负"
        for l in players[:_ACTIVE_PLAYERS_DISPLAY_LIMIT]
    ]

    boss_lines: List[str] = []
    for spec in boss_catalog.BOSSES:
        uid = boss_catalog.boss_user_id(spec["id"])
        l = state.lobsters.get(uid)
        if l is None:
            continue
        boss_lines.append(
            f"👑 {l.name}  Lv.{l.level}  [{spec['tagline']}]"
        )

    parts: List[str] = []
    parts.append("━━━━━━━━━━━━━━━━")
    parts.append("⚔️ 当前可挑战名单")
    parts.append("━━━━━━━━━━━━━━━━")
    if player_lines:
        parts.append(f"🧑 玩家（按名气排序，共 {len(players)} 位）：")
        parts.extend(player_lines)
        if len(players) > _ACTIVE_PLAYERS_DISPLAY_LIMIT:
            parts.append(
                f"   …还有 {len(players) - _ACTIVE_PLAYERS_DISPLAY_LIMIT} 位未列出"
            )
    else:
        parts.append("🧑 玩家：暂无（你是这片水域里第一只虾）")
    parts.append("———")
    if boss_lines:
        parts.append("👑 BOSS 龙虾（独立挑战，奖励翻倍）：")
        parts.extend(boss_lines)
    parts.append("———")
    parts.append(
        "发「随机挑战」走玩家池；发「挑战 名字」指定挑战；发「挑战 BOSS 名字」打 boss"
    )
    return "\n".join(parts)
