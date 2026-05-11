"""龙虾斗兽场 - 后端包入口。

模块组织：
- weixin_client: 精简版微信 iLink Bot API 客户端
- content:      文案/事件/技能/品种/性格 模板池（戏剧化全靠它）
- game:         Lobster 数据结构 + 行为方法
- battle:       对战公式 + 战报生成
- handlers:     微信端命令分发（训练/喂食/挑战 等）
- bot_pool:     一个用户对应一个 bot session 的调度器
- storage:      JSON 文件持久化
- main:         FastAPI 应用 + 前端静态托管
"""
