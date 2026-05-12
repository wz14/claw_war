"""多回合战斗引擎（Phase 5 重写）。

设计原则（不可妥协）：
- 胜负完全由规则判定，AI 只能复述战报，不能改胜负
- 战斗状态机：blood / 精力 / 冷却 / 状态效果 / 流派协同
- 战报紧凑型一次性输出：每回合压缩成 1 行，最多 8 回合
- 派生属性（血量/攻击/防御/命中/闪避/暴击）在战斗开始时算一次，
  存到 BattleState；中间靠 buff / debuff 修改

对外接口（保留兼容 actions.handle_battle）：
- simulate(player, opponent) -> BattleResult
- apply_result_to_player(player, opponent, result) -> str
- BattleResult dataclass

战斗术语全用中文：血量 / 攻击 / 防御 / 精力 / 命中 / 闪避 / 暴击 / 第N回合。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .. import content
from ..content import shop_catalog as sc
from ..content import skill_catalog as skc
from . import shop
from .lobster import Lobster

logger = logging.getLogger(__name__)


# ============ 战斗常量（集中便于调参） ============

# 派生属性公式
HP_BASE = 40
HP_PER_STAMINA = 6
HP_PER_LEVEL = 4

SP_BASE = 20
SP_PER_LEVEL = 2
SP_MAX = 50
SP_REGEN_PER_ROUND = 5

ATK_PER_CLAW = 1.2
ATK_PER_LEVEL = 0.5
DEF_PER_SHELL = 0.8
DEF_K = 35  # 减伤因子：final = raw * (1 - def / (def + DEF_K))。
            # 数值越大，防御边际收益越小、攻击越线性。35 是 RPS 平衡甜点。

# 命中 / 闪避 / 暴击 基线（基于速度差和运气）
HIT_BASE = 70
HIT_PER_SPEED_DIFF = 1.5
HIT_MIN = 40
HIT_MAX = 95
DODGE_PER_SPEED_DIFF = 1.0
DODGE_PER_LUCK = 0.3
DODGE_MAX = 30
CRIT_PER_LUCK = 1.5
CRIT_MAX = 40
CRIT_MULT = 1.5

# 第 6 回合起的指数扣血防超时
TIMEOUT_START_ROUND = 6
TIMEOUT_BASE_RATIO = 0.10
TIMEOUT_GROWTH = 1.5

# 战斗最大回合数（硬上限，超过强制平局判出）
MAX_ROUNDS = 10


# ============ 战斗时状态 ============


@dataclass
class BattleState:
    """一方在战斗中的运行时状态。"""

    lobster: Lobster
    side: str  # "红方" / "蓝方"

    # 派生属性（开战时一次性算出）
    max_hp: int = 0
    hp: int = 0
    sp: int = 0
    atk: float = 0.0
    defense: float = 0.0
    base_hit: float = 0.0
    base_dodge: float = 0.0
    base_crit: float = 0.0

    # 流派分布与协同等级
    faction: Dict[str, int] = field(default_factory=dict)
    synergy_school: Optional[str] = None
    synergy_tier: int = 0  # 0 / 2 / 3

    # 临时 buff / debuff（每个键代表"剩余回合数"）
    self_atk_buff: float = 0.0
    self_atk_buff_rounds: int = 0
    self_atk_debuff: float = 0.0
    self_atk_debuff_rounds: int = 0
    self_dodge_buff: float = 0.0
    self_dodge_buff_rounds: int = 0
    self_dmg_taken_buff: float = 0.0  # 受伤减少比率（负数 = 减伤）
    self_dmg_taken_buff_rounds: int = 0
    self_invincible_rounds: int = 0
    self_skip_turn: bool = False

    target_hit_debuff: float = 0.0
    target_hit_debuff_rounds: int = 0

    # 技能冷却：skill_name -> 剩余回合数
    cooldowns: Dict[str, int] = field(default_factory=dict)
    # 被动技能"每场 N 次"型限次
    lethal_to_one_hp_left: int = 0
    first_hit_used: bool = False
    # 速度协同 ×3 "闪避后下次必暴击"
    pending_guaranteed_crit: bool = False


# ============ 派生属性计算 ============


def _equip_stats(lobster: Lobster) -> Dict[str, float]:
    """汇总装备的静态加成。"""
    agg: Dict[str, float] = {}
    for wid in lobster.equipped.values():
        try:
            w = sc.get_weapon(wid)
        except KeyError:
            logger.warning("battle._equip_stats: 未知装备 id %s 已跳过", wid)
            continue
        for k, v in w["stats"].items():
            agg[k] = agg.get(k, 0.0) + v

    # 副钳"力量主钳协同"装备特殊处理
    aux_id = lobster.equipped.get("副钳")
    if aux_id:
        try:
            aux = sc.get_weapon(aux_id)
            if aux["effect_id"] == "synergy_main_claw_atk_2":
                main_id = lobster.equipped.get("主钳")
                if main_id:
                    main = sc.get_weapon(main_id)
                    if main["school"] == "力量":
                        agg["攻击"] = agg.get("攻击", 0.0) + 2
        except KeyError:
            pass
    return agg


def _consume_item_buff(lobster: Lobster) -> Tuple[Dict[str, float], Optional[str]]:
    """开战前从背包取一个 buff 道具消耗掉。返回 (buff_dict, item_name)。"""
    for iid, cnt in list(lobster.inventory.items()):
        if iid in sc.WEAPONS_BY_ID:
            continue
        if cnt <= 0:
            continue
        try:
            it = sc.get_item(iid)
        except KeyError:
            continue
        lobster.inventory[iid] = cnt - 1
        if lobster.inventory[iid] <= 0:
            del lobster.inventory[iid]
        logger.info(
            "battle._consume_item_buff: uid=%s 消耗道具 %s",
            lobster.user_id[:8], it["name"],
        )
        return dict(it["buff"]), it["name"]
    return {}, None


def _build_state(lobster: Lobster, opponent: Lobster, side: str) -> Tuple[BattleState, Optional[str]]:
    """根据龙虾 + 对手数据派生 BattleState。返回 (state, 消耗的道具名 or None)。"""
    eq = _equip_stats(lobster)
    item_buff, item_name = _consume_item_buff(lobster)

    bonus_hp_cap = eq.get("血量上限", 0) + item_buff.get("血量上限", 0)
    max_hp = HP_BASE + lobster.stamina * HP_PER_STAMINA + lobster.level * HP_PER_LEVEL + int(bonus_hp_cap)
    sp = min(SP_MAX, SP_BASE + lobster.level * SP_PER_LEVEL)
    atk = (
        lobster.claw * ATK_PER_CLAW
        + lobster.level * ATK_PER_LEVEL
        + eq.get("攻击", 0)
        + item_buff.get("攻击", 0)
    )
    defense = lobster.shell * DEF_PER_SHELL + eq.get("防御", 0) + item_buff.get("防御", 0)

    speed_eff = lobster.speed + eq.get("速度", 0)
    opp_speed = opponent.speed
    try:
        opp_eq = _equip_stats(opponent)
        opp_speed += opp_eq.get("速度", 0)
    except Exception as exc:
        logger.warning("battle._build_state: 解析对手装备速度失败 %s", exc)

    base_hit = max(
        HIT_MIN,
        min(
            HIT_MAX,
            HIT_BASE
            + (speed_eff - opp_speed) * HIT_PER_SPEED_DIFF
            + eq.get("命中", 0)
            + item_buff.get("命中", 0),
        ),
    )
    base_dodge = max(
        0,
        min(
            DODGE_MAX,
            (speed_eff - opp_speed) * DODGE_PER_SPEED_DIFF
            + lobster.luck * DODGE_PER_LUCK
            + eq.get("闪避", 0)
            + item_buff.get("闪避", 0),
        ),
    )
    base_crit = min(
        CRIT_MAX,
        lobster.luck * CRIT_PER_LUCK + eq.get("暴击率", 0) + item_buff.get("暴击率", 0),
    )

    # 流派分布与协同
    dist = shop.faction_distribution(lobster)
    syn_school, syn_tier = shop.synergy_tier(dist)

    # 应用协同到派生属性（静态部分）。肉盾防御加成调低（25%/35% → 15%/20%），
    # 否则速度系/力量系打不动肉盾，弱克制无法成立。
    if syn_tier >= 2:
        if syn_school == "速度":
            base_hit = min(HIT_MAX, base_hit + 5)
            base_dodge = min(DODGE_MAX, base_dodge + 5)
        elif syn_school == "肉盾":
            defense *= 1.15
    if syn_tier >= 3:
        if syn_school == "速度":
            base_dodge = min(DODGE_MAX, base_dodge + 5)
        elif syn_school == "肉盾":
            defense *= (1.20 / 1.15)

    st = BattleState(
        lobster=lobster,
        side=side,
        max_hp=int(max_hp),
        hp=int(max_hp),
        sp=int(sp),
        atk=atk,
        defense=defense,
        base_hit=base_hit,
        base_dodge=base_dodge,
        base_crit=base_crit,
        faction=dist,
        synergy_school=syn_school,
        synergy_tier=syn_tier,
    )

    # 被动技能的"每场 N 次"型计数初始化
    for sk in lobster.skills:
        sd = skc.SKILL_CATALOG.get(sk)
        if sd is None:
            continue
        if sd["passive_effect"] == "lethal_to_one_hp":
            uses = sd["effects"].get("uses_per_battle", 1)
            st.lethal_to_one_hp_left = int(uses)

    return st, item_name


# ============ 战斗结果 ============


@dataclass
class BattleResult:
    """单场对战的结构化结果（与 Phase 4 之前兼容）。

    Phase 6 新增 `end_round`：供 actions 落 battles 表 / 微信侧战绩摘要使用，
    免去再次正则解析 narration "（第 N 回合）" 的脆弱。
    """

    winner: Lobster
    loser: Lobster
    is_clutch: bool
    is_upset: bool
    winner_power: float    # 用于兼容老字段，新引擎填胜方血量百分比 * 100
    loser_power: float
    narration: str
    rewards: Dict[str, int]
    consolation: str
    end_round: int = 0     # 结束于第几回合（含 timeout 判负 / 时间到剩血判负）


# ============ 战斗主流程 ============


def simulate(player: Lobster, opponent: Lobster) -> BattleResult:
    """模拟一场对战，返回完整结构化结果。"""
    logger.info(
        "battle.simulate: %s(Lv.%d) vs %s(Lv.%d) 开战",
        player.name, player.level, opponent.name, opponent.level,
    )

    p_state, p_item = _build_state(player, opponent, side="红方")
    o_state, o_item = _build_state(opponent, player, side="蓝方")

    lines: List[str] = []
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append(f"🦞 {player.name} vs {opponent.name}")
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append(_render_side_card(p_state))
    lines.append(_render_side_card(o_state))
    if p_item:
        lines.append(f"🍱 红方战前用了「{p_item}」")
    if o_item:
        lines.append(f"🍱 蓝方战前用了「{o_item}」")
    # 协同提示
    syn_msg = _render_synergy_banner(p_state, o_state)
    if syn_msg:
        lines.append(syn_msg)
    lines.append("———")

    # 主循环
    round_no = 0
    winner_state: Optional[BattleState] = None
    while round_no < MAX_ROUNDS:
        round_no += 1
        # 回合开始：精力回复 + 冷却递减 + 被动回血 + buff/debuff 计时
        for st in (p_state, o_state):
            _on_round_start(st, round_no, lines)

        # 决定先手
        first, second = _decide_first(p_state, o_state)

        # 出招
        for actor in (first, second):
            if actor.hp <= 0 or first.hp <= 0 and actor is second:
                break
            target = second if actor is first else first
            if actor.self_skip_turn:
                actor.self_skip_turn = False
                lines.append(f"▸ 第{round_no}回合 {actor.lobster.name}：盆底潜伏，本回合不出手")
                continue
            line = _take_turn(actor, target, round_no)
            if line:
                lines.append(line)
            if target.hp <= 0:
                winner_state = actor
                break

        if winner_state is not None:
            break

        # 超时扣血（第 6 回合起）：扣"当前血量"的百分比，让差距越拉越大、
        # 避免双方同时归零（同步扣 max_hp 百分比会出现这种情况）
        if round_no >= TIMEOUT_START_ROUND:
            decay_ratio = TIMEOUT_BASE_RATIO * (TIMEOUT_GROWTH ** (round_no - TIMEOUT_START_ROUND))
            p_loss = int(max(1, p_state.hp * decay_ratio))
            o_loss = int(max(1, o_state.hp * decay_ratio))
            p_state.hp = max(0, p_state.hp - p_loss)
            o_state.hp = max(0, o_state.hp - o_loss)
            lines.append(
                f"⏳ 第{round_no}回合体力崩盘：双方扣 ~{int(decay_ratio*100)}% 当前血量"
                f"（红方 -{p_loss} 蓝方 -{o_loss}）"
            )
            if p_state.hp <= 0 and o_state.hp <= 0:
                # 同时归零（理论上现在不会发生）：抽签判，避免永远偏向红方
                winner_state = p_state if random.random() < 0.5 else o_state
                break
            if p_state.hp <= 0:
                winner_state = o_state
                break
            if o_state.hp <= 0:
                winner_state = p_state
                break

    # 走完 MAX_ROUNDS 还没分胜负：按剩余血量百分比判
    if winner_state is None:
        p_ratio = p_state.hp / max(1, p_state.max_hp)
        o_ratio = o_state.hp / max(1, o_state.max_hp)
        winner_state = p_state if p_ratio >= o_ratio else o_state
        lines.append(f"⌛ 满 {MAX_ROUNDS} 回合还没分出胜负，按剩余血量判")

    loser_state = o_state if winner_state is p_state else p_state
    is_player_win = winner_state is p_state
    winner = player if is_player_win else opponent
    loser = opponent if is_player_win else player

    is_upset = winner.level < loser.level
    # 残血反杀：胜方收尾时血量 < 20%
    is_clutch = (winner_state.hp / max(1, winner_state.max_hp)) < 0.20

    lines.append("———")
    if is_clutch:
        lines.append(f"💥【残血反杀】{winner.name} 血量见底却撑住了最后一击")
    if is_upset:
        lines.append(f"⚡【以下犯上】Lv.{winner.level} 击败 Lv.{loser.level}，赛场震动")

    end_round = round_no
    lines.append(f"🏆 胜者：{winner.name} （第{end_round}回合）")
    lines.append(
        f"📊 终局血量：{winner.name} {winner_state.hp}/{winner_state.max_hp}  vs  "
        f"{loser.name} {loser_state.hp}/{loser_state.max_hp}"
    )

    # 奖励
    base_exp = 15 + (loser.level - 1) * 5
    base_coins = 8 + (loser.level - 1) * 3
    base_fame = 3
    if is_upset:
        base_exp += 10
        base_fame += 4
    if is_clutch:
        base_fame += 3
    rewards = {"exp": base_exp, "coins": base_coins, "fame": base_fame}

    consolation = random.choice(content.LOSE_CONSOLATION)

    lines.append("———")
    if winner is player:
        lines.append(
            f"🎁 奖励：经验 +{rewards['exp']}  金币 +{rewards['coins']}  名气 +{rewards['fame']}"
        )
    else:
        lines.append(f"💔 你的龙虾输了。{consolation}")
        lines.append("（输了不掉东西，但心情会掉。建议先去喂食/休息恢复一下。）")

    logger.info(
        "battle.simulate done: winner=%s rounds=%d upset=%s clutch=%s",
        winner.name, end_round, is_upset, is_clutch,
    )

    return BattleResult(
        winner=winner,
        loser=loser,
        is_clutch=is_clutch,
        is_upset=is_upset,
        winner_power=round(winner_state.hp / max(1, winner_state.max_hp) * 100, 1),
        loser_power=round(loser_state.hp / max(1, loser_state.max_hp) * 100, 1),
        narration="\n".join(lines),
        rewards=rewards,
        consolation=consolation,
        end_round=end_round,
    )


# ============ 子流程 ============


def _render_side_card(st: BattleState) -> str:
    """战报头部展示某一方的开局数值。"""
    parts = [f"{st.side} {st.lobster.name} Lv.{st.lobster.level}"]
    syn_str = shop.faction_short_label(st.faction)
    if syn_str != "无流派":
        parts.append(f"[{syn_str}]")
    parts.append(
        f"血{st.max_hp} 攻{int(st.atk)} 防{int(st.defense)} "
        f"命{int(st.base_hit)}% 闪{int(st.base_dodge)}% 暴{int(st.base_crit)}%"
    )
    return " ".join(parts)


def _render_synergy_banner(p: BattleState, o: BattleState) -> str:
    """如果任一方触发 ×3 协同，输出一行戏剧化提示。

    数值与 §3.1 协同表对齐——AI 会看战报点评，描述必须和代码实际算的一致，
    不然 AI 容易被战报误导后说错数值。
    """
    msgs: List[str] = []
    for st in (p, o):
        if st.synergy_tier >= 3 and st.synergy_school:
            tag = {
                "力量": "所有攻击 +15%",
                "速度": "必先手；闪避后下次必暴击；攻击 +18%",
                "肉盾": "防御 +20%、每回合回血 2.5%、致命伤 30% 留 1 血",
            }[st.synergy_school]
            msgs.append(f"🔥 {st.side}「{st.synergy_school}流」×3 协同：{tag}")
    return "\n".join(msgs) if msgs else ""


def _on_round_start(st: BattleState, round_no: int, lines: List[str]) -> None:
    """回合开始结算：精力回复 / 冷却递减 / 被动回血 / buff 计时。"""
    st.sp = min(SP_MAX, st.sp + SP_REGEN_PER_ROUND)
    for sk in list(st.cooldowns.keys()):
        st.cooldowns[sk] = max(0, st.cooldowns[sk] - 1)

    # 锅气护体：每回合回血
    for sk in st.lobster.skills:
        sd = skc.SKILL_CATALOG.get(sk)
        if sd is None:
            continue
        if sd["passive_effect"] == "regen_each_round":
            lv = st.lobster.skill_levels.get(sk, 1)
            scaled = skc.scaled_skill(sk, lv)
            ratio = scaled["effects"]["regen_ratio"]
            heal = int(st.max_hp * ratio)
            before = st.hp
            st.hp = min(st.max_hp, st.hp + heal)
            if st.hp > before:
                # 不刷屏，只在第一次提示
                if round_no == 1:
                    lines.append(f"▸ 第{round_no}回合 {st.lobster.name}「锅气护体」回 {heal} 血")

    # 肉盾协同回血（校准后）：
    # - ×2: 仅静态防御 +15%，不给回血
    # - ×3: 每回合 +2.5% max_hp（配合锅气护体 2% 总回血 ≈ 4-5%，速度可破）
    if st.synergy_school == "肉盾" and st.synergy_tier >= 3:
        heal = int(st.max_hp * 0.025)
        st.hp = min(st.max_hp, st.hp + heal)

    for wid in st.lobster.equipped.values():
        try:
            w = sc.get_weapon(wid)
        except KeyError:
            continue
        if w["effect_id"] == "hp_regen_5":
            st.hp = min(st.max_hp, st.hp + 5)

    # buff / debuff 计时减一
    if st.self_atk_buff_rounds > 0:
        st.self_atk_buff_rounds -= 1
        if st.self_atk_buff_rounds == 0:
            st.self_atk_buff = 0.0
    if st.self_atk_debuff_rounds > 0:
        st.self_atk_debuff_rounds -= 1
        if st.self_atk_debuff_rounds == 0:
            st.self_atk_debuff = 0.0
    if st.self_dodge_buff_rounds > 0:
        st.self_dodge_buff_rounds -= 1
        if st.self_dodge_buff_rounds == 0:
            st.self_dodge_buff = 0.0
    if st.self_dmg_taken_buff_rounds > 0:
        st.self_dmg_taken_buff_rounds -= 1
        if st.self_dmg_taken_buff_rounds == 0:
            st.self_dmg_taken_buff = 0.0
    if st.target_hit_debuff_rounds > 0:
        st.target_hit_debuff_rounds -= 1
        if st.target_hit_debuff_rounds == 0:
            st.target_hit_debuff = 0.0
    if st.self_invincible_rounds > 0:
        st.self_invincible_rounds -= 1


def _decide_first(p: BattleState, o: BattleState) -> Tuple[BattleState, BattleState]:
    """决定本回合先手。

    规则：
    - 双方都 "速度×3 必先手"：互相抵消，按速度差决（速度镜像就 50/50）
    - 一方独有 "速度×3 必先手"：直接它先
    - 否则速度差 > 2 决；速度差 ≤ 2 用 random 掷骰
    """
    p_speed_3 = p.synergy_school == "速度" and p.synergy_tier >= 3
    o_speed_3 = o.synergy_school == "速度" and o.synergy_tier >= 3
    if p_speed_3 and not o_speed_3:
        return p, o
    if o_speed_3 and not p_speed_3:
        return o, p
    p_spd = p.lobster.speed + _equip_stats(p.lobster).get("速度", 0)
    o_spd = o.lobster.speed + _equip_stats(o.lobster).get("速度", 0)
    if abs(p_spd - o_spd) > 2:
        return (p, o) if p_spd > o_spd else (o, p)
    return (p, o) if random.random() < 0.5 else (o, p)


def _pick_skill(actor: BattleState) -> Optional[Dict[str, Any]]:
    """挑一个能用的主动技能；都不能用返回 None（落到普通攻击）。

    简单 AI：优先大伤害技能；若血量 < 30% 且会断钳重生 / 盆底逃逸，优先用。
    """
    actives: List[Tuple[Dict[str, Any], float]] = []
    for sk in actor.lobster.skills:
        sd = skc.SKILL_CATALOG.get(sk)
        if sd is None or sd["kind"] != "主动":
            continue
        if actor.cooldowns.get(sk, 0) > 0:
            continue
        lv = actor.lobster.skill_levels.get(sk, 1)
        scaled = skc.scaled_skill(sk, lv)
        sp_cost = scaled["sp_cost"]
        # 力量协同 ×3：主动技能精力 -20%
        if actor.synergy_school == "力量" and actor.synergy_tier >= 3:
            sp_cost = int(sp_cost * 0.8)
        if actor.sp < sp_cost:
            continue
        actives.append((scaled, sp_cost))

    if not actives:
        return None

    hp_ratio = actor.hp / max(1, actor.max_hp)

    # 残血保命：HP < 30% 才考虑自愈/逃逸；HP > 70% 时直接剔除自愈，
    # 否则会出现"满血开局先用断钳重生白浪费 CD"这种 AI 智障行为
    survival_skills = [
        sd for sd, _ in actives
        if "self_heal_ratio" in sd["effects"] or "self_invincible_rounds" in sd["effects"]
    ]
    damage_skills = [
        (sd, cost) for sd, cost in actives
        if "self_heal_ratio" not in sd["effects"]
        and "self_invincible_rounds" not in sd["effects"]
    ]

    if hp_ratio < 0.30 and survival_skills:
        return survival_skills[0]
    if hp_ratio > 0.70:
        if damage_skills:
            damage_skills.sort(key=lambda x: x[0]["damage_ratio"], reverse=True)
            return damage_skills[0][0]
        return None

    actives.sort(key=lambda x: x[0]["damage_ratio"], reverse=True)
    return actives[0][0]


def _take_turn(actor: BattleState, target: BattleState, round_no: int) -> str:
    """actor 对 target 出一招。返回这一回合的战报行。"""
    if target.self_invincible_rounds > 0:
        return f"▸ 第{round_no}回合 {actor.lobster.name} 出招扑空：{target.lobster.name} 闪入水底"

    skill = _pick_skill(actor)
    if skill is not None:
        return _use_active_skill(actor, target, skill, round_no)
    return _basic_attack(actor, target, round_no)


def _use_active_skill(
    actor: BattleState, target: BattleState, skill: Dict[str, Any], round_no: int,
) -> str:
    sp_cost = skill["sp_cost"]
    if actor.synergy_school == "力量" and actor.synergy_tier >= 3:
        sp_cost = int(sp_cost * 0.8)
    actor.sp -= sp_cost
    actor.cooldowns[skill["name"]] = skill["cooldown"]

    eff = skill["effects"]

    # 自身 buff 类先施加（影响后续伤害计算）
    if "self_atk_buff" in eff:
        actor.self_atk_buff = eff["self_atk_buff"]
        actor.self_atk_buff_rounds = int(eff.get("self_atk_buff_rounds", 1)) + 1
    if "self_atk_debuff" in eff:
        actor.self_atk_debuff = eff["self_atk_debuff"]
        actor.self_atk_debuff_rounds = int(eff.get("self_atk_debuff_rounds", 1)) + 1
    if "self_dodge_buff" in eff:
        actor.self_dodge_buff = eff["self_dodge_buff"]
        actor.self_dodge_buff_rounds = int(eff.get("self_dodge_buff_rounds", 1)) + 1
    if "self_heal_ratio" in eff:
        heal = int(actor.max_hp * eff["self_heal_ratio"])
        before = actor.hp
        actor.hp = min(actor.max_hp, actor.hp + heal)
        return (
            f"▸ 第{round_no}回合 {actor.lobster.name}「{skill['name']}」自愈 "
            f"+{actor.hp - before}血 → {actor.hp}/{actor.max_hp}"
        )
    if "self_invincible_rounds" in eff:
        actor.self_invincible_rounds = int(eff["self_invincible_rounds"]) + 1
        if eff.get("self_skip_turn"):
            actor.self_skip_turn = True
        return f"▸ 第{round_no}回合 {actor.lobster.name}「{skill['name']}」遁入水底"
    if "self_damage_taken_debuff" in eff:
        actor.self_dmg_taken_buff = eff["self_damage_taken_debuff"]
        actor.self_dmg_taken_buff_rounds = int(eff.get("self_damage_taken_debuff_rounds", 1)) + 1

    # 目标 debuff（命中干扰、恐慌等）
    if "target_hit_debuff" in eff:
        target.target_hit_debuff = eff["target_hit_debuff"]
        target.target_hit_debuff_rounds = int(eff.get("target_hit_debuff_rounds", 1)) + 1

    # 偷取精力
    if "steal_sp" in eff:
        stolen = min(target.sp, int(eff["steal_sp"]))
        target.sp -= stolen
        actor.sp = min(SP_MAX, actor.sp + stolen)

    # 纯控制技能（如泡泡干扰）伤害倍率 0 → 不打伤害
    if skill["damage_ratio"] <= 0:
        return (
            f"▸ 第{round_no}回合 {actor.lobster.name}「{skill['name']}」→ "
            f"{target.lobster.name} 施加干扰"
        )

    # 走伤害结算
    return _resolve_damage(actor, target, skill, round_no)


def _basic_attack(actor: BattleState, target: BattleState, round_no: int) -> str:
    """普通攻击：伤害倍率 1.0、无 CD、无 SP 消耗。"""
    pseudo_skill = {
        "name": "普攻",
        "damage_ratio": 1.0,
        "effects": {},
    }
    return _resolve_damage(actor, target, pseudo_skill, round_no)


def _resolve_damage(
    actor: BattleState, target: BattleState, skill: Dict[str, Any], round_no: int,
) -> str:
    """命中判定 → 闪避 → 暴击 → 伤害计算 → 触发被动 → 返回战报行。"""
    must_hit = bool(skill["effects"].get("must_hit", 0))

    # 命中率
    hit = actor.base_hit
    if actor.target_hit_debuff_rounds > 0:
        hit += actor.target_hit_debuff * 100
    hit = max(5, min(100, hit))

    # 闪避率（target 视角）
    dodge = target.base_dodge + target.self_dodge_buff * 100
    if target.synergy_school == "速度" and target.synergy_tier >= 3:
        dodge = min(DODGE_MAX + 10, dodge)

    if not must_hit and random.uniform(0, 100) < dodge:
        # 速度协同 ×3：闪避后下次必暴击
        if target.synergy_school == "速度" and target.synergy_tier >= 3:
            target.pending_guaranteed_crit = True
        for wid in target.lobster.equipped.values():
            try:
                w = sc.get_weapon(wid)
            except KeyError:
                continue
            if w["effect_id"] == "sp_on_dodge_3":
                target.sp = min(SP_MAX, target.sp + 3)
        return (
            f"▸ 第{round_no}回合 {actor.lobster.name}「{skill['name']}」→ "
            f"{target.lobster.name} 闪避"
        )

    if not must_hit and random.uniform(0, 100) >= hit:
        return (
            f"▸ 第{round_no}回合 {actor.lobster.name}「{skill['name']}」→ "
            f"{target.lobster.name} 未命中"
        )

    # 暴击判定
    crit_rate = actor.base_crit
    # 夜市传说被动：暴击率 +10
    for sk in actor.lobster.skills:
        sd = skc.SKILL_CATALOG.get(sk)
        if sd is None:
            continue
        if sd["passive_effect"] == "crit_synergy":
            lv = actor.lobster.skill_levels.get(sk, 1)
            scaled = skc.scaled_skill(sk, lv)
            crit_rate += scaled["effects"]["crit_bonus"] * 100
    is_crit = actor.pending_guaranteed_crit or random.uniform(0, 100) < crit_rate
    actor.pending_guaranteed_crit = False

    # 攻击力
    atk_eff = actor.atk
    atk_eff *= 1.0 + actor.self_atk_buff + actor.self_atk_debuff

    # 流派协同伤害加成（数值经过 1000 场模拟校准）：
    # - 力量 ×2/×3：+8% / +15%（之前 +20% 让力量碾压肉盾过狠）
    # - 速度 ×2/×3：+5% / +18%（让速度爆破能撕开力量的薄防御）
    school_atk_mult = 1.0
    if actor.synergy_school == "力量":
        if actor.synergy_tier >= 3:
            school_atk_mult = 1.15
        elif actor.synergy_tier >= 2:
            school_atk_mult = 1.08
    elif actor.synergy_school == "速度":
        if actor.synergy_tier >= 3:
            school_atk_mult = 1.18
        elif actor.synergy_tier >= 2:
            school_atk_mult = 1.05

    crit_mult = CRIT_MULT
    for wid in actor.lobster.equipped.values():
        try:
            w = sc.get_weapon(wid)
        except KeyError:
            continue
        if w["effect_id"] == "crit_bonus_15":
            crit_mult += 0.15
        elif w["effect_id"] == "crit_bonus_20":
            crit_mult += 0.20

    # 水产之怒：满血目标降伤
    ratio = skill["damage_ratio"]
    if skill["effects"].get("versus_full_hp_ratio") and target.hp >= target.max_hp:
        ratio = skill["effects"]["versus_full_hp_ratio"]

    raw = atk_eff * ratio * school_atk_mult * (crit_mult if is_crit else 1.0)

    # 防御减伤
    defense = target.defense
    # 外卖订单怨念：低血防御提升
    for sk in target.lobster.skills:
        sd = skc.SKILL_CATALOG.get(sk)
        if sd is None:
            continue
        if sd["passive_effect"] == "low_hp_def":
            lv = target.lobster.skill_levels.get(sk, 1)
            scaled = skc.scaled_skill(sk, lv)
            if target.hp / max(1, target.max_hp) < scaled["effects"]["low_hp_threshold"]:
                defense *= 1.0 + scaled["effects"]["def_bonus"]

    final = max(1, int(raw * (1 - defense / (defense + DEF_K))))

    # 受伤减免：临时 buff
    if target.self_dmg_taken_buff_rounds > 0:
        final = max(1, int(final * (1 + target.self_dmg_taken_buff)))

    # 称重恐惧（首次受伤减免 50%）
    for sk in target.lobster.skills:
        sd = skc.SKILL_CATALOG.get(sk)
        if sd is None:
            continue
        if sd["passive_effect"] == "first_hit_shield" and not target.first_hit_used:
            lv = target.lobster.skill_levels.get(sk, 1)
            scaled = skc.scaled_skill(sk, lv)
            final = max(1, int(final * (1 - scaled["effects"]["first_hit_reduction"])))
            target.first_hit_used = True
            break

    # 致命减免（硬壳防御 / 肉盾协同 ×3）
    will_die = (target.hp - final) <= 0
    saved_to_one = False
    if will_die:
        # 硬壳防御
        for sk in target.lobster.skills:
            sd = skc.SKILL_CATALOG.get(sk)
            if sd is None:
                continue
            if sd["passive_effect"] == "lethal_to_one_hp" and target.lethal_to_one_hp_left > 0:
                lv = target.lobster.skill_levels.get(sk, 1)
                scaled = skc.scaled_skill(sk, lv)
                if random.random() < scaled["effects"]["trigger_chance"]:
                    final = max(0, target.hp - 1)
                    target.lethal_to_one_hp_left -= 1
                    saved_to_one = True
                    break
        # 肉盾协同 ×3
        if not saved_to_one and target.synergy_school == "肉盾" and target.synergy_tier >= 3:
            if random.random() < 0.30:
                final = max(0, target.hp - 1)
                saved_to_one = True

    target.hp = max(0, target.hp - final)

    # 被动反伤
    extras: List[str] = []
    for sk in target.lobster.skills:
        sd = skc.SKILL_CATALOG.get(sk)
        if sd is None:
            continue
        if sd["passive_effect"] == "reflect_on_hit":
            lv = target.lobster.skill_levels.get(sk, 1)
            scaled = skc.scaled_skill(sk, lv)
            if random.random() < scaled["effects"]["trigger_chance"]:
                back = max(1, int(final * scaled["effects"]["reflect_ratio"]))
                actor.hp = max(0, actor.hp - back)
                extras.append(f"反伤 -{back}")
        elif sd["passive_effect"] == "fixed_reflect_on_hit":
            lv = target.lobster.skill_levels.get(sk, 1)
            scaled = skc.scaled_skill(sk, lv)
            back = max(1, int(target.max_hp * scaled["effects"]["reflect_max_hp_ratio"]))
            actor.hp = max(0, actor.hp - back)
            extras.append(f"麻辣反伤 -{back}")

    for wid in target.lobster.equipped.values():
        try:
            w = sc.get_weapon(wid)
        except KeyError:
            continue
        if w["effect_id"] == "counter_5":
            back = max(1, int(final * 0.05))
            actor.hp = max(0, actor.hp - back)
            extras.append(f"铅鞋反弹 -{back}")

    # 暴击后回精力（夜市传说）
    if is_crit:
        for sk in actor.lobster.skills:
            sd = skc.SKILL_CATALOG.get(sk)
            if sd is None:
                continue
            if sd["passive_effect"] == "crit_synergy":
                lv = actor.lobster.skill_levels.get(sk, 1)
                scaled = skc.scaled_skill(sk, lv)
                actor.sp = min(SP_MAX, actor.sp + int(scaled["effects"]["sp_on_crit"]))

    # 战报行
    tag = "暴击" if is_crit else "命中"
    extras_text = ("·" + "·".join(extras)) if extras else ""
    saved_text = "（硬壳留 1 血）" if saved_to_one else ""
    return (
        f"▸ 第{round_no}回合 {actor.lobster.name}「{skill['name']}」→ "
        f"{target.lobster.name} {tag} -{final}{extras_text}"
        f" 血{target.hp}/{target.max_hp}{saved_text}"
    )


# ============ 战后结算（保持兼容旧调用） ============


def apply_result_to_player(player: Lobster, opponent: Lobster, result: BattleResult) -> str:
    """把战斗结果回填到玩家龙虾上。逻辑与 Phase 1-4 保持一致。"""
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
        "battle.apply: %s vs %s winner=%s upset=%s clutch=%s",
        player.name, opponent.name, result.winner.name, result.is_upset, result.is_clutch,
    )
    return "".join(extras)
