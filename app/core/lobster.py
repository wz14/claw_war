"""龙虾数据结构 + 养成行为方法。

设计点：
- 一只龙虾对应一个 user_id（来自微信 ilink_user_id 或者人机的合成 id）
- 所有属性都是普通整数，便于持久化和判定
- 行为方法返回 (描述文本, 属性变化文本)，方便上层拼接消息
- 不写隐式 fallback——非法操作直接抛
- 新增 is_bot/bot_kind/equipped/inventory/skill_levels/last_pvp_targets 字段
  为后续 PvP / 商店 / 多回合战斗做准备；旧 JSON 反序列化通过 from_dict 兜底默认值
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from .. import content

logger = logging.getLogger(__name__)


# 各动作冷却时间（秒）。黑客松演示用，调得很短。
ACTION_COOLDOWN_SECONDS: Dict[str, int] = {
    "train": 5,
    "feed": 5,
    "explore": 8,
    "rest": 5,
    "work": 8,
    "battle": 10,
}


@dataclass
class Lobster:
    """一只龙虾的完整状态。"""

    user_id: str              # 微信 ilink_user_id 或 bot:<uuid>
    name: str
    breed: str
    personality: str

    level: int = 1
    exp: int = 0

    # 核心战斗属性
    claw: int = 5             # 钳力
    shell: int = 5            # 壳硬
    speed: int = 5            # 速度
    stamina: int = 5          # 耐力
    luck: int = 5             # 运气

    morale: int = 70          # 心情 0-100

    coins: int = 0
    fame: int = 0

    skills: List[str] = field(default_factory=list)
    titles: List[str] = field(default_factory=list)

    # 战绩
    wins: int = 0
    losses: int = 0
    win_streak: int = 0
    lose_streak: int = 0
    beat_higher_level: int = 0
    clutch_wins: int = 0       # 残血反杀次数

    # 养成统计（用来判称号）
    train_count: int = 0
    rest_count: int = 0
    feed_count: int = 0
    explore_count: int = 0
    work_count: int = 0

    # 每个动作上次执行的时间戳，做冷却
    last_action_at: Dict[str, float] = field(default_factory=dict)

    created_at: float = field(default_factory=time.time)

    # ===== Phase 1d 新增字段（PvP / 人机 / 商店 / 多回合战斗的地基）=====

    # 是否为人机龙虾。is_bot=True 的龙虾不会被 PvP 通知，也不会被 daily 淘汰
    # 流程影响（注意：当前 phase 只是把字段加上，行为接入留给 Phase 3-4）
    is_bot: bool = False
    # 人机种类："daily"（每日刷新池）/ "top_clone"（榜首复刻）/ "wild"（兼容老的临时野虾）
    bot_kind: str = ""

    # 装备槽：slot_name -> item_id，例如 {"claw_aux": "toothpick_spear"}
    equipped: Dict[str, str] = field(default_factory=dict)
    # 道具背包：item_id -> count
    inventory: Dict[str, int] = field(default_factory=dict)
    # 技能等级：skill_name -> level（默认 1，未升级即不存在）
    skill_levels: Dict[str, int] = field(default_factory=dict)

    # PvP 频控：target_user_id -> 上次发起战斗的时间戳
    last_pvp_targets: Dict[str, float] = field(default_factory=dict)

    # ===== 序列化 =====

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Lobster":
        """容错反序列化：旧 JSON 缺少新字段时用默认值兜底。

        不写隐式 fallback 是指"业务异常路径"，但向后兼容数据迁移这种是必须的：
        线上 13 只老龙虾的 JSON 没有 is_bot 等字段，硬解会抛 TypeError。
        """
        known_fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in data.items() if k in known_fields}
        return cls(**clean)

    # ===== 衍生属性 =====

    def morale_label(self) -> str:
        """详细心情描述（带情景吐槽，给 AI prompt / 排行榜用）。"""
        if self.morale >= 85:
            return "亢奋（你是不是又给它喝啤酒了）"
        if self.morale >= 65:
            return "良好"
        if self.morale >= 40:
            return "一般"
        if self.morale >= 20:
            return "低落"
        return "想退役"

    def morale_label_short(self) -> str:
        """精简心情标签（给 player_card 页脚用，只 2 个字保证排版整齐）。"""
        if self.morale >= 85:
            return "亢奋"
        if self.morale >= 65:
            return "良好"
        if self.morale >= 40:
            return "一般"
        if self.morale >= 20:
            return "低落"
        return "想退役"

    def stage(self) -> str:
        """根据等级返回进化阶段名。"""
        stage_name = content.EVOLUTION_STAGES[0][1]
        for threshold, name in content.EVOLUTION_STAGES:
            if self.level >= threshold:
                stage_name = name
        return stage_name

    def power(self, randomize: bool = False) -> float:
        """战力公式。

        基础战力 = 钳力*1.3 + 壳硬*1.1 + 速度*1.0 + 耐力*1.2 + 运气*0.8 + 心情修正
        randomize=True 时再叠加一个 [-5, 5] 的随机数。
        """
        morale_mod = (self.morale - 50) / 12.0
        base = (
            self.claw * 1.3
            + self.shell * 1.1
            + self.speed * 1.0
            + self.stamina * 1.2
            + self.luck * 0.8
            + morale_mod
            + self.level * 0.5
        )
        if randomize:
            base += random.uniform(-5.0, 5.0)
        return round(base, 2)

    def stats_summary(self) -> str:
        """简化版属性面板（不含技能/称号/战绩，由 Phase 2 排版规约统一）。

        注意：此方法 lobster 类内独立可用，**不**含名气排名 / 分享链接，
        因为这两项需要 all_lobsters 上下文。完整 player_card 走
        app.core.render.render_player_card(lobster, all_lobsters)。
        """
        return (
            f"━━━━━━━━━━━━━━━━\n"
            f"🦞 {self.name}  Lv.{self.level}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🥊 钳 {self.claw}  🛡 壳 {self.shell}  💨 速 {self.speed}\n"
            f"🔋 耐 {self.stamina}  🍀 运 {self.luck}  "
            f"❤️ 心情 {self.morale_label_short()}\n"
            f"\n"
            f"🎒 金币 {self.coins}  ⭐ 名气 {self.fame}"
        )

    # ===== 行为辅助 =====

    def _clamp(self) -> None:
        """把属性截到合理范围。"""
        self.morale = max(0, min(100, self.morale))
        for attr in ("claw", "shell", "speed", "stamina", "luck"):
            v = getattr(self, attr)
            setattr(self, attr, max(1, min(99, v)))

    def _apply_delta(self, delta: Dict[str, Any]) -> str:
        """把事件 delta 应用到属性上，返回人类可读的变化文本。"""
        parts: List[str] = []
        for key, val in delta.items():
            if key.startswith("_"):
                continue
            if not hasattr(self, key):
                logger.warning("apply_delta: 未知属性 %s", key)
                continue
            cur = getattr(self, key)
            setattr(self, key, cur + val)
            sign = "+" if val >= 0 else ""
            parts.append(f"{self._cn_label(key)} {sign}{val}")
        if "_skill" in delta:
            skill = delta["_skill"]
            if skill not in self.skills:
                self.skills.append(skill)
                parts.append(f"习得新技能【{skill}】")
        self._clamp()
        return "  ".join(parts) if parts else "(无变化，怪事)"

    @staticmethod
    def _cn_label(key: str) -> str:
        return {
            "claw": "钳力",
            "shell": "壳硬",
            "speed": "速度",
            "stamina": "耐力",
            "luck": "运气",
            "morale": "心情",
            "coins": "金币",
            "fame": "名气",
        }.get(key, key)

    def in_cooldown(self, action: str) -> Optional[int]:
        """返回剩余冷却秒数；不在冷却则返回 None。"""
        cd = ACTION_COOLDOWN_SECONDS.get(action, 0)
        last = self.last_action_at.get(action, 0.0)
        remain = int(cd - (time.time() - last))
        return remain if remain > 0 else None

    def _stamp(self, action: str) -> None:
        self.last_action_at[action] = time.time()

    # ===== 行为方法 =====

    def train(self) -> Tuple[str, str]:
        desc, delta = random.choice(content.TRAIN_EVENTS)
        change = self._apply_delta(delta)
        self.train_count += 1
        self.exp += 4
        self._stamp("train")
        return desc, change

    def feed(self) -> Tuple[str, str]:
        desc, delta = random.choice(content.FEED_EVENTS)
        change = self._apply_delta(delta)
        self.feed_count += 1
        self.exp += 2
        self._stamp("feed")
        return desc, change

    def explore(self) -> Tuple[str, str]:
        desc, delta = random.choice(content.EXPLORE_EVENTS)
        change = self._apply_delta(delta)
        self.explore_count += 1
        self.exp += 6
        self._stamp("explore")
        return desc, change

    def rest(self) -> Tuple[str, str]:
        desc = random.choice(content.REST_EVENTS)
        delta = {"morale": 15, "stamina": 2}
        change = self._apply_delta(delta)
        self.rest_count += 1
        self._stamp("rest")
        return desc, change

    def work(self) -> Tuple[str, str]:
        desc, delta = random.choice(content.WORK_EVENTS)
        change = self._apply_delta(delta)
        self.work_count += 1
        self.exp += 3
        self._stamp("work")
        return desc, change

    # ===== 升级 =====

    def maybe_level_up(self) -> Optional[str]:
        """如果经验够，升级并返回提示文本；否则返回 None。"""
        threshold = 20 + self.level * 10
        if self.exp < threshold:
            return None
        self.exp -= threshold
        self.level += 1
        attr = random.choice(["claw", "shell", "speed", "stamina", "luck"])
        bump = random.randint(1, 2)
        setattr(self, attr, getattr(self, attr) + bump)
        self._clamp()
        new_stage = self.stage()
        logger.info("lobster %s 升到 Lv.%d，奖励 %s+%d", self.name, self.level, attr, bump)
        return (
            f"\n🎉【升级】{self.name} 升到 Lv.{self.level}！"
            f"当前阶段：{new_stage}\n奖励：{self._cn_label(attr)} +{bump}"
        )

    # ===== 称号 =====

    def refresh_titles(self) -> List[str]:
        """检查并授予新称号。返回这次新拿到的称号列表。"""
        newly: List[str] = []
        for title, _desc, predicate in content.TITLES:
            if title in self.titles:
                continue
            try:
                ok = predicate(self)
            except Exception as exc:
                logger.warning("title predicate %s failed: %s", title, exc)
                continue
            if ok:
                self.titles.append(title)
                newly.append(title)
                logger.info("lobster %s 获得称号 %s", self.name, title)
        return newly
