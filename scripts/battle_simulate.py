"""战斗引擎数值校准脚本：构造预设 build 互打 N 场，看胜率。

目标：
- 同 build 镜像对决：胜率应在 45-55%（理想 50%）
- 弱克制（速度克力量 / 力量克肉盾 / 肉盾克速度）：克制方胜率 55-65%
- 高级 vs 低级（差 5 级）：高级胜率应 >= 70%
- 同级 + 装备齐 vs 同级 + 裸装：装备方胜率应 >= 60%

用法：
    python scripts/battle_simulate.py [--rounds 1000]

不写 fallback：脚本崩了就崩，把 traceback 打出来。
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import battle as bt  # noqa: E402
from app.core.lobster import Lobster  # noqa: E402

# 模拟不需要噪声日志
logging.basicConfig(level=logging.WARNING)


# ============ 预设 build ============


def _bare_lobster(name: str, level: int = 5) -> Lobster:
    """裸装基础龙虾（平均属性，无技能、无装备）。"""
    return Lobster(
        user_id=f"sim-{name}",
        name=name,
        breed="模拟虾",
        personality="模拟",
        level=level,
        claw=5 + level - 1,
        shell=5 + level - 1,
        speed=5 + level - 1,
        stamina=5 + level - 1,
        luck=5,
    )


def power_build(name: str, level: int = 5) -> Lobster:
    """力量流：堆钳力 + 力量装备 + 力量技能。"""
    l = _bare_lobster(name, level)
    l.claw += 4
    l.stamina += 1
    l.skills = ["蒜蓉觉醒", "水产之怒", "麻辣反伤"]
    l.skill_levels = {"蒜蓉觉醒": 2, "水产之怒": 1, "麻辣反伤": 1}
    l.equipped = {
        "主钳": "rusty_pliers" if level >= 5 else "toothpick_spear",
        "副钳": "crab_pincer" if level >= 3 else "chopstick_buckler",
        "背甲": "mahjong_tile_back" if level >= 4 else "plastic_cape",
        "鞋": "cotton_socks",
    }
    return l


def speed_build(name: str, level: int = 5) -> Lobster:
    """速度流：堆速度 + 速度装备 + 速度技能。"""
    l = _bare_lobster(name, level)
    l.speed += 4
    l.luck += 2
    l.skills = ["横着走", "夜场气场", "夜市传说"]
    l.skill_levels = {"横着走": 2, "夜场气场": 1, "夜市传说": 1}
    l.equipped = {
        "主钳": "beer_cap_blade",
        "副钳": "silver_needle" if level >= 4 else "chopstick_buckler",
        "背甲": "plastic_cape",
        "鞋": "nike_zoom" if level >= 5 else "cotton_socks",
    }
    return l


def tank_build(name: str, level: int = 5) -> Lobster:
    """肉盾流：堆耐力/壳硬 + 防御装备 + 肉盾技能。"""
    l = _bare_lobster(name, level)
    l.shell += 4
    l.stamina += 3
    l.skills = ["椒盐护体", "锅气护体", "断钳重生"]
    l.skill_levels = {"椒盐护体": 2, "锅气护体": 1, "断钳重生": 1}
    l.equipped = {
        "主钳": "toothpick_spear",
        "副钳": "chopstick_buckler",
        "背甲": "tin_carapace" if level >= 7 else "plastic_cape",
        "鞋": "lead_boots" if level >= 4 else "cotton_socks",
    }
    return l


# ============ 模拟核心 ============


def _reset_for_battle(l: Lobster) -> None:
    """每场战斗前清状态（避免 simulate 把上一场结果带过来）。"""
    l.morale = 70
    l.wins = 0
    l.losses = 0
    l.win_streak = 0
    l.lose_streak = 0
    l.exp = 0
    l.coins = 9999  # 避免触发奖励溢出导致升级
    l.fame = 0


def run_matchup(
    build_a: Any, build_b: Any, rounds: int, label: str,
) -> Dict[str, Any]:
    wins_a = 0
    wins_b = 0
    rounds_log: List[int] = []
    for i in range(rounds):
        a = build_a(f"A{i}", level=5)
        b = build_b(f"B{i}", level=5)
        _reset_for_battle(a)
        _reset_for_battle(b)
        result = bt.simulate(a, b)
        if result.winner is a:
            wins_a += 1
        else:
            wins_b += 1
        # 计算回合数（粗略从战报里数 "▸ 第" 出现次数 / 2，作为参考）
        rounds_log.append(result.narration.count("▸ 第"))

    pct = wins_a / rounds * 100
    avg_rounds = sum(rounds_log) / max(1, len(rounds_log))
    return {
        "label": label,
        "wins_a": wins_a,
        "wins_b": wins_b,
        "win_rate_a": round(pct, 1),
        "avg_actions": round(avg_rounds, 1),
    }


def run_level_matchup(
    build: Any, level_a: int, level_b: int, rounds: int, label: str,
) -> Dict[str, Any]:
    """同 build 不同等级（验证等级压制）。"""
    wins_a = 0
    for i in range(rounds):
        a = build(f"H{i}", level=level_a)
        b = build(f"L{i}", level=level_b)
        _reset_for_battle(a)
        _reset_for_battle(b)
        result = bt.simulate(a, b)
        if result.winner is a:
            wins_a += 1
    pct = wins_a / rounds * 100
    return {
        "label": label,
        "wins_high": wins_a,
        "wins_low": rounds - wins_a,
        "win_rate_high": round(pct, 1),
    }


def run_naked_matchup(
    build: Any, rounds: int, label: str,
) -> Dict[str, Any]:
    """带装备的 build vs 裸装相同等级 + 相同属性（验证装备/技能价值）。"""
    wins_geared = 0
    for i in range(rounds):
        a = build(f"G{i}", level=5)
        b = _bare_lobster(f"N{i}", level=5)
        b.skills = []
        b.skill_levels = {}
        b.equipped = {}
        _reset_for_battle(a)
        _reset_for_battle(b)
        result = bt.simulate(a, b)
        if result.winner is a:
            wins_geared += 1
    pct = wins_geared / rounds * 100
    return {
        "label": label,
        "wins_geared": wins_geared,
        "win_rate_geared": round(pct, 1),
    }


# ============ 主流程 ============


def _judge(actual: float, lo: float, hi: float) -> str:
    if lo <= actual <= hi:
        return "✅"
    if actual < lo:
        return f"⚠️ 偏低（期望 {lo}-{hi}%）"
    return f"⚠️ 偏高（期望 {lo}-{hi}%）"


def main() -> None:
    parser = argparse.ArgumentParser(description="战斗引擎数值校准")
    parser.add_argument("--rounds", type=int, default=300, help="每个对决跑多少场（默认 300）")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)
    print(f"\n🦞 战斗引擎数值校准（每场 {args.rounds} 局，seed={args.seed}）")
    print("=" * 72)

    rows: List[Tuple[str, Any]] = []

    # 1. 同 build 镜像（应 45-55%）
    print("\n【镜像对决】（理想胜率 50%, 容忍 45-55%）")
    for build, name in [(power_build, "力量"), (speed_build, "速度"), (tank_build, "肉盾")]:
        r = run_matchup(build, build, args.rounds, f"{name} vs {name}")
        verdict = _judge(r["win_rate_a"], 45, 55)
        print(f"  {r['label']:<14} A:{r['wins_a']:>3} 胜  B:{r['wins_b']:>3} 胜  "
              f"A胜率 {r['win_rate_a']:.1f}%  平均出手 {r['avg_actions']}  {verdict}")
        rows.append((f"镜像 {name}", r))

    # 2. 弱克制（克制方期望 55-65%）
    print("\n【弱克制】（理想：克制方 55-65%；ABABA 顺序：A=速度 B=力量；速克力 → 速度胜率高）")
    matchups = [
        (speed_build, power_build, "速度 vs 力量"),
        (power_build, tank_build, "力量 vs 肉盾"),
        (tank_build, speed_build, "肉盾 vs 速度"),
    ]
    for ba, bb, lbl in matchups:
        r = run_matchup(ba, bb, args.rounds, lbl)
        verdict = _judge(r["win_rate_a"], 55, 65)
        print(f"  {r['label']:<14} A:{r['wins_a']:>3}  B:{r['wins_b']:>3}  "
              f"A胜率 {r['win_rate_a']:.1f}%  平均出手 {r['avg_actions']}  {verdict}")
        rows.append((lbl, r))

    # 3. 等级压制（差 5 级，高级期望 >= 70%）
    print("\n【等级压制】（Lv.10 vs Lv.5，高级期望 >= 70%）")
    for build, name in [(power_build, "力量"), (speed_build, "速度"), (tank_build, "肉盾")]:
        r = run_level_matchup(build, 10, 5, args.rounds, f"{name} Lv10 vs Lv5")
        verdict = "✅" if r["win_rate_high"] >= 70 else f"⚠️ 偏低（{r['win_rate_high']}%）"
        print(f"  {r['label']:<22} 高级胜率 {r['win_rate_high']:.1f}%  {verdict}")
        rows.append((r["label"], r))

    # 4. 有装备 vs 无装备（同级，装备方期望 >= 60%）
    print("\n【装备价值】（带 build vs 同级裸虾，期望 >= 60%）")
    for build, name in [(power_build, "力量"), (speed_build, "速度"), (tank_build, "肉盾")]:
        r = run_naked_matchup(build, args.rounds, f"{name} build vs 裸虾")
        verdict = "✅" if r["win_rate_geared"] >= 60 else f"⚠️ 偏低（{r['win_rate_geared']}%）"
        print(f"  {r['label']:<22} 装备方胜率 {r['win_rate_geared']:.1f}%  {verdict}")
        rows.append((r["label"], r))

    print("\n" + "=" * 72)
    print("✅ = 数值在容忍范围内；⚠️ = 需要调参或被忍痛接受")
    print("=" * 72 + "\n")


if __name__ == "__main__":
    main()
