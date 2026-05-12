"""把游戏动作包装成 LangChain Tool。

关键设计：
- ❗每次会话动态 build 一套 tool，并把 user_id 通过 closure 锁死。
  这样模型没法靠 prompt 注入「帮我用别人的 id 训练」之类。
- Tool 的 docstring = AI 看到的工具描述。写得短、明确、不啰嗦。
- Tool 内部直接调 actions.handle_* 拿"游戏判定结果"原文，
  AI 收到之后只负责复述/点评/戏剧化包装，不能改胜负。

Phase 5：新增 6 个商店 / 装备 / 升级工具。
所有 ValueError 由这一层 catch 并回 AI 友好文案（不让 AI 看到 stack trace）。

历史问题修复（2026-05）：
- 之前用 langchain_core.tools.Tool（single-input 工具），新模型
  （deepseek-v4-flash 等）以 OpenAI tools schema 调用时会塞 `arguments={}`，
  被 LangChain 解包成 `Args: []` → 直接抛
  `Too many arguments to single-input tool`。
- 全部切换到 StructuredTool.from_function，会按函数签名自动生成 args schema，
  无参工具就是空 schema，新模型再传空 args 也不会报错。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List

from langchain_core.tools import StructuredTool

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


def build_tools(state: "AppState", user_id: str) -> List[StructuredTool]:
    """为某个玩家构造一套绑定好 user_id 的工具集。"""

    def _status() -> str:
        """读出玩家当前龙虾的状态：等级、6 项属性、心情、金币、名气、当前排名。"""
        lobster = _ensure_lobster(state, user_id)
        logger.info("tool[status] uid=%s name=%s", user_id[:8], lobster.name)
        return actions.handle_status(lobster, state.lobsters)

    def _train() -> str:
        """让龙虾去训练。会随机加 钳力/速度/壳硬/耐力，偶尔掉心情，有冷却。"""
        lobster = _ensure_lobster(state, user_id)
        result = actions.handle_train(lobster)
        logger.info("tool[train] uid=%s -> %s", user_id[:8], result[:50])
        return result

    def _feed() -> str:
        """喂食。加心情和耐力，偶尔吃错东西出戏剧效果。有冷却。"""
        lobster = _ensure_lobster(state, user_id)
        result = actions.handle_feed(lobster)
        logger.info("tool[feed] uid=%s -> %s", user_id[:8], result[:50])
        return result

    def _explore() -> str:
        """出门探险。高随机度：可能拿到金币、名气，甚至习得新技能。有冷却。"""
        lobster = _ensure_lobster(state, user_id)
        result = actions.handle_explore(lobster)
        logger.info("tool[explore] uid=%s -> %s", user_id[:8], result[:50])
        return result

    def _rest() -> str:
        """让龙虾休息。恢复心情和少量耐力。注意：连续休息太多次会被嘲笑。"""
        lobster = _ensure_lobster(state, user_id)
        result = actions.handle_rest(lobster)
        logger.info("tool[rest] uid=%s -> %s", user_id[:8], result[:50])
        return result

    def _work() -> str:
        """打工赚金币。代价是心情或耐力下降。有冷却。"""
        lobster = _ensure_lobster(state, user_id)
        result = actions.handle_work(lobster)
        logger.info("tool[work] uid=%s -> %s", user_id[:8], result[:50])
        return result

    def _battle() -> str:
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

    def _leaderboard() -> str:
        """读出全平台龙虾名气榜 Top 10。"""
        logger.info("tool[leaderboard] uid=%s", user_id[:8])
        return actions.handle_leaderboard(state.lobsters)

    def _help() -> str:
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

    def _show_loadout() -> str:
        """读出当前装备 + 技能等级 + 道具背包。"""
        lobster = _ensure_lobster(state, user_id)
        logger.info("tool[show_loadout] uid=%s", user_id[:8])
        return actions.handle_show_loadout(lobster)

    def _battle_history(limit_str: str = "") -> str:
        """读出自己最近 N 场战绩（默认 5 场）。仅微信侧精简文本，详情走前端 web。"""
        lobster = _ensure_lobster(state, user_id)
        limit = 5
        try:
            if limit_str and limit_str.strip():
                limit = int(limit_str.strip())
        except ValueError:
            logger.warning("tool[battle_history] uid=%s 非法 limit=%r 用默认 5", user_id[:8], limit_str)
        result = actions.handle_battle_history(lobster, limit)
        logger.info("tool[battle_history] uid=%s limit=%d", user_id[:8], limit)
        return result

    def _query_other(name: str = "") -> str:
        """查别人的龙虾公开信息（属性/技能/战绩/流派）。参数 = 对方龙虾名字。"""
        try:
            result = actions.handle_query_lobster(state.lobsters, name)
            logger.info("tool[query_other] uid=%s name=%s", user_id[:8], name)
            return result
        except ValueError as exc:
            logger.warning("tool[query_other] uid=%s name=%s 失败: %s", user_id[:8], name, exc)
            return f"⚠️ {exc}"

    # PvP 三个工具的 feed 入栈在 pvp.execute_pvp 里完成，
    # 这里只做 ValueError → ⚠️ 文案 + 日志的 wrapper。
    def _pvp_random() -> str:
        """随机 PvP：从在线真人池抽对手；没真人时降级到普通 bot。"""
        lobster = _ensure_lobster(state, user_id)
        try:
            result = actions.handle_pvp_random(state, lobster)
            logger.info(
                "tool[pvp_random] uid=%s -> %s",
                user_id[:8], result.split("\n", 1)[0][:60],
            )
            return result
        except ValueError as exc:
            logger.warning("tool[pvp_random] uid=%s 失败: %s", user_id[:8], exc)
            return f"⚠️ {exc}"

    def _pvp_specific(target_name: str = "") -> str:
        """指定真人/bot 名字 PvP（不能挑 boss，boss 走 challenge_boss）。参数 = 对方名字。"""
        lobster = _ensure_lobster(state, user_id)
        try:
            result = actions.handle_pvp_specific(state, lobster, target_name)
            logger.info(
                "tool[pvp_specific] uid=%s target=%s -> %s",
                user_id[:8], target_name, result.split("\n", 1)[0][:60],
            )
            return result
        except ValueError as exc:
            logger.warning(
                "tool[pvp_specific] uid=%s target=%s 失败: %s",
                user_id[:8], target_name, exc,
            )
            return f"⚠️ {exc}"

    def _pvp_boss(boss_name: str = "") -> str:
        """挑战预设 BOSS 龙虾。参数 = boss 中文名（如「不锈钢魔王」「霓虹夜行者」「蒜蓉帝王」）。"""
        lobster = _ensure_lobster(state, user_id)
        try:
            result = actions.handle_pvp_boss(state, lobster, boss_name)
            logger.info(
                "tool[pvp_boss] uid=%s boss=%s -> %s",
                user_id[:8], boss_name, result.split("\n", 1)[0][:60],
            )
            return result
        except ValueError as exc:
            logger.warning(
                "tool[pvp_boss] uid=%s boss=%s 失败: %s",
                user_id[:8], boss_name, exc,
            )
            return f"⚠️ {exc}"

    def _list_players() -> str:
        """读出当前可挑战的真人 / boss 名单。"""
        try:
            result = actions.handle_list_active_players(state, user_id)
            logger.info("tool[list_players] uid=%s", user_id[:8])
            return result
        except Exception as exc:
            logger.warning("tool[list_players] uid=%s 失败: %s", user_id[:8], exc)
            return f"⚠️ {exc}"

    return [
        StructuredTool.from_function(
            name="get_lobster_status",
            func=_status,
            description=(
                "查询玩家自己龙虾的完整状态（属性、技能、战绩、心情）。"
                "玩家问「我的龙虾」「状态」「面板」「我现在多少血」「龙虾啥样了」时调用。"
            ),
        ),
        StructuredTool.from_function(
            name="train_lobster",
            func=_train,
            description=(
                "让玩家的龙虾进行一次训练，随机改变属性。"
                "玩家说「训练」「练一下」「练功」「带它去举铁」时调用。"
            ),
        ),
        StructuredTool.from_function(
            name="feed_lobster",
            func=_feed,
            description=(
                "给玩家的龙虾喂食。玩家说「喂」「投喂」「吃饭」「给点好吃的」时调用。"
            ),
        ),
        StructuredTool.from_function(
            name="explore",
            func=_explore,
            description=(
                "让玩家的龙虾去探险。高随机度，可能拿新技能。"
                "玩家说「探险」「冒险」「出门走走」「溜达」时调用。"
            ),
        ),
        StructuredTool.from_function(
            name="rest",
            func=_rest,
            description=(
                "让龙虾休息恢复心情。玩家说「休息」「睡觉」「躺平」「歇会儿」时调用。"
            ),
        ),
        StructuredTool.from_function(
            name="work",
            func=_work,
            description=(
                "让龙虾去打工赚金币。玩家说「打工」「上班」「搬砖」「赚钱」时调用。"
            ),
        ),
        StructuredTool.from_function(
            name="battle",
            func=_battle,
            description=(
                "发起一场对战，系统匹配野生对手并按规则判胜负，返回完整文字战报。"
                "玩家说「挑战」「pk」「打架」「比一场」「决斗」「上场」时调用。"
                "战报的胜负是规则判定，你必须如实复述，不能反转。"
            ),
        ),
        StructuredTool.from_function(
            name="get_leaderboard",
            func=_leaderboard,
            description=(
                "查看全平台龙虾名气榜 Top10。"
                "玩家说「排行榜」「榜单」「谁最强」「我第几名」时调用。"
            ),
        ),
        StructuredTool.from_function(
            name="get_help",
            func=_help,
            description=(
                "返回玩法菜单。玩家说「帮助」「菜单」「怎么玩」「指令」时调用，"
                "或者你判断玩家迷茫时也可以主动调。"
            ),
        ),
        StructuredTool.from_function(
            name="open_shop",
            func=_open_shop,
            description=(
                "打开商店货架（每 2 小时全服刷新一次）。参数 kind ∈ "
                "{weapon, item, skill}：weapon=武器装备，item=战前道具，skill=技能升级。"
                "玩家说「商店」「逛街」「看看货」「看武器」「看道具」「看技能」时调用，"
                "不带方向时默认 weapon。"
            ),
        ),
        StructuredTool.from_function(
            name="buy_item",
            func=_buy,
            description=(
                "购买武器或道具。传入商品中文名（如「牙签长矛」「啤酒能量瓶」）。"
                "玩家说「买 XXX」「入手 XXX」时调用。"
                "返回 ⚠️ 开头的文本表示失败（金币不足/等级不够/不在货架等），如实复述。"
            ),
        ),
        StructuredTool.from_function(
            name="equip_item",
            func=_equip,
            description=(
                "把背包里的某件武器装到对应槽位。传入武器中文名。"
                "玩家说「装备 XXX」「上 XXX」「换上 XXX」时调用。"
                "同槽旧装备会自动退回背包。"
            ),
        ),
        StructuredTool.from_function(
            name="unequip_slot",
            func=_unequip,
            description=(
                "卸下某个槽位的装备。槽位中文：主钳 / 副钳 / 背甲 / 鞋。"
                "玩家说「卸下 主钳」「不要鞋」时调用。"
            ),
        ),
        StructuredTool.from_function(
            name="upgrade_skill",
            func=_upgrade_skill,
            description=(
                "用金币升级已习得的技能（最高 Lv.3）。传入技能中文名。"
                "玩家说「升级 蒜蓉觉醒」「精进 横着走」时调用。"
                "玩家未习得的技能不能升级，会返回 ⚠️ 提示。"
            ),
        ),
        StructuredTool.from_function(
            name="show_loadout",
            func=_show_loadout,
            description=(
                "查看自己当前的装备、技能等级与道具背包。"
                "玩家说「我的装备」「装备面板」「背包」「我学了啥技能」时调用。"
            ),
        ),
        StructuredTool.from_function(
            name="get_battle_history",
            func=_battle_history,
            description=(
                "查看自己最近 N 场战斗的精简战绩（默认 5 场，最多 10 场）。"
                "玩家说「最近战绩」「最近打了啥」「战斗历史」「上一场赢了吗」时调用。"
                "参数 = 想看的场数（可省略，留空就用默认 5）。"
                "返回的每行只是简短摘要（对手/胜负/回合数/标签），"
                "完整战报详情走前端 web 页面，AI 不要试图复述整场战报。"
            ),
        ),
        StructuredTool.from_function(
            name="query_other_lobster",
            func=_query_other,
            description=(
                "查别人龙虾的公开信息：属性、技能、战绩、流派、称号、当前名气排名。"
                "玩家说「查 XXX」「XX 啥水平」「XX 几级」「他叫什么名字」（带名字时）时调用。"
                "参数 = 对方龙虾的中文名（精确匹配）。"
                "返回 ⚠️ 开头表示没找到，如实复述给玩家、提醒可能名字打错或对方还没上场。"
                "**不会**返回对方的金币 / 道具 / 装备 / token——这是隐私字段。"
            ),
        ),
        StructuredTool.from_function(
            name="pvp_random",
            func=_pvp_random,
            description=(
                "随机 PvP：从全平台玩家池随机抽一只挑战（不含 BOSS）。"
                "玩家说「随机挑战」「随机 PK」「随便来一场」「随机找个玩家打」时调用。"
                "和普通挑战共用同一个冷却（避免刷战绩）。同一对手 30 分钟最多打 1 次。"
                "战报里若包含「📨 已通知对手」表示对手当前在线、已收到挑战通知。"
            ),
        ),
        StructuredTool.from_function(
            name="challenge_player",
            func=_pvp_specific,
            description=(
                "指定名字挑战另一名玩家（不含 BOSS——BOSS 用 challenge_boss）。"
                "玩家说「挑战 蒜蓉暴君」「跟 麻辣战神 PK」「打 XXX」时调用。"
                "参数 = 对方龙虾的中文名（精确匹配）。同一对手 30 分钟最多打 1 次。"
                "返回 ⚠️ 开头表示找不到 / 是 BOSS / 频控未过 / 自己挑自己，原样转述给玩家并指出原因。"
            ),
        ),
        StructuredTool.from_function(
            name="challenge_boss",
            func=_pvp_boss,
            description=(
                "挑战预设的 BOSS 龙虾（参数固定的强敌副本，难度高、奖励翻倍）。"
                "玩家说「挑战 BOSS XXX」「打 boss XXX」「来一场 boss 战」时调用。"
                "参数 = boss 中文名（如「不锈钢魔王」「霓虹夜行者」「蒜蓉帝王」）。"
                "BOSS 战不受 30 分钟频控，但仍走 battle 动作冷却。胜利会额外加金币 + 名气。"
                "BOSS 不算玩家——不出现在玩家排行榜，也不会被随机 PvP 抽到。"
                "玩家不知道有哪些 boss 时，先调 list_active_players 把名单给他。"
            ),
        ),
        StructuredTool.from_function(
            name="list_active_players",
            func=_list_players,
            description=(
                "列出当前可挑战的玩家（按名气排序）+ 全部 BOSS 龙虾名单。"
                "玩家说「列玩家」「在线玩家」「谁能打」「有哪些 boss」「打谁」时调用。"
                "返回包含每只龙虾的等级 / 名气 / 战绩 / BOSS 简介，不含装备等隐私字段。"
            ),
        ),
    ]
