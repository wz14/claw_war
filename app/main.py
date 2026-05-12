"""向后兼容 shim：保留 `uvicorn app.main:app` 入口。

实际实现在 [app/api/main.py](app/api/main.py)。
脚本 scripts/test_ai_chat.py 会 `from app.main import AppState`，所以这里也 re-export。
"""

from .api.main import (  # noqa: F401
    AppState,
    ClaimAttempt,
    STATE,
    app,
    on_inbound,
)
