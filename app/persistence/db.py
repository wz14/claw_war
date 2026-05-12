"""SQLite 连接 + schema 初始化。

设计选型：
- 用 Python 标准库 sqlite3，避免引入 ORM 重型依赖
- 同步 API + asyncio.to_thread 包装异步（DAO 层做）
- WAL 模式 + busy_timeout，单文件多并发足够黑客松场景
- schema 用 CREATE TABLE IF NOT EXISTS 幂等创建，启动时调一次

表：
- lobsters       : 一个龙虾一行，复杂结构（skills/titles/equipped/...）存 JSON blob
- bot_sessions   : 一个 bot 一行
- battles        : 一场对战一行（Phase 4-5 引入 PvP / 多回合时再扩字段）
- shop_purchases : Phase 5 商店流水
- chat_messages  : 预留给 Phase 6 的 AI 对话历史
- feed           : 战报流（兼容旧 feed.json 数据结构）

不写 fallback：连接失败、schema 失败一律抛出，让上层 fail fast。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


DATA_DIR = Path(os.environ.get("CLAW_WAR_DATA_DIR", "./data"))
DB_FILE = DATA_DIR / "claw_war.db"


# 全局单例连接。SQLite 在同一线程内串行使用是安全的，
# 我们用 check_same_thread=False + 自己加锁/线程隔离，让 asyncio.to_thread 能复用同一个连接。
_CONN: Optional[sqlite3.Connection] = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lobsters (
    user_id        TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    level          INTEGER NOT NULL DEFAULT 1,
    fame           INTEGER NOT NULL DEFAULT 0,
    wins           INTEGER NOT NULL DEFAULT 0,
    losses         INTEGER NOT NULL DEFAULT 0,
    is_bot         INTEGER NOT NULL DEFAULT 0,
    bot_kind       TEXT NOT NULL DEFAULT '',
    updated_at     REAL NOT NULL,
    blob_json      TEXT NOT NULL  -- 完整 Lobster.to_dict()
);
CREATE INDEX IF NOT EXISTS idx_lobsters_fame ON lobsters(fame DESC, wins DESC, level DESC);
CREATE INDEX IF NOT EXISTS idx_lobsters_is_bot ON lobsters(is_bot, level);

CREATE TABLE IF NOT EXISTS bot_sessions (
    user_id         TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    token           TEXT NOT NULL,
    base_url        TEXT NOT NULL,
    sync_buf        TEXT NOT NULL DEFAULT '',
    context_token   TEXT NOT NULL DEFAULT '',
    welcomed        INTEGER NOT NULL DEFAULT 0,
    dead            INTEGER NOT NULL DEFAULT 0,
    dead_reason     TEXT NOT NULL DEFAULT '',
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_sessions_dead ON bot_sessions(dead);

CREATE TABLE IF NOT EXISTS battles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    challenger_uid  TEXT NOT NULL,
    opponent_uid    TEXT NOT NULL,
    winner_uid      TEXT NOT NULL,
    is_pvp          INTEGER NOT NULL DEFAULT 0,
    is_clutch       INTEGER NOT NULL DEFAULT 0,
    is_upset        INTEGER NOT NULL DEFAULT 0,
    rewards_json    TEXT NOT NULL DEFAULT '{}',
    narration       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_battles_challenger ON battles(challenger_uid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_battles_opponent ON battles(opponent_uid, ts DESC);

CREATE TABLE IF NOT EXISTS shop_purchases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    user_id     TEXT NOT NULL,
    item_kind   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    price       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shop_purchases_user ON shop_purchases(user_id, ts DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    user_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_user ON chat_messages(user_id, ts DESC);

CREATE TABLE IF NOT EXISTS feed (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    player      TEXT NOT NULL,
    narration   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feed_ts ON feed(ts DESC);

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def init_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """初始化全局 SQLite 连接，建表。

    幂等：可以重复调用，第二次直接复用现有连接。
    """
    global _CONN
    if _CONN is not None:
        return _CONN
    _ensure_dir()
    target = db_path if db_path is not None else DB_FILE
    logger.info("db: 初始化 SQLite 连接 path=%s", target)
    conn = sqlite3.connect(
        target,
        check_same_thread=False,
        isolation_level=None,  # 自动 commit；显式 BEGIN/COMMIT 用 executescript
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    _CONN = conn
    logger.info("db: schema 初始化完成")
    return conn


def get_conn() -> sqlite3.Connection:
    """拿连接。未初始化就直接抛——按规则不写隐式 fallback。"""
    if _CONN is None:
        raise RuntimeError("db: SQLite 连接未初始化，请先调用 init_db()")
    return _CONN


def close_db() -> None:
    global _CONN
    if _CONN is not None:
        _CONN.close()
        _CONN = None
        logger.info("db: 已关闭 SQLite 连接")


def get_meta(key: str) -> Optional[str]:
    row = get_conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None else None


def set_meta(key: str, value: str) -> None:
    get_conn().execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
