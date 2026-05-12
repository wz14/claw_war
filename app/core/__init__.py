"""游戏核心：龙虾数据结构、动作判定、对战引擎。

不依赖 web/微信/AI 层，可被任何上游模块复用。
"""

from .lobster import Lobster, ACTION_COOLDOWN_SECONDS
from .factory import create_lobster, make_wild_opponent, random_name
from . import actions, battle

__all__ = [
    "Lobster",
    "ACTION_COOLDOWN_SECONDS",
    "create_lobster",
    "make_wild_opponent",
    "random_name",
    "actions",
    "battle",
]
