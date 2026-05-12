"""商店图鉴：武器装备 12 件 + 战前道具 5 个 + 触发效果枚举。

数据结构（合同）：
- 装备字段：
    id          : str       全局唯一英文 id（玩家不直接看到）
    name        : str       中文名
    slot        : "主钳"/"副钳"/"背甲"/"鞋"
    school      : "力量"/"速度"/"肉盾"   流派标签（决定协同贡献）
    stats       : Dict[str, int|float]   静态加成（开战合并到 BattleState）
                  支持：攻击 / 防御 / 血量上限 / 速度 / 命中 / 闪避 / 暴击率
    effect_id   : str       触发型效果 id（""=无）
    price       : int       金币价格
    min_level   : int       最低龙虾等级要求
    desc        : str       玩家友好的描述

- 道具字段（消耗品，战前用）：
    id / name   同上
    buff        : Dict[str, int|float]   下场战斗的临时加成（开战时合并 + 立即消耗）
    price       : int
    desc        : str

效果触发点（被战斗引擎按 effect_id 查表）：
- crit_bonus_15     : 暴击时伤害再 +15%
- crit_bonus_20     : 暴击时伤害再 +20%
- hp_regen_5        : 每回合开始回血 5
- sp_on_dodge_3     : 闪避一次回精力 3
- counter_5         : 受伤反弹 5% 伤害
- synergy_main_claw_atk_2 : 同时装"主钳力量"+本件副钳时，主钳 atk +2
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ============ 装备 catalog（12 件） ============

WEAPONS: List[Dict[str, Any]] = [
    # 主钳
    {
        "id": "toothpick_spear", "name": "牙签长矛", "slot": "主钳", "school": "力量",
        "stats": {"攻击": 4}, "effect_id": "",
        "price": 60, "min_level": 1,
        "desc": "牙签削尖，戳得疼。攻击 +4",
    },
    {
        "id": "rusty_pliers", "name": "生锈剪钳", "slot": "主钳", "school": "力量",
        "stats": {"攻击": 7}, "effect_id": "crit_bonus_15",
        "price": 160, "min_level": 5,
        "desc": "厨房抽屉里出土的破烂，暴击伤害 +15%。攻击 +7",
    },
    {
        "id": "beer_cap_blade", "name": "啤酒盖刀刃", "slot": "主钳", "school": "速度",
        "stats": {"攻击": 3, "命中": 5}, "effect_id": "",
        "price": 80, "min_level": 2,
        "desc": "盖儿被你卷出锋，命中 +5、攻击 +3",
    },
    # 副钳
    {
        "id": "crab_pincer", "name": "螃蟹残钳", "slot": "副钳", "school": "力量",
        "stats": {"攻击": 3}, "effect_id": "synergy_main_claw_atk_2",
        "price": 100, "min_level": 3,
        "desc": "捡来的螃蟹残钳。装力量主钳时主钳攻击 +2。攻击 +3",
    },
    {
        "id": "chopstick_buckler", "name": "筷子小盾", "slot": "副钳", "school": "肉盾",
        "stats": {"防御": 4}, "effect_id": "",
        "price": 90, "min_level": 2,
        "desc": "外卖筷子绑成的小盾。防御 +4",
    },
    {
        "id": "silver_needle", "name": "银针副手", "slot": "副钳", "school": "速度",
        "stats": {"命中": 5, "暴击率": 5}, "effect_id": "",
        "price": 140, "min_level": 4,
        "desc": "找穴位的银针。命中 +5、暴击率 +5%",
    },
    # 背甲
    {
        "id": "plastic_cape", "name": "塑料袋披风", "slot": "背甲", "school": "肉盾",
        "stats": {"防御": 5}, "effect_id": "",
        "price": 110, "min_level": 3,
        "desc": "外卖打包袋裁的披风。防御 +5",
    },
    {
        "id": "tin_carapace", "name": "铁皮龟壳", "slot": "背甲", "school": "肉盾",
        "stats": {"防御": 9, "血量上限": 20}, "effect_id": "hp_regen_5",
        "price": 280, "min_level": 7,
        "desc": "拆门帘焊的龟壳。每回合回 5 血。防御 +9、血量上限 +20",
    },
    {
        "id": "mahjong_tile_back", "name": "麻将牌背甲", "slot": "背甲", "school": "力量",
        "stats": {"防御": 4, "攻击": 2}, "effect_id": "",
        "price": 150, "min_level": 4,
        "desc": "麻将拼的硬壳。防御 +4、攻击 +2",
    },
    # 鞋
    {
        "id": "cotton_socks", "name": "棉袜跑鞋", "slot": "鞋", "school": "速度",
        "stats": {"速度": 3, "闪避": 5}, "effect_id": "",
        "price": 70, "min_level": 1,
        "desc": "脏棉袜套钳脚。速度 +3、闪避 +5%",
    },
    {
        "id": "nike_zoom", "name": "假冒 Zoom", "slot": "鞋", "school": "速度",
        "stats": {"速度": 5, "闪避": 8}, "effect_id": "sp_on_dodge_3",
        "price": 200, "min_level": 5,
        "desc": "夜市淘到的 Nike Joom。闪避后回 3 精力。速度 +5、闪避 +8%",
    },
    {
        "id": "lead_boots", "name": "铅块鞋", "slot": "鞋", "school": "肉盾",
        "stats": {"防御": 6, "速度": -2}, "effect_id": "counter_5",
        "price": 130, "min_level": 4,
        "desc": "压脚的铅块。受伤反弹 5%。防御 +6、速度 -2",
    },
]


# ============ 道具 catalog（5 个） ============

ITEMS: List[Dict[str, Any]] = [
    {
        "id": "stamina_potion", "name": "啤酒能量瓶", "buff": {"血量上限": 30},
        "price": 30,
        "desc": "下场战斗血量上限 +30",
    },
    {
        "id": "salt_shaker", "name": "海盐瓶", "buff": {"攻击": 5},
        "price": 35,
        "desc": "下场战斗攻击 +5",
    },
    {
        "id": "pepper_packet", "name": "辣椒小包", "buff": {"暴击率": 15},
        "price": 35,
        "desc": "下场战斗暴击率 +15%",
    },
    {
        "id": "seaweed_armor", "name": "紫菜护具", "buff": {"防御": 5},
        "price": 35,
        "desc": "下场战斗防御 +5",
    },
    {
        "id": "lucky_dice", "name": "幸运骰子", "buff": {"闪避": 10},
        "price": 40,
        "desc": "下场战斗闪避 +10%",
    },
]


# ============ 索引方法 ============

WEAPONS_BY_ID: Dict[str, Dict[str, Any]] = {w["id"]: w for w in WEAPONS}
ITEMS_BY_ID: Dict[str, Dict[str, Any]] = {it["id"]: it for it in ITEMS}


def get_weapon(item_id: str) -> Dict[str, Any]:
    w = WEAPONS_BY_ID.get(item_id)
    if w is None:
        raise KeyError(f"未知装备 id：{item_id}")
    return w


def get_item(item_id: str) -> Dict[str, Any]:
    it = ITEMS_BY_ID.get(item_id)
    if it is None:
        raise KeyError(f"未知道具 id：{item_id}")
    return it


def find_by_name(name: str) -> Dict[str, Any]:
    """玩家通过"中文名"指定商品时用。匹配不到直接报错。"""
    norm = name.strip()
    for w in WEAPONS:
        if w["name"] == norm:
            return w
    for it in ITEMS:
        if it["name"] == norm:
            return it
    raise KeyError(f"商店里没有叫「{norm}」的商品")


# ============ 槽位定义（用于 equip 校验） ============

SLOTS: List[str] = ["主钳", "副钳", "背甲", "鞋"]
