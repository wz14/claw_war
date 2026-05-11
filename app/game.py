"""龙虾游戏核心数据结构 + 养成行为。

设计点：
- 一只龙虾对应一个 user_id（来自微信 ilink_user_id）
- 所有属性都是普通整数，便于持久化和判定
- 行为方法返回 (描述文本, 属性变化 dict)，方便上层拼接消息
- 不写隐式 fallback——非法操作直接抛
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from . import content

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

    user_id: str              # 微信 ilink_user_id
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

    # ===== 序列化 =====

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Lobster":
        return cls(**data)

    # ===== 衍生属性 =====

    def morale_label(self) -> str:
        if self.morale >= 85:
            return "亢奋（你是不是又给它喝啤酒了）"
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
        skills = "、".join(self.skills) if self.skills else "无（空有一身肌肉）"
        titles = "、".join(self.titles) if self.titles else "无（还没整出花样）"
        return (
            f"【{self.name}】Lv.{self.level} · {self.stage()}\n"
            f"品种：{self.breed}\n"
            f"性格：{self.personality}\n"
            f"战绩：{self.wins}胜{self.losses}负"
            f"{' · 连胜' + str(self.win_streak) if self.win_streak >= 2 else ''}"
            f"{' · 连败' + str(self.lose_streak) if self.lose_streak >= 2 else ''}\n"
            f"———\n"
            f"钳力 {self.claw}   壳硬 {self.shell}\n"
            f"速度 {self.speed}   耐力 {self.stamina}\n"
            f"运气 {self.luck}   心情 {self.morale_label()} ({self.morale})\n"
            f"金币 {self.coins}   名气 {self.fame}\n"
            f"经验 {self.exp}\n"
            f"———\n"
            f"技能：{skills}\n"
            f"称号：{titles}"
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
        # 升级随机加 1-2 点核心属性
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


# ============ 工厂方法 ============


def random_name() -> str:
    return random.choice(content.NAME_PREFIXES) + random.choice(content.NAME_SUFFIXES)


def create_lobster(user_id: str, name: Optional[str] = None) -> Lobster:
    """根据 user_id 生成一只随机龙虾。"""
    final_name = name or random_name()
    skills = random.sample(content.INITIAL_SKILLS, k=2)
    # 起手属性轻度随机化
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
    """造一只野生对手，与玩家等级接近。"""
    # 等级波动 -1 ~ +2
    opp_level = max(1, player_level + random.randint(-1, 2))
    opp = create_lobster(user_id=f"wild-{random.randint(10000, 99999)}")
    opp.level = opp_level
    # 等级越高数值越好
    boost = (opp_level - 1) * 1
    opp.claw += boost
    opp.shell += boost
    opp.speed += boost
    opp.stamina += boost
    opp._clamp()
    return opp
