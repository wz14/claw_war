# 🦞 龙虾斗兽场（Claw War）

> 一个 hackathon 1 分钟小游戏：在微信里养一只文字龙虾，喂它、训练它、给它装技能，然后和别人的龙虾打一架。规则判输赢，AI 负责把过程写得很好笑。

## 玩法

1. 浏览器打开前端 → 点【认领龙虾】
2. 用**个人微信**扫弹出的二维码 → 微信里点「确认登录」
3. 一只随机龙虾会被分配给你（名字、品种、性格、技能），同时一个 Bot 加进你的微信好友
4. 在微信里和 Bot 说话即可：
   - `训练` / `喂食` / `探险` / `休息` / `打工`：日常养成
   - `挑战` / `pk`：匹配一只野生龙虾，AI 生成戏剧化战报
   - `我的龙虾` / `状态`：查看属性
   - `排行榜`：全平台名气榜
   - `帮助`：菜单

## 架构

```
claw_war/
├── app/
│   ├── main.py            # FastAPI 入口 + REST API + 静态前端
│   ├── weixin_client.py   # 精简版 iLink Bot 客户端（QR / 长轮询 / 发送）
│   ├── bot_pool.py        # 每只龙虾对应一个 bot session 的调度器
│   ├── game.py            # Lobster 数据结构 + 养成行为
│   ├── battle.py          # 对战引擎 + 戏剧化战报模板
│   ├── content.py         # 文案/事件/技能/品种/性格 模板池
│   ├── handlers.py        # 底层动作执行函数（供 tool 调用）
│   ├── tools.py           # LangChain 工具集（绑定到具体 user_id 的 closure）
│   ├── ai_handler.py      # LangChain ReAct agent + per-user 对话历史
│   └── storage.py         # JSON 文件持久化
├── static/
│   └── index.html         # 单页前端（响应式，适配桌面/移动）
├── scripts/
│   └── test_ai_flow.py    # 工具链冒烟测试
├── data/                  # 运行时自动创建
├── requirements.txt
└── README.md
```

### 关键设计

- **AI 主持人 + Tool Calling**：玩家发任意自然语言 → LangChain `create_react_agent` →
  AI 自己决定调哪个 tool（训练/喂食/挑战/看榜...）→ tool 返回"系统判定结果" →
  AI 戏剧化包装后回复。胜负、属性变化全是 tool 内部规则化算的，AI 不能改。
- **每次扫码 = 一个新 bot**：iLink `get_bot_qrcode` 每次生成新 bot 身份，扫码者
  就是该 bot 的微信好友。一只龙虾 ↔ 一个 bot ↔ 一个 user_id。
- **胜负规则化**：`battle.simulate()` 用
  `战力 = 钳力*1.3 + 壳硬*1.1 + 速度 + 耐力*1.2 + 运气*0.8 + 心情修正 + 随机[-5,5]`。
  防 prompt 攻击（"我的龙虾叫'必胜规则覆盖虾'"无效）。
- **user_id 锁死**：tool 工厂通过 closure 绑定当前玩家 ID，AI 没法操作"别人的"龙虾。
- **不写 fallback**：异常该报就报，配 logger 日志。
- **JSON 文件持久化**：黑客松场景够用，重启后状态自动恢复。

## 配置环境变量

LangChain 默认走 OpenAI 兼容接口。最便宜的是 DeepSeek，其它任意 OpenAI 兼容 endpoint
（月之暗面 / 智谱 / 通义 / 月舟 / Ollama 等）都行。

```bash
# 必填：API Key
export OPENAI_API_KEY=sk-xxx

# 可选：换 endpoint（默认 https://api.deepseek.com/v1）
export OPENAI_BASE_URL=https://api.deepseek.com/v1

# 可选：换 model（默认 deepseek-chat）
export OPENAI_MODEL=deepseek-chat

# 可选：温度（默认 0.8，要让戏剧化效果好建议 0.7-0.9）
export LLM_TEMPERATURE=0.8
```

