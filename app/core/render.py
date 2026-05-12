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
from urllib.parse import quote

from .. import content
from . import shop

if TYPE_CHECKING:
    from .lobster import Lobster

logger = logging.getLogger(__name__)


# 分享链接 = Railway 生产环境地址
# 真正的常量定义在 app.content.SHARE_URL，这里只是 re-export 给 render 内部用
SHARE_URL = content.SHARE_URL

# 玩家 card 的视觉分隔线。用 ━ 而非 - / = 是因为微信里 ━ 显示宽度更稳
DIVIDER = "━━━━━━━━━━━━━━━━"


def battle_history_url(user_id: str) -> str:
    """生成玩家个人战报详情页 URL（Phase 6 战报页）。

    user_id 里可能含 `@` `.` 等 query 边界字符，统一 quote 一遍最稳；
    SHARE_URL 末尾可能有 `/`，rstrip 一下避免 //battles 这种丑写法。
    """
    encoded = quote(user_id, safe="")
    return f"{SHARE_URL.rstrip('/')}/battles?user_id={encoded}"


def compute_rank(lobster: "Lobster", all_lobsters: Dict[str, "Lobster"]) -> int:
    """按 (fame, wins, level) 倒序计算 lobster 在全榜中的排名（1-based）。

    Phase 3 决策：人机龙虾（is_bot=True）和真人玩家**同样参与排名**，
    玩家视角看不出 bot / 真人区别。这样 leaderboard 也热闹、新人有目标可追。
    如果 lobster 不在 all_lobsters 里（兜底防御），返回总数 + 1。
    """
    sorted_list = sorted(
        all_lobsters.values(),
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
    return len(sorted_list) + 1


def render_player_card(lobster: "Lobster", all_lobsters: Dict[str, "Lobster"]) -> str:
    """渲染 Phase 2 排版规约定义的玩家页脚 + Phase 5 流派/装备摘要。

    格式（每条消息末尾都附这一段）：
        ━━━━━━━━━━━━━━━━
        🦞 蒜蓉暴君  Lv.4
        ━━━━━━━━━━━━━━━━
        🥊 钳 7  🛡 壳 5  💨 速 6
        🔋 耐 6  🍀 运 8  ❤️ 心情 良好

        🧬 流派：力量×2 速度×1（×2 协同）
        🎒 金币 28  ⭐ 名气 7  📊 排名 #5
        🔗 我的战报：https://claw-war-production.up.railway.app/battles?user_id=xxxx

    流派行：
    - 综合"已习得技能 + 当前装备"统计；玩家没装备/没技能时不显示这行
    - ×2 / ×3 协同会在末尾用括号标注，引导玩家继续往同流派堆
    """
    rank = compute_rank(lobster, all_lobsters)
    dist = shop.faction_distribution(lobster)
    syn_school, syn_tier = shop.synergy_tier(dist)
    has_anything = any(v > 0 for v in dist.values())

    lines = [
        DIVIDER,
        f"🦞 {lobster.name}  Lv.{lobster.level}",
        DIVIDER,
        f"🥊 钳 {lobster.claw}  🛡 壳 {lobster.shell}  💨 速 {lobster.speed}",
        f"🔋 耐 {lobster.stamina}  🍀 运 {lobster.luck}  "
        f"❤️ 心情 {lobster.morale_label_short()}",
        "",
    ]
    if has_anything:
        syn_tag = f"（{syn_school}×{syn_tier} 协同）" if syn_tier >= 2 else ""
        lines.append(f"🧬 流派：{shop.faction_short_label(dist)}{syn_tag}")
    lines.append(f"🎒 金币 {lobster.coins}  ⭐ 名气 {lobster.fame}  📊 排名 #{rank}")
    # Phase 6：玩家 card 末尾从全站分享链接改成"专属战报详情页"，
    # 点进去就能看到这只龙虾完整的多回合战报历史
    lines.append(f"🔗 我的战报：{battle_history_url(lobster.user_id)}")
    return "\n".join(lines)


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
