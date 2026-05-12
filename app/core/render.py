"""微信展示层渲染：player_card 页脚、分享链接、排名计算。

设计思路（Phase 2 排版规约）：
- 微信不渲染 Markdown，全靠符号 + emoji 做轻量分块
- 玩家收到的"每条 AI 回复"末尾都会被附上一段统一的 player_card：
  名字 / 等级 / 6 项核心属性 / 心情 / 金币 / 名气 / **当前名气排名** / **分享链接**
- 排版规约里**不**含技能 / 称号 / 战绩（避免每条消息太长，
  这些信息走 explore / battle 时会实时显示）
- player_card 既是"我的龙虾"命令的返回值，也是每条消息的页脚

排名计算：
- 只统计 is_bot=False 的真人玩家，避免 Phase 3 加入人机后污染排名
- 排序键 (fame DESC, wins DESC, level DESC) 与 /api/leaderboard 保持一致
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict

from .. import content

if TYPE_CHECKING:
    from .lobster import Lobster

logger = logging.getLogger(__name__)


# 分享链接 = Railway 生产环境地址
# 真正的常量定义在 app.content.SHARE_URL，这里只是 re-export 给 render 内部用
SHARE_URL = content.SHARE_URL

# 玩家 card 的视觉分隔线。用 ━ 而非 - / = 是因为微信里 ━ 显示宽度更稳
DIVIDER = "━━━━━━━━━━━━━━━━"


def compute_rank(lobster: "Lobster", all_lobsters: Dict[str, "Lobster"]) -> int:
    """按 (fame, wins, level) 倒序计算 lobster 在真人玩家中的排名。

    返回 1-based 排名。如果 lobster 不在 all_lobsters 里（理论上不应该，
    但兜底防御），返回真人总数 + 1。
    """
    real_lobsters = [l for l in all_lobsters.values() if not l.is_bot]
    sorted_list = sorted(
        real_lobsters,
        key=lambda l: (l.fame, l.wins, l.level),
        reverse=True,
    )
    for idx, l in enumerate(sorted_list, start=1):
        if l.user_id == lobster.user_id:
            return idx
    logger.warning(
        "compute_rank: lobster %s 不在 all_lobsters 里，返回兜底 rank",
        lobster.user_id[:8],
    )
    return len(real_lobsters) + 1


def render_player_card(lobster: "Lobster", all_lobsters: Dict[str, "Lobster"]) -> str:
    """渲染 Phase 2 排版规约定义的玩家页脚。

    格式（每条消息末尾都附这一段）：
        ━━━━━━━━━━━━━━━━
        🦞 蒜蓉暴君  Lv.4
        ━━━━━━━━━━━━━━━━
        🥊 钳 7  🛡 壳 5  💨 速 6
        🔋 耐 6  🍀 运 8  ❤️ 心情 良好

        🎒 金币 28  ⭐ 名气 7  📊 排名 #5
        🔗 https://claw-war-production.up.railway.app/
    """
    rank = compute_rank(lobster, all_lobsters)
    return (
        f"{DIVIDER}\n"
        f"🦞 {lobster.name}  Lv.{lobster.level}\n"
        f"{DIVIDER}\n"
        f"🥊 钳 {lobster.claw}  🛡 壳 {lobster.shell}  💨 速 {lobster.speed}\n"
        f"🔋 耐 {lobster.stamina}  🍀 运 {lobster.luck}  "
        f"❤️ 心情 {lobster.morale_label_short()}\n"
        f"\n"
        f"🎒 金币 {lobster.coins}  ⭐ 名气 {lobster.fame}  📊 排名 #{rank}\n"
        f"🔗 {SHARE_URL}"
    )


def render_share_line() -> str:
    """单行分享链接，用于欢迎语等已含详细 stats 的消息末尾。

    避免欢迎语 + 完整 player_card 重复展示属性。
    """
    return f"🔗 {SHARE_URL}"


def append_footer(message: str, lobster: "Lobster", all_lobsters: Dict[str, "Lobster"]) -> str:
    """在已有消息末尾拼接 player_card 页脚。

    用 \n\n 分隔，让 player_card 视觉上和上文消息明确分块。
    """
    if not message:
        return render_player_card(lobster, all_lobsters)
    return f"{message.rstrip()}\n\n{render_player_card(lobster, all_lobsters)}"
