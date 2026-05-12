"""Boss 龙虾参数预设（Phase 4）。

设计：
- Boss 是 is_bot=True bot_kind="boss" 的特殊龙虾
- 启动时通过 bot_manager.ensure_bosses 落入 lobsters 表，user_id 用 boss-<id>
- 玩家通过 challenge_boss(name) 挑战；难度由低到高，奖励比 PvP 真人/普通 bot 高
- Boss 不会被 daily 淘汰（依赖 is_bot=True 字段；当前 bot_manager 也不淘汰）
- 所有 boss 等级、属性、技能、装备都是写死的；不会被战斗结果回填修改
  （我们在 handle_pvp_boss 里只对玩家侧 apply 战果，对 boss 不调 apply）

数据合同（Lobster 兼容）：
- id        : str  全局唯一，user_id = "boss-<id>"
- name      : str  中文名（玩家挑战时输入）
- tagline   : str  简短描述（仅渲染列表用，不进 Lobster 字段）
- breed/personality/level/claw/shell/speed/stamina/luck/morale: Lobster 字段
- skills    : List[str]  已习得技能（必须存在于 skill_catalog）
- skill_levels : Dict[str, int]  显式技能等级，缺省按 1 处理
- equipped  : Dict[str, str]    装备槽 -> shop_catalog id（必须存在）
- titles    : List[str]  玩家可见的"老板称号"

不写隐式 fallback：技能/装备 id 写错，启动期 ensure_bosses 解析时直接抛错。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


BOSSES: List[Dict[str, Any]] = [
    {
        "id": "iron_pot_overlord",
        "name": "不锈钢魔王",
        "tagline": "盆地里盘踞十年的肉盾老贼，靠续航把所有挑战者熬死",
        "breed": "不锈钢盆原住民",
        "personality": "话少,但每句都伤人",
        "level": 18,
        "claw": 14, "shell": 24, "speed": 8, "stamina": 26, "luck": 10,
        "morale": 80,
        "fame": 200, "wins": 40, "losses": 5,
        "skills": ["椒盐护体", "锅气护体", "硬壳防御", "断钳重生"],
        "skill_levels": {"锅气护体": 3, "椒盐护体": 3, "硬壳防御": 2, "断钳重生": 2},
        "equipped": {
            "主钳": "rusty_pliers",
            "副钳": "chopstick_buckler",
            "背甲": "tin_carapace",
            "鞋": "lead_boots",
        },
        "titles": ["盆地之王", "钳皇"],
    },
    {
        "id": "neon_night_runner",
        "name": "霓虹夜行者",
        "tagline": "夜市里跑得比警察还快的速度流刺客，钳影还没看清人就闪回水底",
        "breed": "外卖逃逸虾",
        "personality": "戏精,喜欢假装暴毙",
        "level": 20,
        "claw": 16, "shell": 10, "speed": 26, "stamina": 14, "luck": 22,
        "morale": 88,
        "fame": 260, "wins": 55, "losses": 8,
        "skills": ["横着走", "夜场气场", "夜市传说", "盆底逃逸术"],
        "skill_levels": {"横着走": 3, "夜场气场": 3, "夜市传说": 3, "盆底逃逸术": 2},
        "equipped": {
            "主钳": "beer_cap_blade",
            "副钳": "silver_needle",
            "背甲": "plastic_cape",
            "鞋": "nike_zoom",
        },
        "titles": ["夜市名宿", "夜市小霸王"],
    },
    {
        "id": "garlic_emperor",
        "name": "蒜蓉帝王",
        "tagline": "一钳下去整条夜市都飘蒜味，纯爆发力量流，前 3 回合最致命",
        "breed": "蒜蓉派系传人",
        "personality": "极度自尊,不准任何人叫它'虾米'",
        "level": 22,
        "claw": 30, "shell": 12, "speed": 14, "stamina": 16, "luck": 18,
        "morale": 90,
        "fame": 320, "wins": 70, "losses": 12,
        "skills": ["蒜蓉觉醒", "水产之怒", "钳皇遗志", "麻辣反伤"],
        "skill_levels": {"蒜蓉觉醒": 3, "水产之怒": 3, "钳皇遗志": 2, "麻辣反伤": 3},
        "equipped": {
            "主钳": "rusty_pliers",
            "副钳": "crab_pincer",
            "背甲": "mahjong_tile_back",
            "鞋": "cotton_socks",
        },
        "titles": ["钳皇", "预制菜噩梦", "夜市名宿"],
    },
]


BOSSES_BY_ID: Dict[str, Dict[str, Any]] = {b["id"]: b for b in BOSSES}
BOSSES_BY_NAME: Dict[str, Dict[str, Any]] = {b["name"]: b for b in BOSSES}


def boss_user_id(boss_id: str) -> str:
    """boss 的 user_id 命名空间：boss-<id>。

    与 seed-XXX（普通 bot）/ wild-NNNNN（临时野虾）/ 真实 ilink user_id 都区分开，
    grep 排查方便，PvP 路由也能一眼看出对面是 boss。
    """
    return f"boss-{boss_id}"


def get_boss(name_or_id: str) -> Dict[str, Any]:
    """按"中文名"或"英文 id"查找 boss 预设。两种都查不到就抛 KeyError。"""
    norm = (name_or_id or "").strip()
    if not norm:
        raise KeyError("boss 名称为空")
    if norm in BOSSES_BY_NAME:
        return BOSSES_BY_NAME[norm]
    if norm in BOSSES_BY_ID:
        return BOSSES_BY_ID[norm]
    raise KeyError(f"找不到 boss「{norm}」")
