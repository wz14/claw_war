"""JSON 文件持久化。

黑客松场景：单文件、整体落盘，配合 asyncio 锁简单串行化。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("CLAW_WAR_DATA_DIR", "./data"))
LOBSTERS_FILE = DATA_DIR / "lobsters.json"
BOTS_FILE = DATA_DIR / "bots.json"
FEED_FILE = DATA_DIR / "feed.json"

_lock = asyncio.Lock()


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, payload: Any) -> None:
    """先写 .tmp 再 rename，避免半截文件。"""
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


# ============ Lobster 持久化 ============


async def save_lobsters(lobsters: Dict[str, Dict[str, Any]]) -> None:
    async with _lock:
        _atomic_write(LOBSTERS_FILE, lobsters)
    logger.debug("storage: 保存了 %d 只龙虾", len(lobsters))


def load_lobsters() -> Dict[str, Dict[str, Any]]:
    data = _read_json(LOBSTERS_FILE, {})
    logger.info("storage: 加载 %d 只历史龙虾", len(data))
    return data


# ============ Bot 凭证持久化 ============


async def save_bots(bots: Dict[str, Dict[str, Any]]) -> None:
    async with _lock:
        _atomic_write(BOTS_FILE, bots)
    logger.debug("storage: 保存了 %d 个 bot 凭证", len(bots))


def load_bots() -> Dict[str, Dict[str, Any]]:
    data = _read_json(BOTS_FILE, {})
    logger.info("storage: 加载 %d 个历史 bot 凭证", len(data))
    return data


# ============ 战报 Feed 持久化 ============


async def save_feed(feed: List[Dict[str, Any]]) -> None:
    async with _lock:
        # 只保留最近 200 条，太多没必要
        _atomic_write(FEED_FILE, feed[-200:])


def load_feed() -> List[Dict[str, Any]]:
    return _read_json(FEED_FILE, [])
