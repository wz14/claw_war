"""一次性 JSON → SQLite 迁移。

启动时调一次：
- 如果 meta 里没有 migrated_from_json=1，就把 data/lobsters.json / bots.json /
  feed.json 三个文件读进来，备份到 data/legacy/<ts>/ 后写入 SQLite
- 写完打 meta=1，下次启动直接跳过

不写 fallback：备份失败、写库失败一律抛，让上层 fail fast。
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict

from . import db, storage_legacy
from . import dao

logger = logging.getLogger(__name__)


_MIGRATION_KEY = "migrated_from_json_v1"


def _backup_legacy_jsons(target_dir: Path) -> Dict[str, bool]:
    """把 lobsters/bots/feed JSON 复制到 target_dir，返回各文件是否存在。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    result: Dict[str, bool] = {}
    for path in (
        storage_legacy.LOBSTERS_FILE,
        storage_legacy.BOTS_FILE,
        storage_legacy.FEED_FILE,
    ):
        if path.exists():
            shutil.copy2(path, target_dir / path.name)
            result[path.name] = True
        else:
            result[path.name] = False
    return result


async def maybe_migrate_json_to_sqlite() -> Dict[str, Any]:
    """启动时调用。已经迁移过就直接返回。"""
    if db.get_meta(_MIGRATION_KEY) == "1":
        logger.info("migration: 已迁移过，跳过")
        return {"already_done": True}

    ts = int(time.time())
    legacy_dir = storage_legacy.DATA_DIR / "legacy" / str(ts)
    backup_status = _backup_legacy_jsons(legacy_dir)
    logger.info("migration: 备份 JSON 到 %s status=%s", legacy_dir, backup_status)

    lobsters = storage_legacy.load_lobsters()
    bots = storage_legacy.load_bots()
    feed = storage_legacy.load_feed()
    logger.info(
        "migration: 读 JSON 完成 lobsters=%d bots=%d feed=%d",
        len(lobsters), len(bots), len(feed),
    )

    if lobsters:
        await dao.save_lobsters_bulk(lobsters)
    if bots:
        await dao.save_bots_bulk(bots)
    if feed:
        await dao.append_feed_bulk(feed)

    db.set_meta(_MIGRATION_KEY, "1")
    db.set_meta("migrated_at", str(ts))
    db.set_meta("migrated_counts", f"lobsters={len(lobsters)} bots={len(bots)} feed={len(feed)}")
    logger.info(
        "migration: ✅ 迁移完成 lobsters=%d bots=%d feed=%d backup=%s",
        len(lobsters), len(bots), len(feed), legacy_dir,
    )
    return {
        "already_done": False,
        "lobsters": len(lobsters),
        "bots": len(bots),
        "feed": len(feed),
        "backup_dir": str(legacy_dir),
    }
