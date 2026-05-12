"""技能图鉴：把 15 个原始技能名全部数值化。

设计：
- 每个技能有「流派」标签（力量/速度/肉盾），用于战斗时统计流派协同
- 技能分主动 / 被动：
    * 主动：消耗精力（SP），有冷却（CD），命中后造成伤害或施加状态
    * 被动：开战时常驻或事件触发型，比如受伤反弹、致命减免
- 技能升级靠金币（lv1→lv2 80 金、lv2→lv3 220 金），效果按 §3.3 增量
- 所有字段中文，方便 AI prompt 直接引用，也方便玩家阅读

战斗引擎查询的字段（合同）：
- name           : str           技能中文名（与玩家显示名一致）
- school         : "力量"/"速度"/"肉盾"
- kind           : "主动"/"被动"
- sp_cost        : int           主动技能精力消耗；被动技能 0
- cooldown       : int           主动技能冷却回合数；被动 0
- damage_ratio   : float         主动技能伤害倍率（普通攻击 = 1.0）；被动 0
- effects        : Dict[str, float]  附加效果（自身 buff、目标 debuff、回血等）
- passive_effect : str           被动技能的触发器 id（战斗引擎按 id 查表执行）
- desc           : str           人类可读描述（给 AI / 玩家看）

升级表（lv 数值乘子）：
- 主动技能 lv2: damage_ratio +0.2, sp_cost -2, cooldown -0
  lv3 再加: damage_ratio +0.3, sp_cost -3, cooldown -1（不低于 1）
- 被动技能 lv2: 触发数值 *1.5
  lv3 再加:    触发数值 *1.5（即相对 lv1 总共 *2.25）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ============ 技能数值表 ============

SKILL_CATALOG: Dict[str, Dict[str, Any]] = {
    "蒜蓉觉醒": {
        "name": "蒜蓉觉醒",
        "school": "力量",
        "kind": "主动",
        "sp_cost": 15,
        "cooldown": 2,
        "damage_ratio": 1.6,
        "effects": {"self_atk_buff": 0.20, "self_atk_buff_rounds": 1},
        "passive_effect": "",
        "desc": "蒜香炸裂，攻击 +20% 持续 1 回合，本击伤害 1.6 倍",
    },
    "水产之怒": {
        "name": "水产之怒",
        "school": "力量",
        "kind": "主动",
        "sp_cost": 20,
        "cooldown": 3,
        "damage_ratio": 2.0,
        "effects": {"versus_full_hp_ratio": 1.5},
        "passive_effect": "",
        "desc": "怒不可遏，伤害 2.0 倍；对满血目标降为 1.5 倍",
    },
    "钳皇遗志": {
        "name": "钳皇遗志",
        "school": "力量",
        "kind": "主动",
        "sp_cost": 25,
        "cooldown": 4,
        "damage_ratio": 2.5,
        "effects": {"self_atk_debuff": -0.10, "self_atk_debuff_rounds": 1},
        "passive_effect": "",
        "desc": "倾尽全力一击伤害 2.5 倍；下回合自身攻击 -10%",
    },
    "横着走": {
        "name": "横着走",
        "school": "速度",
        "kind": "主动",
        "sp_cost": 10,
        "cooldown": 1,
        "damage_ratio": 1.0,
        "effects": {"must_hit": 1, "self_dodge_buff": 0.20, "self_dodge_buff_rounds": 1},
        "passive_effect": "",
        "desc": "横移突袭必中，下回合自身闪避 +20%",
    },
    "夜场气场": {
        "name": "夜场气场",
        "school": "速度",
        "kind": "主动",
        "sp_cost": 12,
        "cooldown": 2,
        "damage_ratio": 1.2,
        "effects": {"steal_sp": 5},
        "passive_effect": "",
        "desc": "气场压制伤害 1.2 倍，命中后偷取对方 5 精力",
    },
    "泡泡干扰": {
        "name": "泡泡干扰",
        "school": "速度",
        "kind": "主动",
        "sp_cost": 8,
        "cooldown": 2,
        "damage_ratio": 0.0,
        "effects": {"target_hit_debuff": -0.30, "target_hit_debuff_rounds": 1},
        "passive_effect": "",
        "desc": "吐泡泡干扰对手，下回合对方命中 -30%（不造成伤害）",
    },
    "椒盐护体": {
        "name": "椒盐护体",
        "school": "肉盾",
        "kind": "被动",
        "sp_cost": 0,
        "cooldown": 0,
        "damage_ratio": 0.0,
        "effects": {"trigger_chance": 0.20, "reflect_ratio": 0.30},
        "passive_effect": "reflect_on_hit",
        "desc": "受伤时 20% 概率反弹 30% 伤害",
    },
    "硬壳防御": {
        "name": "硬壳防御",
        "school": "肉盾",
        "kind": "被动",
        "sp_cost": 0,
        "cooldown": 0,
        "damage_ratio": 0.0,
        "effects": {"trigger_chance": 0.25, "uses_per_battle": 1},
        "passive_effect": "lethal_to_one_hp",
        "desc": "受致命伤前 25% 概率减免至 1 血（每场 1 次）",
    },
    "锅气护体": {
        "name": "锅气护体",
        "school": "肉盾",
        "kind": "被动",
        "sp_cost": 0,
        "cooldown": 0,
        "damage_ratio": 0.0,
        "effects": {"regen_ratio": 0.020},
        "passive_effect": "regen_each_round",
        "desc": "每回合开始恢复 2% 血量",
    },
    "断钳重生": {
        "name": "断钳重生",
        "school": "肉盾",
        "kind": "主动",
        "sp_cost": 18,
        "cooldown": 5,
        "damage_ratio": 0.0,
        "effects": {"self_heal_ratio": 0.25, "self_damage_taken_debuff": -0.20, "self_damage_taken_debuff_rounds": 1},
        "passive_effect": "",
        "desc": "舍钳保命，恢复 25% 血量，下回合受伤 -20%",
    },
    "麻辣反伤": {
        "name": "麻辣反伤",
        "school": "力量",
        "kind": "被动",
        "sp_cost": 0,
        "cooldown": 0,
        "damage_ratio": 0.0,
        "effects": {"reflect_max_hp_ratio": 0.02},
        "passive_effect": "fixed_reflect_on_hit",
        "desc": "每次被击中反弹自身 2% 血量上限的真实伤害",
    },
    "夜市传说": {
        "name": "夜市传说",
        "school": "速度",
        "kind": "被动",
        "sp_cost": 0,
        "cooldown": 0,
        "damage_ratio": 0.0,
        "effects": {"crit_bonus": 0.10, "sp_on_crit": 5},
        "passive_effect": "crit_synergy",
        "desc": "暴击率 +10%，暴击后回复 5 精力",
    },
    "外卖订单怨念": {
        "name": "外卖订单怨念",
        "school": "肉盾",
        "kind": "被动",
        "sp_cost": 0,
        "cooldown": 0,
        "damage_ratio": 0.0,
        "effects": {"low_hp_threshold": 0.30, "def_bonus": 0.40},
        "passive_effect": "low_hp_def",
        "desc": "血量低于 30% 时防御 +40%",
    },
    "盆底逃逸术": {
        "name": "盆底逃逸术",
        "school": "速度",
        "kind": "主动",
        "sp_cost": 14,
        "cooldown": 3,
        "damage_ratio": 0.0,
        "effects": {"self_invincible_rounds": 1, "self_skip_turn": 1},
        "passive_effect": "",
        "desc": "瞬间遁入水底，下回合不可被命中（本回合也不出手）",
    },
    "预制菜恐慌": {
        "name": "预制菜恐慌",
        "school": "力量",
        "kind": "主动",
        "sp_cost": 16,
        "cooldown": 3,
        "damage_ratio": 1.5,
        "effects": {"target_hit_debuff": -0.15, "target_hit_debuff_rounds": 2},
        "passive_effect": "",
        "desc": "伤害 1.5 倍，使目标恐慌，命中 -15% 持续 2 回合",
    },
    "称重恐惧": {
        "name": "称重恐惧",
        "school": "肉盾",
        "kind": "被动",
        "sp_cost": 0,
        "cooldown": 0,
        "damage_ratio": 0.0,
        "effects": {"first_hit_reduction": 0.50},
        "passive_effect": "first_hit_shield",
        "desc": "首次受伤减免 50% 伤害",
    },
}


# ============ 升级配方 ============

SKILL_UPGRADE_COSTS: Dict[int, int] = {
    # 把 skill_levels 字典里的"目标等级"映射到所需金币
    2: 80,   # lv1 -> lv2
    3: 220,  # lv2 -> lv3
}

MAX_SKILL_LEVEL = 3


def get_skill_def(name: str) -> Dict[str, Any]:
    """读取技能定义。技能名不存在时直接报错（不写隐式 fallback）。"""
    sd = SKILL_CATALOG.get(name)
    if sd is None:
        raise KeyError(f"未知技能：{name}（可能是脏数据或玩家手敲）")
    return sd


def scaled_skill(name: str, level: int) -> Dict[str, Any]:
    """按技能等级返回应用过 lv 加成的数值副本。

    level=1 直接返回 catalog 原值；level>=2 按 §3.3 增量叠加。
    """
    if level < 1 or level > MAX_SKILL_LEVEL:
        raise ValueError(f"非法技能等级 {level}（合法范围 1-{MAX_SKILL_LEVEL}）")
    base = dict(get_skill_def(name))
    base["effects"] = dict(base.get("effects", {}))

    if base["kind"] == "主动":
        if level >= 2:
            base["damage_ratio"] = round(base["damage_ratio"] + 0.2, 2)
            base["sp_cost"] = max(0, base["sp_cost"] - 2)
        if level >= 3:
            base["damage_ratio"] = round(base["damage_ratio"] + 0.3, 2)
            base["sp_cost"] = max(0, base["sp_cost"] - 3)
            base["cooldown"] = max(1, base["cooldown"] - 1)
    else:
        if level >= 2:
            for k, v in list(base["effects"].items()):
                if isinstance(v, (int, float)) and not k.endswith("_rounds") and not k.startswith("uses_"):
                    base["effects"][k] = round(v * 1.5, 4)
        if level >= 3:
            for k, v in list(base["effects"].items()):
                if isinstance(v, (int, float)) and not k.endswith("_rounds") and not k.startswith("uses_"):
                    base["effects"][k] = round(v * 1.5, 4)

    base["level"] = level
    return base


def list_school_distribution(skills: List[str]) -> Dict[str, int]:
    """统计技能流派分布（用于战斗引擎判定协同）。

    未知技能名打日志并跳过，不影响其它技能的统计。
    """
    dist: Dict[str, int] = {"力量": 0, "速度": 0, "肉盾": 0}
    for sk in skills:
        sd = SKILL_CATALOG.get(sk)
        if sd is None:
            logger.warning("skill_catalog: 未识别技能 %s，已跳过流派统计", sk)
            continue
        school = sd["school"]
        dist[school] = dist.get(school, 0) + 1
    return dist
