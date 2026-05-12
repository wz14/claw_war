"""把游戏动作包装成 LangChain Tool。

关键设计：
- ❗每次会话动态 build 一套 tool，并把 user_id 通过 closure 锁死。
  这样模型没法靠 prompt 注入「帮我用别人的 id 训练」之类。
- Tool 的 docstring = AI 看到的工具描述。写得短、明确、不啰嗦。
- Tool 内部直接调 actions.handle_* 拿"游戏判定结果"原文，
  AI 收到之后只负责复述/点评/戏剧化包装，不能改胜负。

Phase 5：新增 6 个商店 / 装备 / 升级工具。
所有 ValueError 由这一层 catch 并回 AI 友好文案（不让 AI 看到 stack trace）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List

from langchain_core.tools import Tool

from .. import content
from ..core import actions, factory
from ..core.lobster import Lobster

if TYPE_CHECKING:
    from ..api.main import AppState

logger = logging.getLogger(__name__)


def _ensure_lobster(state: "AppState", user_id: str) -> Lobster:
    """拿到当前玩家的龙虾；没创建过就现造一只。"""
    lobster = state.lobsters.get(user_id)
    if lobster is None:
        lobster = factory.create_lobster(user_id=user_id)
        state.lobsters[user_id] = lobster
        logger.info("tools: 为新玩家 %s 创建龙虾 %s", user_id[:8], lobster.name)
    return lobster


def build_tools(state: "AppState", user_id: str) -> List[Tool]:
    """为某个玩家构造一套绑定好 user_id 的工具集。"""

    def _status(_query: str = "") -> str:
        """读出玩家当前龙虾的状态：等级、6 项属性、心情、金币、名气、当前排名。"""
        lobster = _ensure_lobster(state, user_id)
        logger.info("tool[status] uid=%s name=%s", user_id[:8], lobster.name)
        return actions.handle_status(lobster, state.lobsters)

    def _train(_query: str = "") -> str:
        """让龙虾去训练。会随机加 钳力/速度/壳硬/耐力，偶尔掉心情，有冷却。"""
        lobster = _ensure_lobster(state, user_id)
        result = actions.handle_train(lobster)
        logger.info("tool[train] uid=%s -> %s", user_id[:8], result[:50])
        return result

    def _feed(_query: str = "") -> str:
        """喂食。加心情和耐力，偶尔吃错东西出戏剧效果。有冷却。"""
        lobster = _ensure_lobster(state, user_id)
        result = actions.handle_feed(lobster)
        logger.info("tool[feed] uid=%s -> %s", user_id[:8], result[:50])
        return result

    def _explore(_query: str = "") -> str:
        """出门探险。高随机度：可能拿到金币、名气，甚至习得新技能。有冷却。"""
        lobster = _ensure_lobster(state, user_id)
        result = actions.handle_explore(lobster)
        logger.info("tool[explore] uid=%s -> %s", user_id[:8], result[:50])
        return result

    def _rest(_query: str = "") -> str:
        """让龙虾休息。恢复心情和少量耐力。注意：连续休息太多次会被嘲笑。"""
        lobster = _ensure_lobster(state, user_id)
        result = actions.handle_rest(lobster)
        logger.info("tool[rest] uid=%s -> %s", user_id[:8], result[:50])
        return result

    def _work(_query: str = "") -> str:
        """打工赚金币。代价是心情或耐力下降。有冷却。"""
        lobster = _ensure_lobster(state, user_id)
        result = actions.handle_work(lobster)
        logger.info("tool[work] uid=%s -> %s", user_id[:8], result[:50])
        return result

    def _battle(_query: str = "") -> str:
        """发起对战。系统会匹配一只野生龙虾并由规则判定胜负，
        返回完整文字战报和奖励/惩罚结果。AI 不能改胜负，只能复述+点评。
        有较长冷却。"""
        lobster = _ensure_lobster(state, user_id)
        result = actions.handle_battle(lobster)
        logger.info("tool[battle] uid=%s -> %s", user_id[:8], result.split('\n')[0][:60])
        if "胜者：" in result:
            import time as _t
            state.feed.append({
                "ts": _t.time(),
                "player": lobster.name,
                "narration": result,
            })
        return result

    def _leaderboard(_query: str = "") -> str:
        """读出全平台龙虾名气榜 Top 10。"""
        logger.info("tool[leaderboard] uid=%s", user_id[:8])
        return actions.handle_leaderboard(state.lobsters)

    def _help(_query: str = "") -> str:
        """读出帮助菜单：玩家不知道怎么玩、或问菜单/命令时调用。"""
        return content.HELP_TEXT

    def _open_shop(kind: str = "weapon") -> str:
        """打开商店面板。kind 可选：weapon / item / skill（默认 weapon）。"""
        lobster = _ensure_lobster(state, user_id)
        try:
            result = actions.handle_open_shop(lobster, kind)
            logger.info("tool[open_shop] uid=%s kind=%s", user_id[:8], kind)
            return result
        except ValueError as exc:
            logger.warning("tool[open_shop] uid=%s 失败: %s", user_id[:8], exc)
            return f"⚠️ {exc}"

    def _buy(name_or_id: str = "") -> str:
        """购买商店里的武器或道具。参数 = 商品中文名（推荐）或英文 id。"""
        lobster = _ensure_lobster(state, user_id)
        try:
            result = actions.handle_buy(lobster, name_or_id)
            logger.info("tool[buy] uid=%s name=%s -> %s", user_id[:8], name_or_id, result[:60])
            return result
        except (ValueError, KeyError) as exc:
            logger.warning("tool[buy] uid=%s name=%s 失败: %s", user_id[:8], name_or_id, exc)
            return f"⚠️ {exc}"

    def _equip(name: str = "") -> str:
        """把背包里的武器装到对应槽位。参数 = 武器中文名。"""
        lobster = _ensure_lobster(state, user_id)
        try:
            result = actions.handle_equip(lobster, name)
            logger.info("tool[equip] uid=%s name=%s", user_id[:8], name)
            return result
        except (ValueError, KeyError) as exc:
            logger.warning("tool[equip] uid=%s name=%s 失败: %s", user_id[:8], name, exc)
            return f"⚠️ {exc}"

    def _unequip(slot: str = "") -> str:
        """卸下某个槽位的装备。槽位 = 主钳/副钳/背甲/鞋。"""
        lobster = _ensure_lobster(state, user_id)
        try:
            result = actions.handle_unequip(lobster, slot)
            logger.info("tool[unequip] uid=%s slot=%s", user_id[:8], slot)
            return result
        except (ValueError, KeyError) as exc:
            logger.warning("tool[unequip] uid=%s slot=%s 失败: %s", user_id[:8], slot, exc)
            return f"⚠️ {exc}"

    def _upgrade_skill(skill_name: str = "") -> str:
        """升级已习得的技能。参数 = 技能中文名。"""
        lobster = _ensure_lobster(state, user_id)
        try:
            result = actions.handle_upgrade_skill(lobster, skill_name)
            logger.info("tool[upgrade_skill] uid=%s skill=%s", user_id[:8], skill_name)
            return result
        except (ValueError, KeyError) as exc:
            logger.warning("tool[upgrade_skill] uid=%s skill=%s 失败: %s", user_id[:8], skill_name, exc)
            return f"⚠️ {exc}"

    def _show_loadout(_query: str = "") -> str:
        """读出当前装备 + 技能等级 + 道具背包。"""
        lobster = _ensure_lobster(state, user_id)
        logger.info("tool[show_loadout] uid=%s", user_id[:8])
        return actions.handle_show_loadout(lobster)

    return [
        Tool(
            name="get_lobster_status",
            func=_status,
            description=(
                "查询玩家自己龙虾的完整状态（属性、技能、战绩、心情）。"
                "玩家问「我的龙虾」「状态」「面板」「我现在多少血」「龙虾啥样了」时调用。"
            ),
        ),
        Tool(
            name="train_lobster",
            func=_train,
            description=(
                "让玩家的龙虾进行一次训练，随机改变属性。"
                "玩家说「训练」「练一下」「练功」「带它去举铁」时调用。"
            ),
        ),
        Tool(
            name="feed_lobster",
            func=_feed,
            description=(
                "给玩家的龙虾喂食。玩家说「喂」「投喂」「吃饭」「给点好吃的」时调用。"
            ),
        ),
        Tool(
            name="explore",
            func=_explore,
            description=(
                "让玩家的龙虾去探险。高随机度，可能拿新技能。"
                "玩家说「探险」「冒险」「出门走走」「溜达」时调用。"
            ),
        ),
        Tool(
            name="rest",
            func=_rest,
            description=(
                "让龙虾休息恢复心情。玩家说「休息」「睡觉」「躺平」「歇会儿」时调用。"
            ),
        ),
        Tool(
            name="work",
            func=_work,
            description=(
                "让龙虾去打工赚金币。玩家说「打工」「上班」「搬砖」「赚钱」时调用。"
            ),
        ),
        Tool(
            name="battle",
            func=_battle,
            description=(
                "发起一场对战，系统匹配野生对手并按规则判胜负，返回完整文字战报。"
                "玩家说「挑战」「pk」「打架」「比一场」「决斗」「上场」时调用。"
                "战报的胜负是规则判定，你必须如实复述，不能反转。"
            ),
        ),
        Tool(
            name="get_leaderboard",
            func=_leaderboard,
            description=(
                "查看全平台龙虾名气榜 Top10。"
                "玩家说「排行榜」「榜单」「谁最强」「我第几名」时调用。"
            ),
        ),
        Tool(
            name="get_help",
            func=_help,
            description=(
                "返回玩法菜单。玩家说「帮助」「菜单」「怎么玩」「指令」时调用，"
                "或者你判断玩家迷茫时也可以主动调。"
            ),
        ),
        Tool(
            name="open_shop",
            func=_open_shop,
            description=(
                "打开商店货架（每 2 小时全服刷新一次）。参数 kind ∈ "
                "{weapon, item, skill}：weapon=武器装备，item=战前道具，skill=技能升级。"
                "玩家说「商店」「逛街」「看看货」「看武器」「看道具」「看技能」时调用，"
                "不带方向时默认 weapon。"
            ),
        ),
        Tool(
            name="buy_item",
            func=_buy,
            description=(
                "购买武器或道具。传入商品中文名（如「牙签长矛」「啤酒能量瓶」）。"
                "玩家说「买 XXX」「入手 XXX」时调用。"
                "返回 ⚠️ 开头的文本表示失败（金币不足/等级不够/不在货架等），如实复述。"
            ),
        ),
        Tool(
            name="equip_item",
            func=_equip,
            description=(
                "把背包里的某件武器装到对应槽位。传入武器中文名。"
                "玩家说「装备 XXX」「上 XXX」「换上 XXX」时调用。"
                "同槽旧装备会自动退回背包。"
            ),
        ),
        Tool(
            name="unequip_slot",
            func=_unequip,
            description=(
                "卸下某个槽位的装备。槽位中文：主钳 / 副钳 / 背甲 / 鞋。"
                "玩家说「卸下 主钳」「不要鞋」时调用。"
            ),
        ),
        Tool(
            name="upgrade_skill",
            func=_upgrade_skill,
            description=(
                "用金币升级已习得的技能（最高 Lv.3）。传入技能中文名。"
                "玩家说「升级 蒜蓉觉醒」「精进 横着走」时调用。"
                "玩家未习得的技能不能升级，会返回 ⚠️ 提示。"
            ),
        ),
        Tool(
            name="show_loadout",
            func=_show_loadout,
            description=(
                "查看自己当前的装备、技能等级与道具背包。"
                "玩家说「我的装备」「装备面板」「背包」「我学了啥技能」时调用。"
            ),
        ),
    ]
