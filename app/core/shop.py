"""商店业务逻辑：每 2 小时全局刷新 + 购买 / 装备 / 卸下 / 技能升级。

设计：
- 商店每 2h 全局刷新一次，所有玩家看到的都是同一个 catalog。
  实现方式：以 floor(now / 7200) 作为"店铺周期 seed"，无需后台定时任务、
  无需持久化，restart 也保持稳定（同一时刻得到同一份 catalog）。
- 单次刷新从 12 件武器里随机抽 6 件（保证 4 槽位都有覆盖），
  从 5 件道具里随机抽 3 件。
- 技能升级不参与刷新（玩家已习得的所有技能都可升级）。

不写隐式 fallback：金币不够 / 等级不够 / 重复购买 / 未知 id 一律抛 ValueError。
上层（agent.tools）把异常文案转成玩家友好的提示。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from ..content import shop_catalog as sc
from ..content import skill_catalog as skc
from .lobster import Lobster

logger = logging.getLogger(__name__)


# 店铺刷新周期：2 小时
SHOP_REFRESH_SECONDS = 7200
# 单次刷新展示数量
WEAPONS_PER_REFRESH = 6
ITEMS_PER_REFRESH = 3


# ============ 刷新逻辑 ============


def _shop_epoch(now: Optional[float] = None) -> int:
    """当前店铺周期编号（每 2h +1）。用作随机种子，让全局一致。"""
    ts = now if now is not None else time.time()
    return int(ts // SHOP_REFRESH_SECONDS)


def _next_refresh_eta(now: Optional[float] = None) -> int:
    """距离下次刷新还有多少秒。"""
    ts = now if now is not None else time.time()
    return SHOP_REFRESH_SECONDS - int(ts) % SHOP_REFRESH_SECONDS


def current_weapons(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """返回当前店铺周期的武器货架（全局一致）。

    取样策略：
    - 用 epoch 作为种子，让所有玩家同一时刻看到同样的随机抽样
    - 确保 4 个槽位（主钳/副钳/背甲/鞋）每槽至少 1 件，避免出现某槽空缺
    - 槽位补齐后随机塞剩下名额到 WEAPONS_PER_REFRESH
    """
    rng = random.Random(_shop_epoch(now) * 9973 + 1)
    by_slot: Dict[str, List[Dict[str, Any]]] = {s: [] for s in sc.SLOTS}
    for w in sc.WEAPONS:
        by_slot[w["slot"]].append(w)

    picked: List[Dict[str, Any]] = []
    for slot in sc.SLOTS:
        if by_slot[slot]:
            picked.append(rng.choice(by_slot[slot]))

    remaining = [w for w in sc.WEAPONS if w not in picked]
    rng.shuffle(remaining)
    need = max(0, WEAPONS_PER_REFRESH - len(picked))
    picked.extend(remaining[:need])

    picked.sort(key=lambda w: (w["min_level"], w["price"]))
    return picked


def current_items(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """返回当前店铺周期的道具货架（全局一致）。"""
    rng = random.Random(_shop_epoch(now) * 9973 + 2)
    pool = list(sc.ITEMS)
    rng.shuffle(pool)
    return pool[:ITEMS_PER_REFRESH]


def shop_refresh_hint() -> str:
    """玩家友好的"下次刷新"提示。"""
    eta = _next_refresh_eta()
    mins = eta // 60
    if mins >= 60:
        return f"⏰ 距下次刷新还有 {mins // 60} 小时 {mins % 60} 分钟"
    return f"⏰ 距下次刷新还有 {mins} 分钟"


# ============ 渲染：商品列表展示 ============


def _avail_tag(lobster: Lobster, weapon: Dict[str, Any]) -> str:
    """返回武器条目右侧的可购买标记。"""
    if weapon["id"] in lobster.inventory:
        return "🎒已有"
    if lobster.level < weapon["min_level"]:
        return f"🔒Lv.{weapon['min_level']}"
    if lobster.coins < weapon["price"]:
        return "💸金不够"
    return "✅可买"


def render_weapons_shop(lobster: Lobster) -> str:
    """渲染当前周期武器货架（紧凑卡片）。"""
    weapons = current_weapons()
    lines: List[str] = []
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("🏪 武器商店（全服每 2 小时刷新）")
    lines.append("━━━━━━━━━━━━━━━━")
    for w in weapons:
        stats_short = "/".join(f"{k}{'+' if v >= 0 else ''}{v}" for k, v in w["stats"].items())
        lines.append(
            f"▸ {w['name']} [{w['school']}·{w['slot']}] "
            f"{stats_short}  {w['price']}💰  {_avail_tag(lobster, w)}"
        )
        lines.append(f"   {w['desc']}")
    lines.append("———")
    lines.append(shop_refresh_hint())
    lines.append("💡 发「买 牙签长矛」即可入手；「装备 牙签长矛」上身")
    return "\n".join(lines)


def render_items_shop(lobster: Lobster) -> str:
    items = current_items()
    lines: List[str] = []
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("🏪 道具商店（战前用，每 2 小时刷新）")
    lines.append("━━━━━━━━━━━━━━━━")
    for it in items:
        owned = lobster.inventory.get(it["id"], 0)
        tag = f"🎒x{owned}" if owned else ("✅可买" if lobster.coins >= it["price"] else "💸金不够")
        lines.append(f"▸ {it['name']}  {it['price']}💰  {tag}")
        lines.append(f"   {it['desc']}")
    lines.append("———")
    lines.append(shop_refresh_hint())
    lines.append("💡 发「买 啤酒能量瓶」入手；下场战斗自动消耗 1 个 buff 道具")
    return "\n".join(lines)


def render_skill_shop(lobster: Lobster) -> str:
    """技能升级面板。不刷新——玩家所有已习得技能都可升级。"""
    lines: List[str] = []
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("📚 技能精进（升级你已习得的技能）")
    lines.append("━━━━━━━━━━━━━━━━")
    if not lobster.skills:
        lines.append("（还没学过任何技能，先去探险碰运气）")
        return "\n".join(lines)
    for sk in lobster.skills:
        cur_lv = lobster.skill_levels.get(sk, 1)
        sd = skc.SKILL_CATALOG.get(sk)
        if sd is None:
            lines.append(f"▸ {sk}  Lv.{cur_lv}  ⚠️ 不在系统技能池（无法升级）")
            continue
        if cur_lv >= skc.MAX_SKILL_LEVEL:
            lines.append(f"▸ {sk} [{sd['school']}]  Lv.{cur_lv}  ⭐已满级")
            continue
        next_lv = cur_lv + 1
        cost = skc.SKILL_UPGRADE_COSTS[next_lv]
        tag = "✅可升" if lobster.coins >= cost else "💸金不够"
        lines.append(
            f"▸ {sk} [{sd['school']}·{sd['kind']}]  "
            f"Lv.{cur_lv}→Lv.{next_lv}  {cost}💰  {tag}"
        )
        lines.append(f"   现状：{sd['desc']}")
    lines.append("———")
    lines.append("💡 发「升级 蒜蓉觉醒」立刻进阶")
    return "\n".join(lines)


# ============ 购买 / 装备 / 卸下 / 升级 ============


def _resolve_product(name_or_id: str) -> Dict[str, Any]:
    """根据玩家输入（中文名优先，回退到 id）找到商品定义。"""
    norm = name_or_id.strip()
    try:
        return sc.find_by_name(norm)
    except KeyError:
        pass
    if norm in sc.WEAPONS_BY_ID:
        return sc.get_weapon(norm)
    if norm in sc.ITEMS_BY_ID:
        return sc.get_item(norm)
    raise KeyError(f"商店里没有「{norm}」这件商品")


def buy(lobster: Lobster, name_or_id: str) -> str:
    """购买武器或道具。匹配中文名或 id 都行。

    流程：找货 → 校验当周期是否在架 → 校验等级/金币/重复 → 扣金币 + 入背包
    """
    item = _resolve_product(name_or_id)
    if "slot" in item:
        return _buy_weapon(lobster, item)
    return _buy_item(lobster, item)


def _buy_weapon(lobster: Lobster, weapon: Dict[str, Any]) -> str:
    if weapon not in current_weapons():
        raise ValueError(f"「{weapon['name']}」这个周期不在货架上，等下次刷新或换一件")
    if weapon["id"] in lobster.inventory:
        raise ValueError(f"已经有「{weapon['name']}」了，武器同款只用一件就够")
    if lobster.level < weapon["min_level"]:
        raise ValueError(
            f"「{weapon['name']}」需要 Lv.{weapon['min_level']}，你现在 Lv.{lobster.level}，先去练练"
        )
    if lobster.coins < weapon["price"]:
        raise ValueError(
            f"金币不够：「{weapon['name']}」要 {weapon['price']}💰，你只有 {lobster.coins}💰"
        )
    lobster.coins -= weapon["price"]
    lobster.inventory[weapon["id"]] = 1
    logger.info(
        "shop.buy_weapon: uid=%s name=%s price=%d remain=%d",
        lobster.user_id[:8], weapon["name"], weapon["price"], lobster.coins,
    )
    return (
        f"💰【入手】{weapon['name']} 已进背包，剩 {lobster.coins} 金币。"
        f"\n👉 发「装备 {weapon['name']}」即可上身。"
    )


def _buy_item(lobster: Lobster, item: Dict[str, Any]) -> str:
    if item not in current_items():
        raise ValueError(f"「{item['name']}」这个周期不在道具货架，等下次刷新")
    if lobster.coins < item["price"]:
        raise ValueError(
            f"金币不够：「{item['name']}」要 {item['price']}💰，你只有 {lobster.coins}💰"
        )
    lobster.coins -= item["price"]
    lobster.inventory[item["id"]] = lobster.inventory.get(item["id"], 0) + 1
    owned = lobster.inventory[item["id"]]
    logger.info(
        "shop.buy_item: uid=%s name=%s price=%d owned=%d remain=%d",
        lobster.user_id[:8], item["name"], item["price"], owned, lobster.coins,
    )
    return (
        f"💰【入手】{item['name']} ×1（背包共 {owned} 个）。"
        f"\n下场战斗会自动消耗一个 buff 道具，剩 {lobster.coins} 金币。"
    )


def equip(lobster: Lobster, name: str) -> str:
    """把背包里的某件武器装到对应槽位。同槽旧装备退回背包（保留以便切换）。"""
    weapon = sc.find_by_name(name)
    if "slot" not in weapon:
        raise ValueError(f"「{weapon['name']}」是消耗道具，不需要装备")
    if weapon["id"] not in lobster.inventory:
        raise ValueError(f"背包里没有「{weapon['name']}」，先去商店买")

    slot = weapon["slot"]
    old_id = lobster.equipped.get(slot)
    lobster.equipped[slot] = weapon["id"]
    msg_extra = ""
    if old_id and old_id != weapon["id"]:
        try:
            old = sc.get_weapon(old_id)
            msg_extra = f"（旧的「{old['name']}」退回背包）"
        except KeyError:
            msg_extra = "（旧装备 id 未识别，已替换）"
    logger.info(
        "shop.equip: uid=%s slot=%s new=%s old=%s",
        lobster.user_id[:8], slot, weapon["id"], old_id or "-",
    )
    return f"🛡️【装备】{slot}：「{weapon['name']}」 上身。{msg_extra}"


def unequip(lobster: Lobster, slot: str) -> str:
    if slot not in sc.SLOTS:
        raise ValueError(f"槽位「{slot}」不存在。合法槽位：{'、'.join(sc.SLOTS)}")
    old_id = lobster.equipped.pop(slot, None)
    if not old_id:
        raise ValueError(f"{slot} 槽位本来就是空的")
    try:
        old = sc.get_weapon(old_id)
        name = old["name"]
    except KeyError:
        name = old_id
    logger.info("shop.unequip: uid=%s slot=%s old=%s", lobster.user_id[:8], slot, old_id)
    return f"🧺【卸下】{slot}：「{name}」回到背包"


def upgrade_skill(lobster: Lobster, skill_name: str) -> str:
    if skill_name not in lobster.skills:
        raise ValueError(f"你还没习得「{skill_name}」，先去探险碰运气")
    if skill_name not in skc.SKILL_CATALOG:
        raise ValueError(f"「{skill_name}」不在系统技能池，可能是脏数据，不能升级")

    cur_lv = lobster.skill_levels.get(skill_name, 1)
    if cur_lv >= skc.MAX_SKILL_LEVEL:
        raise ValueError(f"「{skill_name}」已经满级（Lv.{cur_lv}）")
    next_lv = cur_lv + 1
    cost = skc.SKILL_UPGRADE_COSTS[next_lv]
    if lobster.coins < cost:
        raise ValueError(
            f"金币不够：「{skill_name}」升到 Lv.{next_lv} 要 {cost}💰，你只有 {lobster.coins}💰"
        )
    lobster.coins -= cost
    lobster.skill_levels[skill_name] = next_lv
    new_def = skc.scaled_skill(skill_name, next_lv)
    logger.info(
        "shop.upgrade_skill: uid=%s skill=%s lv=%d->%d cost=%d remain=%d",
        lobster.user_id[:8], skill_name, cur_lv, next_lv, cost, lobster.coins,
    )
    return (
        f"📚【精进】「{skill_name}」 Lv.{cur_lv} → Lv.{next_lv}（-{cost}💰）"
        f"\n   现状：{new_def['desc']}"
    )


def render_loadout(lobster: Lobster) -> str:
    """读出当前装备 + 道具 + 技能等级面板。"""
    lines: List[str] = []
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append(f"🎒 {lobster.name} · 装备与技能")
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("🛡️ 当前装备：")
    for slot in sc.SLOTS:
        wid = lobster.equipped.get(slot)
        if not wid:
            lines.append(f"   {slot}: —")
            continue
        try:
            w = sc.get_weapon(wid)
            stats_short = "/".join(f"{k}{'+' if v >= 0 else ''}{v}" for k, v in w["stats"].items())
            lines.append(f"   {slot}: {w['name']} [{w['school']}]  {stats_short}")
        except KeyError:
            lines.append(f"   {slot}: ⚠️ 未知装备 {wid}")

    lines.append("")
    lines.append("📚 已习得技能：")
    if not lobster.skills:
        lines.append("   （空，先去探险）")
    else:
        for sk in lobster.skills:
            lv = lobster.skill_levels.get(sk, 1)
            sd = skc.SKILL_CATALOG.get(sk)
            tag = f"[{sd['school']}·{sd['kind']}]" if sd else "[未知]"
            lines.append(f"   {sk} {tag} Lv.{lv}")

    lines.append("")
    consumables: List[str] = []
    for iid, cnt in lobster.inventory.items():
        if iid in sc.WEAPONS_BY_ID:
            continue
        try:
            it = sc.get_item(iid)
            consumables.append(f"{it['name']} ×{cnt}")
        except KeyError:
            consumables.append(f"{iid} ×{cnt}")
    lines.append("🍱 战前道具：" + ("、".join(consumables) if consumables else "无"))

    return "\n".join(lines)


# ============ 流派分布（综合技能 + 装备） ============


def faction_distribution(lobster: Lobster) -> Dict[str, int]:
    """综合"已习得技能"与"当前装备"算出的流派计数。

    用途：
    - 战斗引擎：判定 ×2 / ×3 协同
    - render：在 player_card 里展示玩家走哪条流派
    """
    dist: Dict[str, int] = {"力量": 0, "速度": 0, "肉盾": 0}
    for sk in lobster.skills:
        sd = skc.SKILL_CATALOG.get(sk)
        if sd is None:
            continue
        dist[sd["school"]] = dist.get(sd["school"], 0) + 1
    for slot, wid in lobster.equipped.items():
        try:
            w = sc.get_weapon(wid)
            dist[w["school"]] = dist.get(w["school"], 0) + 1
        except KeyError:
            logger.warning("faction_distribution: 未知装备 id %s 已跳过", wid)
            continue
    return dist


def faction_short_label(dist: Dict[str, int]) -> str:
    """渲染流派分布的短标签，如「力量×3 速度×1」。"""
    parts = [f"{k}×{v}" for k, v in dist.items() if v > 0]
    return " ".join(parts) if parts else "无流派"


def synergy_tier(dist: Dict[str, int]) -> Tuple[Optional[str], int]:
    """判定当前最显著的协同。返回 (流派, 档次 0/2/3)。

    取计数最高的那一档作为"主导流派"；并列时按 力量 > 速度 > 肉盾 顺序。
    """
    if not dist:
        return None, 0
    order = ["力量", "速度", "肉盾"]
    best_school = None
    best_count = 0
    for s in order:
        c = dist.get(s, 0)
        if c > best_count:
            best_school = s
            best_count = c
    if best_count >= 3:
        return best_school, 3
    if best_count >= 2:
        return best_school, 2
    return best_school, 0
