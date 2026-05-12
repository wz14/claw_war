#!/usr/bin/env python3
"""把旧的 JSON 数据一次性迁移到 SQLite。

正常路径下 [app/api/main.py](app/api/main.py) 启动时会自动调
[app.persistence.migration.maybe_migrate_json_to_sqlite](app/persistence/migration.py)，
所以**不需要**显式跑这个脚本。

但提供 CLI 入口的好处：
1. 部署 Railway 之前可以本地先跑一次，校验 JSON 数据完整再 push
2. 测试环境可以独立验证迁移结果

用法：
    # 默认从 ./data/lobsters.json 等读、写到 ./data/claw_war.db
    python scripts/migrate_json_to_sqlite.py

    # 自定义数据目录
    CLAW_WAR_DATA_DIR=/tmp/clawdata python scripts/migrate_json_to_sqlite.py

不写 fallback：所有失败都会抛出，scripts 直接 exit 非 0。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

# 让 scripts 能 import 到 app/
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.persistence import db, migration  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("migrate")


async def main() -> None:
    db.init_db()
    result = await migration.maybe_migrate_json_to_sqlite()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
