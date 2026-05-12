"""持久化对外门面：所有 save_* / load_* 都走这里，下面是 SQLite。

保留与 storage_legacy 一致的函数签名，让上层 api.main 调用零侵入；
但底层已切到 SQLite (DAO)。

Phase 1c 之后启动时会自动跑 migration，把 JSON → SQLite。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from . import dao

logger = logging.getLogger(__name__)


# ============ Lobster ============


async def save_lobsters(lobsters: Dict[str, Dict[str, Any]]) -> None:
    """全量保存。当前是批量 upsert；龙虾不会被这里删除（淘汰走 delete_lobster）。"""
    await dao.save_lobsters_bulk(lobsters)
    logger.debug("storage: 保存了 %d 只龙虾 (sqlite)", len(lobsters))


async def load_lobsters() -> Dict[str, Dict[str, Any]]:
    data = await dao.load_all_lobsters()
    logger.info("storage: 加载 %d 只龙虾 (sqlite)", len(data))
    return data


# ============ Bot 凭证 ============


async def save_bots(bots: Dict[str, Dict[str, Any]]) -> None:
    await dao.save_bots_bulk(bots)
    logger.debug("storage: 保存了 %d 个 bot 凭证 (sqlite)", len(bots))


async def load_bots() -> Dict[str, Dict[str, Any]]:
    data = await dao.load_all_bots()
    logger.info("storage: 加载 %d 个 bot 凭证 (sqlite)", len(data))
    return data


# ============ 战报 Feed ============


async def save_feed(feed: List[Dict[str, Any]]) -> None:
    """兼容旧调用：上层把 STATE.feed 整个传过来时，我们做"差量插入 + 截断"。

    注意旧实现是"整个文件覆盖只留最近 200 条"——这里改成 SQLite 累加：
    - 假设上层每次都是把 STATE.feed 整个传过来
    - 我们只插入"最近一条"；如果 feed 长度变了多于 1 条则全部插（兜底）
    - 然后调 trim 保持总量 <= 200

    这样写避免重复插入历史数据。
    """
    if not feed:
        return
    existing = await dao.load_recent_feed(limit=1)
    last_ts = existing[0]["ts"] if existing else 0.0
    # 只插入比 last_ts 新的条目
    new_items = [it for it in feed if float(it.get("ts", 0)) > last_ts]
    if new_items:
        await dao.append_feed_bulk(new_items)
    # 总量截断（旧实现保留 200 条）
    await dao.trim_feed(keep=200)


async def load_feed() -> List[Dict[str, Any]]:
    """加载最近 200 条战报（按时间升序，与旧 JSON 顺序保持一致）。"""
    items = await dao.load_recent_feed(limit=200)
    items.reverse()  # dao 按 id DESC，反转回来变时间升序
    logger.info("storage: 加载 %d 条战报 (sqlite)", len(items))
    return items