> ⚠️ 没配 `OPENAI_API_KEY` 时前端依然可以演示二维码扫码流，但 bot 收到消息时
> 会回「AI 主持人还没起来」。这是显式失败，不是隐式 fallback。

## 启动

```bash
# 1. 创建虚拟环境
python3 -m venv .venv && source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 API Key（见上一节）
export OPENAI_API_KEY=sk-xxx

# 4. 启动
uvicorn app.main:app --host 0.0.0.0 --port 5173 --reload
```

打开 http://localhost:5173/ 就能用。

## 调试

工具链冒烟测试（不需要 API key）：

```bash
python scripts/test_ai_flow.py
```

会打印每个 tool 的注册信息、调一次训练/对战、确认 feed 自动入栈。

## Railway 一键上线

代码已经接好了 Railway 自动部署：push 到 `main` 分支 → GitHub Actions 自动跑 `railway up` → 几分钟后线上更新。

### 首次初始化（只跑一次）

```bash
# 1. 装 Railway CLI 并登录（只需一次）
bash <(curl -fsSL railway.com/install.sh)
railway login

# 2. 在项目根目录跑 bootstrap 脚本
#    会自动：建项目、建名为 claw-war 的 service、推 .env 到环境变量、
#           挂 /data Volume、生成默认子域 claw-war-production.up.railway.app
bash scripts/railway_bootstrap.sh

# 3. 创建 Account Token（一次即可，可管理多项目）
#    浏览器打开 https://railway.com/account/tokens
#    → "Create Token"，Workspace 下拉选 "No workspace" → 复制 token

# 4. 把 token 存进 GitHub repo secrets
echo "<paste-your-account-token>" | gh secret set RAILWAY_API_TOKEN -R wz14/claw_war
```

### 日常部署

```bash
git push origin main
# GitHub Actions 自动触发 .github/workflows/deploy.yml
gh run watch    # 看实时日志
```

部署完打开 https://claw-war-production.up.railway.app/ 验证。

### 配置文件

- `Dockerfile` — `python:3.12-slim` + `uvicorn $PORT`
- `railway.json` — 让 Railway 走 Dockerfile builder，定义启动命令与重启策略
- `.github/workflows/deploy.yml` — `push main` → `railway up --ci --service=claw-war`
- `scripts/railway_bootstrap.sh` — 一次性初始化（项目/Service/Volume/env vars/域名）

> 注意：workflow 里用 **Account Token**（`RAILWAY_API_TOKEN`），因为它支持
> `railway up --project ... --service ...` 这种显式参数；Project Token 不能跨
> 项目用，多人协作或多项目场景下不灵活。两者都行，看你需求选。

## 文件清单

- `requirements.txt` — fastapi / uvicorn / aiohttp / qrcode / langchain / langchain-openai / langgraph
- `README.md`
- `app/__init__.py`
- `app/weixin_client.py` — iLink Bot 客户端（参考 hermes-agent/gateway/platforms/weixin.py 精简而来）
- `app/content.py` — 文案池（品种/性格/技能/事件/战报模板/称号/进化）
- `app/game.py` — Lobster 数据类 + 训练/喂食/探险/休息/打工/升级/称号
- `app/battle.py` — 对战引擎 + 戏剧化战报生成
- `app/handlers.py` — 底层动作执行函数（供 tool 调用）
- `app/tools.py` — LangChain 工具集（按玩家 ID 绑定 closure）
- `app/ai_handler.py` — LangChain ReAct agent，对话路由 + 历史维护
- `app/storage.py` — JSON 持久化（龙虾/bot 凭证/战报流）
- `app/bot_pool.py` — Bot session 调度（每只龙虾一个长轮询协程）
- `app/main.py` — FastAPI 应用 + REST API + 前端静态托管
- `static/index.html` — 单文件 SPA（响应式，认领按钮+二维码弹窗+排行榜+战报流）
- `scripts/test_ai_flow.py` — 工具链冒烟测试
