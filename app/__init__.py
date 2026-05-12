"""龙虾斗兽场 - 后端包入口。

模块组织（Phase 1 重构后）：
- core/         游戏核心：龙虾数据结构 + 行为 + 对战引擎
- agent/        AI 主持人：LLM、tools、prompts
- integrations/ 外部集成：微信 iLink Bot 协议、bot session 调度
- persistence/  持久化：SQLite + DAO + 一次性 JSON → SQLite 迁移
- api/          FastAPI 路由 + AppState + lifespan
- content/      文案/事件/技能模板池
- main.py       入口 shim：from .api.main import app
"""
