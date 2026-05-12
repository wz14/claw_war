"""DAO：把 SQLite CRUD 包装成异步接口。

用法约定：
- 所有写入接口都是 async def，内部用 asyncio.to_thread 调同步 sqlite3
- 读取接口也用 to_thread，避免阻塞 event loop
- DAO 不持有 Lobster/BotSession 类型，只接收 dict（避免 persistence 反向依赖 core/integrations）
- 上层 main.py 在调用前自己做 to_dict() 转换
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from . import db

logger = logging.getLogger(__name__)


# ============ Lobster ============


def _save_lobster_sync(user_id: str, lobster_dict: Dict[str, Any]) -> None:
    blob = json.dumps(lobster_dict, ensure_ascii=False, separators=(",", ":"))
    db.get_conn().execute(
        "INSERT INTO lobsters(user_id, name, level, fame, wins, losses, "
        "is_bot, bot_kind, updated_at, blob_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "  name=excluded.name, level=excluded.level, fame=excluded.fame, "
        "  wins=excluded.wins, losses=excluded.losses, is_bot=excluded.is_bot, "
        "  bot_kind=excluded.bot_kind, updated_at=excluded.updated_at, "
        "  blob_json=excluded.blob_json",
        (
            user_id,
            str(lobster_dict.get("name", "")),
            int(lobster_dict.get("level", 1)),
            int(lobster_dict.get("fame", 0)),
            int(lobster_dict.get("wins", 0)),
            int(lobster_dict.get("losses", 0)),
            1 if lobster_dict.get("is_bot") else 0,
            str(lobster_dict.get("bot_kind", "")),
            time.time(),
            blob,
        ),
    )


async def save_lobster(user_id: str, lobster_dict: Dict[str, Any]) -> None:
    await asyncio.to_thread(_save_lobster_sync, user_id, lobster_dict)


async def save_lobsters_bulk(lobsters: Dict[str, Dict[str, Any]]) -> None:
    """批量保存（启动时全量落盘用）。"""
    def _bulk() -> None:
        conn = db.get_conn()
        conn.execute("BEGIN")
        try:
            for uid, ld in lobsters.items():
                _save_lobster_sync(uid, ld)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await asyncio.to_thread(_bulk)
    logger.debug("dao: 批量保存 %d 只龙虾", len(lobsters))


def _load_all_lobsters_sync() -> Dict[str, Dict[str, Any]]:
    rows = db.get_conn().execute("SELECT user_id, blob_json FROM lobsters").fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        try:
            out[row["user_id"]] = json.loads(row["blob_json"])
        except Exception as exc:
            logger.error("dao: 解析 lobster blob 失败 uid=%s err=%s", row["user_id"][:8], exc)
            raise
    return out


async def load_all_lobsters() -> Dict[str, Dict[str, Any]]:
    return await asyncio.to_thread(_load_all_lobsters_sync)


def _delete_lobster_sync(user_id: str) -> None:
    db.get_conn().execute("DELETE FROM lobsters WHERE user_id=?", (user_id,))


async def delete_lobster(user_id: str) -> None:
    await asyncio.to_thread(_delete_lobster_sync, user_id)


def _load_lobster_sync(user_id: str) -> Optional[Dict[str, Any]]:
    """读单只龙虾的 blob_json（Phase 6 给 /api/battles 详情用）。"""
    row = db.get_conn().execute(
        "SELECT blob_json FROM lobsters WHERE user_id=?", (user_id,)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["blob_json"])


async def load_lobster(user_id: str) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(_load_lobster_sync, user_id)


# ============ Bot Session ============


def _save_bot_sync(user_id: str, sess_dict: Dict[str, Any]) -> None:
    db.get_conn().execute(
        "INSERT INTO bot_sessions(user_id, account_id, token, base_url, "
        "sync_buf, context_token, welcomed, dead, dead_reason, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "  account_id=excluded.account_id, token=excluded.token, "
        "  base_url=excluded.base_url, sync_buf=excluded.sync_buf, "
        "  context_token=excluded.context_token, welcomed=excluded.welcomed, "
        "  dead=excluded.dead, dead_reason=excluded.dead_reason, "
        "  updated_at=excluded.updated_at",
        (
            user_id,
            str(sess_dict.get("account_id", "")),
            str(sess_dict.get("token", "")),
            str(sess_dict.get("base_url", "")),
            str(sess_dict.get("sync_buf", "")),
            str(sess_dict.get("context_token", "")),
            1 if sess_dict.get("welcomed") else 0,
            1 if sess_dict.get("dead") else 0,
            str(sess_dict.get("dead_reason", "")),
            time.time(),
        ),
    )


async def save_bot(user_id: str, sess_dict: Dict[str, Any]) -> None:
    await asyncio.to_thread(_save_bot_sync, user_id, sess_dict)


async def save_bots_bulk(sessions: Dict[str, Dict[str, Any]]) -> None:
    def _bulk() -> None:
        conn = db.get_conn()
        conn.execute("BEGIN")
        try:
            for uid, sd in sessions.items():
                _save_bot_sync(uid, sd)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await asyncio.to_thread(_bulk)
    logger.debug("dao: 批量保存 %d 个 bot session", len(sessions))


def _load_all_bots_sync() -> Dict[str, Dict[str, Any]]:
    rows = db.get_conn().execute("SELECT * FROM bot_sessions").fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        out[row["user_id"]] = {
            "account_id": row["account_id"],
            "token": row["token"],
            "base_url": row["base_url"],
            "user_id": row["user_id"],
            "sync_buf": row["sync_buf"],
            "context_token": row["context_token"],
            "welcomed": bool(row["welcomed"]),
            "dead": bool(row["dead"]),
            "dead_reason": row["dead_reason"],
        }
    return out


async def load_all_bots() -> Dict[str, Dict[str, Any]]:
    return await asyncio.to_thread(_load_all_bots_sync)


# ============ Feed（战报流）============


def _append_feed_sync(player: str, narration: str, ts: Optional[float] = None) -> None:
    db.get_conn().execute(
        "INSERT INTO feed(ts, player, narration) VALUES(?, ?, ?)",
        (ts if ts is not None else time.time(), player, narration),
    )


async def append_feed(player: str, narration: str, ts: Optional[float] = None) -> None:
    await asyncio.to_thread(_append_feed_sync, player, narration, ts)


async def append_feed_bulk(items: List[Dict[str, Any]]) -> None:
    def _bulk() -> None:
        conn = db.get_conn()
        conn.execute("BEGIN")
        try:
            for it in items:
                _append_feed_sync(
                    str(it.get("player", "")),
                    str(it.get("narration", "")),
                    float(it["ts"]) if "ts" in it else None,
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    await asyncio.to_thread(_bulk)


def _load_recent_feed_sync(limit: int) -> List[Dict[str, Any]]:
    rows = db.get_conn().execute(
        "SELECT ts, player, narration FROM feed ORDER BY id DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [
        {"ts": row["ts"], "player": row["player"], "narration": row["narration"]}
        for row in rows
    ]


async def load_recent_feed(limit: int = 20) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_load_recent_feed_sync, limit)


def _trim_feed_sync(keep: int) -> int:
    """只保留最近 keep 条 feed，返回删除的行数。"""
    cur = db.get_conn().execute(
        "DELETE FROM feed WHERE id NOT IN (SELECT id FROM feed ORDER BY id DESC LIMIT ?)",
        (max(1, int(keep)),),
    )
    return cur.rowcount or 0


async def trim_feed(keep: int = 200) -> int:
    return await asyncio.to_thread(_trim_feed_sync, keep)


# ============ Battles（Phase 6 战斗历史）============


def _save_battle_sync(
    challenger_uid: str,
    opponent_uid: str,
    winner_uid: str,
    narration: str,
    rewards_meta: Dict[str, Any],
    is_pvp: bool = False,
    is_clutch: bool = False,
    is_upset: bool = False,
    ts: Optional[float] = None,
) -> int:
    """同步落 battles 表。返回新插入的 id。

    rewards_meta 既包含奖励（exp/coins/fame）也包含元数据（双方名字/胜方名字/结束回合），
    一律 dump 进 rewards_json，避免再扩 schema。
    """
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO battles("
        "ts, challenger_uid, opponent_uid, winner_uid, "
        "is_pvp, is_clutch, is_upset, rewards_json, narration"
        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts if ts is not None else time.time(),
            challenger_uid,
            opponent_uid,
            winner_uid,
            1 if is_pvp else 0,
            1 if is_clutch else 0,
            1 if is_upset else 0,
            json.dumps(rewards_meta, ensure_ascii=False, separators=(",", ":")),
            narration,
        ),
    )
    new_id = int(cur.lastrowid or 0)
    logger.debug(
        "dao: 战斗入库 id=%d challenger=%s opponent=%s winner=%s pvp=%s",
        new_id, challenger_uid[:8], opponent_uid[:8], winner_uid[:8], is_pvp,
    )
    return new_id


def save_battle_sync(
    challenger_uid: str,
    opponent_uid: str,
    winner_uid: str,
    narration: str,
    rewards_meta: Dict[str, Any],
    is_pvp: bool = False,
    is_clutch: bool = False,
    is_upset: bool = False,
    ts: Optional[float] = None,
) -> int:
    """同步入库（供 actions.handle_battle 这种 sync 路径直接调）。"""
    return _save_battle_sync(
        challenger_uid=challenger_uid,
        opponent_uid=opponent_uid,
        winner_uid=winner_uid,
        narration=narration,
        rewards_meta=rewards_meta,
        is_pvp=is_pvp,
        is_clutch=is_clutch,
        is_upset=is_upset,
        ts=ts,
    )


def _load_battles_for_user_sync(user_id: str, limit: int) -> List[Dict[str, Any]]:
    """按时间倒序，拉这个 user 参与过（challenger 或 opponent 任一）的最近 N 场战斗。"""
    rows = db.get_conn().execute(
        "SELECT id, ts, challenger_uid, opponent_uid, winner_uid, "
        "is_pvp, is_clutch, is_upset, rewards_json, narration "
        "FROM battles "
        "WHERE challenger_uid = ? OR opponent_uid = ? "
        "ORDER BY id DESC LIMIT ?",
        (user_id, user_id, max(1, int(limit))),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        meta: Dict[str, Any] = json.loads(row["rewards_json"] or "{}")
        out.append({
            "id": int(row["id"]),
            "ts": float(row["ts"]),
            "challenger_uid": row["challenger_uid"],
            "opponent_uid": row["opponent_uid"],
            "winner_uid": row["winner_uid"],
            "is_pvp": bool(row["is_pvp"]),
            "is_clutch": bool(row["is_clutch"]),
            "is_upset": bool(row["is_upset"]),
            "rewards_meta": meta,
            "narration": row["narration"],
        })
    return out


async def load_battles_for_user(user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_load_battles_for_user_sync, user_id, limit)


def load_battles_for_user_sync(user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """同步版（给 sync tools 用）。"""
    return _load_battles_for_user_sync(user_id, limit)


# ============ 计数 / 统计（给 /api/stats 用）============


def _stats_sync() -> Dict[str, int]:
    conn = db.get_conn()
    lobster_count = conn.execute("SELECT COUNT(*) AS c FROM lobsters").fetchone()["c"]
    battle_count = conn.execute("SELECT COUNT(*) AS c FROM battles").fetchone()["c"]
    return {
        "lobster_count": int(lobster_count),
        "battle_count": int(battle_count),
    }


async def get_db_stats() -> Dict[str, int]:
    return await asyncio.to_thread(_stats_sync)
