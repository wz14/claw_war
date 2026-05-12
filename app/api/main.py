"""龙虾斗兽场 FastAPI 主入口（拆分后真正的应用模块）。

提供：
- GET  /                       静态前端
- POST /api/claim              发起一次"认领龙虾"：拉取新二维码 + 后台轮询其状态
- GET  /api/claim/{sid}        查询某次认领的状态（前端轮询）
- GET  /api/qr/{sid}.png       把二维码 URL 渲染成 PNG 图片（前端 <img src>）
- GET  /api/leaderboard        排行榜
- GET  /api/feed               最近战报流
- GET  /api/stats              一些总数（虾口、对战数）

启动：
    uvicorn app.main:app --host 0.0.0.0 --port 5173
（app.main 是个 shim，会 re-export 这里的 app 对象）

历史变更：
- v0.1: 单文件 app/main.py + JSON 持久化
- v0.2 (Phase 1): 拆分到 app/api/main.py，引入 SQLite + DAO + 一次性迁移；
  Lobster 加 is_bot/equipped/inventory/skill_levels/last_pvp_targets 字段
"""

from __future__ import annotations

# 在所有 import 之前加载 .env，让 ChatOpenAI 等模块能读到环境变量。
from dotenv import load_dotenv as _load_dotenv  # noqa: E402

_load_dotenv()

import asyncio
import io
import logging
import os
import random
import secrets
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import qrcode
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import content
from ..agent import AIHandler
from ..core import bot_manager, factory, render
from ..core.lobster import Lobster
from ..integrations import weixin_client as wx
from ..integrations.bot_pool import (
    BotPool,
    BotSession,
    session_from_qr_confirmation,
)
from ..persistence import db, migration, storage

# === 全局日志配置 ===
# Railway / Docker 默认捕获 stdout，但 Python 默认 root logger 是 WARNING，
# 会吞掉所有 INFO，导致 inbound / tool 调用 / send_text 全部不可见，bug 无从定位。
# 这里强制 basicConfig 让 INFO 起步、并使用 force=True 覆盖 uvicorn 已建好的 handler。
# 通过 LOG_LEVEL 环境变量可调整（默认 INFO）。
#
# 注意：basicConfig 默认 StreamHandler 写 sys.stderr，而 Railway 把容器 stderr
# 整行视为 error 级别 → 会把所有 INFO 染红，按级别筛日志彻底失效。
# 这里显式把日志输出绑定到 sys.stdout，让 Railway 按真实级别上色 / 过滤。
_log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_name, logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
# 第三方库太吵的话单独压一压（保留 WARNING+）
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info("logging configured: level=%s", _log_level_name)


# ============ 全局状态 ============


@dataclass
class ClaimAttempt:
    """一次「认领龙虾」的状态机。

    流程：wait → scaned → confirmed → lobster_ready
                       ↘ expired / failed
    """

    sid: str
    qrcode_value: str
    qrcode_url: str
    base_url: str
    status: str = "wait"
    refresh_count: int = 0
    confirmed_at: Optional[float] = None
    user_id: Optional[str] = None
    lobster_name: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class AppState:
    http: Optional[aiohttp.ClientSession] = None
    claims: Dict[str, ClaimAttempt] = field(default_factory=dict)
    claim_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    lobsters: Dict[str, Lobster] = field(default_factory=dict)
    pool: Optional[BotPool] = None
    feed: List[Dict[str, Any]] = field(default_factory=list)
    persist_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ai: Optional[AIHandler] = None
    # Phase 3：种子人机维护任务（启动后常驻；shutdown 时 cancel）
    bot_maintenance_task: Optional[asyncio.Task] = None
    # Phase 4：缓存主 event loop 引用，给 sync tool 回调推送 PvP 通知用
    # （LangChain sync tool 跑在 thread executor，不能直接 asyncio.create_task）
    main_loop: Optional[asyncio.AbstractEventLoop] = None


STATE = AppState()


# ============ 持久化辅助 ============


def _persist_lobsters_sync_only() -> Dict[str, Dict[str, Any]]:
    return {uid: l.to_dict() for uid, l in STATE.lobsters.items()}


async def _persist_all() -> None:
    """统一落盘（SQLite，全异步）。"""
    async with STATE.persist_lock:
        await storage.save_lobsters(_persist_lobsters_sync_only())
        if STATE.pool is not None:
            await storage.save_bots(
                {uid: s.to_dict() for uid, s in STATE.pool.sessions.items()}
            )
        await storage.save_feed(STATE.feed)


async def _load_initial() -> None:
    """启动时把 SQLite 上的旧数据加载回内存。"""
    raw_map = await storage.load_lobsters()
    for uid, raw in raw_map.items():
        try:
            STATE.lobsters[uid] = Lobster.from_dict(raw)
        except Exception as exc:
            logger.warning("load lobster %s failed: %s", uid[:8], exc)
    STATE.feed = await storage.load_feed()
    logger.info(
        "启动加载完成：龙虾 %d 只，战报 %d 条", len(STATE.lobsters), len(STATE.feed),
    )


# ============ 入站消息处理 ============


def _render_welcome(lobster: Lobster) -> str:
    """根据龙虾属性渲染欢迎语模板。

    末尾的 {share_url} 占位符在这里填入，让欢迎语自带分享入口
    （Phase 2 排版规约：所有出站消息都要带分享链接）。
    """
    return content.WELCOME_TEMPLATE.format(
        name=lobster.name,
        breed=lobster.breed,
        personality=lobster.personality,
        claw=lobster.claw,
        shell=lobster.shell,
        speed=lobster.speed,
        stamina=lobster.stamina,
        luck=lobster.luck,
        skills="、".join(lobster.skills),
        share_url=content.SHARE_URL,
    )


async def on_inbound(
    session: BotSession,
    sender: str,
    text: str,
    context_token: Optional[str],
) -> Optional[str]:
    """长轮询收到一条用户消息时的回调。

    流程：
    1. 确保该玩家有一只龙虾（没就现造）
    2. 如果 bot session 还没发过欢迎语（扫码确认时主动发往往因为缺 context_token
       而失败），这次入站立刻独立发一次欢迎语；只有发送真的成功才把
       session.welcomed 置为 True，否则下一次入站会再试，避免介绍消息永远丢
    3. 把消息交给 ai_handler 处理，AI 自己决定调哪个 tool
    4. 持久化
    """
    if STATE.ai is None:
        return "（AI 主持人还没起来，请先配置 OPENAI_API_KEY 再重试。）"

    lobster = STATE.lobsters.get(sender)
    if lobster is None:
        lobster = factory.create_lobster(user_id=sender)
        STATE.lobsters[sender] = lobster
        logger.info("on_inbound: 为新玩家 %s 即时创建龙虾 %s", sender[:8], lobster.name)

    if not session.welcomed:
        welcome = _render_welcome(lobster)
        if STATE.pool is None:
            logger.error("on_inbound: pool 未初始化，欢迎语补发跳过")
        else:
            try:
                await STATE.pool.send(sender, welcome)
                session.welcomed = True
                logger.info("on_inbound: 补发欢迎语成功 uid=%s", sender[:8])
                for claim in STATE.claims.values():
                    if claim.user_id == sender and not claim.lobster_name:
                        claim.lobster_name = lobster.name
                        claim.status = "lobster_ready"
            except Exception as exc:
                # send 失败保持 welcomed=False，下次入站再试
                # 不写隐式 fallback：把异常记下，下条 AI 回复照常发
                logger.error(
                    "on_inbound: 补发欢迎语失败 uid=%s err=%s（welcomed 保持 False，下次再试）",
                    sender[:8], exc,
                )

    try:
        ai_reply = await STATE.ai.handle(sender, text)
    except Exception as exc:
        logger.error("on_inbound: AI 主持人异常 uid=%s: %s", sender[:8], exc, exc_info=True)
        ai_reply = "（兽场解说嗓子哑了，喝口水再来。请稍后再发一次。）"

    # Phase 2 排版规约：每条 AI 回复末尾附 player_card 页脚
    # （含名气排名 + 分享链接，方便玩家随手转发拉新人）
    if ai_reply:
        ai_reply = render.append_footer(ai_reply, lobster, STATE.lobsters)

    await _persist_all()
    return ai_reply or None


# ============ QR 轮询任务 ============


async def _qr_status_task(sid: str) -> None:
    """后台异步轮询某个 claim 的二维码状态，直到 confirmed / expired / 超时。"""
    assert STATE.http is not None
    claim = STATE.claims.get(sid)
    if claim is None:
        return
    deadline = time.monotonic() + 480  # 最长 8 分钟
    current_base = claim.base_url

    while time.monotonic() < deadline:
        if claim.status in ("expired", "failed", "lobster_ready"):
            return
        try:
            resp = await wx.poll_qrcode_status(
                STATE.http, qrcode_value=claim.qrcode_value, base_url=current_base,
            )
        except asyncio.TimeoutError:
            await asyncio.sleep(1)
            continue
        except Exception as exc:
            logger.warning("qr poll %s error: %s", sid[:8], exc)
            await asyncio.sleep(2)
            continue

        status = str(resp.get("status") or "wait")
        logger.debug("qr %s status=%s", sid[:8], status)

        if status == "wait":
            claim.status = "wait"
        elif status == "scaned":
            claim.status = "scaned"
        elif status == "scaned_but_redirect":
            redirect_host = str(resp.get("redirect_host") or "")
            if redirect_host:
                current_base = f"https://{redirect_host}"
                claim.base_url = current_base
        elif status == "expired":
            claim.refresh_count += 1
            if claim.refresh_count > 3:
                claim.status = "expired"
                claim.error = "二维码已过期太多次，请点「重新认领」。"
                logger.info("claim %s expired permanently", sid[:8])
                return
            try:
                value, url = await wx.fetch_qrcode(STATE.http)
                claim.qrcode_value = value
                claim.qrcode_url = url
                claim.status = "wait"
                logger.info("claim %s qr refreshed (count=%d)", sid[:8], claim.refresh_count)
            except Exception as exc:
                claim.status = "failed"
                claim.error = f"刷新二维码失败：{exc}"
                logger.error("refresh qr failed: %s", exc)
                return
        elif status == "confirmed":
            try:
                bot_session = session_from_qr_confirmation(resp)
            except Exception as exc:
                claim.status = "failed"
                claim.error = f"认领失败：{exc}"
                logger.error("session_from_qr_confirmation failed: %s", exc)
                return
            claim.status = "confirmed"
            claim.confirmed_at = time.time()
            claim.user_id = bot_session.user_id
            assert STATE.pool is not None
            await STATE.pool.register_and_start(bot_session)
            await _persist_all()
            logger.info(
                "claim %s confirmed user=%s bot=%s",
                sid[:8], bot_session.user_id[:8], bot_session.account_id[:12],
            )
            existing = STATE.lobsters.get(bot_session.user_id)
            if existing is None:
                lobster = factory.create_lobster(user_id=bot_session.user_id)
                STATE.lobsters[bot_session.user_id] = lobster
            else:
                lobster = existing
            claim.lobster_name = lobster.name
            welcome = _render_welcome(lobster)
            try:
                await STATE.pool.send(bot_session.user_id, welcome)
                bot_session.welcomed = True
                claim.status = "lobster_ready"
                logger.info("welcome message sent to %s", bot_session.user_id[:8])
            except Exception as exc:
                logger.warning(
                    "主动欢迎发送失败（welcomed 保持 False，下次入站会补发）：%s", exc,
                )
                claim.status = "lobster_ready"
            await _persist_all()
            return
        await asyncio.sleep(1.5)

    if claim.status not in ("lobster_ready", "confirmed"):
        claim.status = "expired"
        claim.error = "等候超时，请重新认领。"


# ============ FastAPI 生命周期 ============


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("启动龙虾斗兽场...")
    # Step 0: 缓存主 event loop 引用（PvP 通知从 sync tool 投递回主 loop 时要用）
    STATE.main_loop = asyncio.get_running_loop()
    logger.info("启动: 主 event loop 已缓存 id=%s", id(STATE.main_loop))

    # Step 1: 初始化 SQLite + 一次性迁移 JSON（幂等）
    db.init_db()
    migration_result = await migration.maybe_migrate_json_to_sqlite()
    logger.info("启动: migration 结果 %s", migration_result)

    # Step 2: HTTP 客户端
    STATE.http = aiohttp.ClientSession(
        trust_env=True,
        connector=wx.make_ssl_connector(),
    )

    # Step 3: 加载已有龙虾 / 战报到内存
    await _load_initial()

    # Step 4: BotPool + 失活回调
    STATE.pool = BotPool(STATE.http, inbound_cb=on_inbound)

    async def _persist_dead_session(_session: BotSession) -> None:
        """bot session 失活时立刻把 dead 状态落盘，避免重启后又把它拉起来刷屏。"""
        await _persist_all()
    STATE.pool.set_on_session_died(_persist_dead_session)

    # Step 5: AI 主持人。没配 OPENAI_API_KEY 就直接报错并继续，
    # bot 端会回提示让用户去配。这是允许"前端能用、bot 暂时回报错"的折衷。
    try:
        STATE.ai = AIHandler(STATE)
    except Exception as exc:
        logger.error("AIHandler 初始化失败：%s", exc)
        STATE.ai = None

    # Step 6: 恢复历史 bot session（dead 的会被 register_and_start 跳过）
    bots_raw = await storage.load_bots()
    for uid, raw in bots_raw.items():
        try:
            sess = BotSession.from_dict(raw)
            await STATE.pool.register_and_start(sess)
        except Exception as exc:
            logger.warning("恢复 bot session %s 失败: %s", uid[:8], exc)

    # Step 7: 启动种子人机维护（启动期补足下限 + 之后每 24h 新增 + 巡检）
    STATE.bot_maintenance_task = asyncio.create_task(
        bot_manager.bot_maintenance_loop(STATE),
        name="bot_maintenance_loop",
    )

    # Step 8: 注入 boss 龙虾（Phase 4 PvP / 挑战 boss 入口）
    # 必须在 _load_initial 之后跑，让预设覆盖 SQLite 里可能存在的旧 boss 副本
    try:
        injected = await bot_manager.ensure_bosses(STATE)
        logger.info("启动: ensure_bosses 注入 %d 只 boss", injected)
    except Exception as exc:
        logger.error("启动: ensure_bosses 失败: %s", exc, exc_info=True)
        raise

    yield

    logger.info("关闭龙虾斗兽场...")
    for task in list(STATE.claim_tasks.values()):
        task.cancel()
    if STATE.bot_maintenance_task is not None and not STATE.bot_maintenance_task.done():
        STATE.bot_maintenance_task.cancel()
        try:
            await STATE.bot_maintenance_task
        except asyncio.CancelledError:
            pass
    if STATE.pool is not None:
        await STATE.pool.stop_all()
    if STATE.http is not None and not STATE.http.closed:
        await STATE.http.close()
    db.close_db()


app = FastAPI(title="龙虾斗兽场", lifespan=lifespan)


# ============ API ============


@app.post("/api/claim")
async def claim_new() -> Dict[str, Any]:
    """发起一次「认领龙虾」：返回一张全新的二维码。"""
    assert STATE.http is not None
    try:
        qrcode_value, qrcode_url = await wx.fetch_qrcode(STATE.http)
    except Exception as exc:
        logger.error("fetch_qrcode 失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"拉取二维码失败：{exc}")
    sid = secrets.token_urlsafe(12)
    claim = ClaimAttempt(
        sid=sid,
        qrcode_value=qrcode_value,
        qrcode_url=qrcode_url,
        base_url=wx.ILINK_BASE_URL,
    )
    STATE.claims[sid] = claim
    STATE.claim_tasks[sid] = asyncio.create_task(_qr_status_task(sid), name=f"qr-{sid[:6]}")
    logger.info("new claim sid=%s", sid)
    return {
        "sid": sid,
        "qr_image_url": f"/api/qr/{sid}.png",
        "qr_scan_url": qrcode_url,
        "status": claim.status,
        "tagline": random.choice(content.CLAIM_TEASERS),
    }


@app.get("/api/claim/{sid}")
async def claim_status(sid: str) -> Dict[str, Any]:
    """查询某次认领的状态。"""
    claim = STATE.claims.get(sid)
    if claim is None:
        raise HTTPException(status_code=404, detail="claim 不存在或已被回收")
    return {
        "sid": claim.sid,
        "status": claim.status,
        "lobster_name": claim.lobster_name,
        "refresh_count": claim.refresh_count,
        "error": claim.error,
        "qr_image_url": f"/api/qr/{sid}.png",
        "qr_scan_url": claim.qrcode_url,
    }


@app.get("/api/qr/{sid}.png")
async def qr_image(sid: str) -> Response:
    """把 claim 当前的二维码 URL 渲染成 PNG。"""
    claim = STATE.claims.get(sid)
    if claim is None:
        raise HTTPException(status_code=404, detail="claim 不存在")
    if not claim.qrcode_url:
        raise HTTPException(status_code=500, detail="二维码内容为空")
    img = qrcode.make(claim.qrcode_url, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/leaderboard")
async def leaderboard() -> List[Dict[str, Any]]:
    """全平台龙虾名气榜。

    Phase 4：BOSS（bot_kind="boss"）不是玩家，不进玩家榜单；普通 bot 玩家视角即玩家，照常入榜。
    `is_bot` 字段保留输出（前端 / 调试用），但不暴露 bot_kind。
    """
    eligible = [l for l in STATE.lobsters.values() if l.bot_kind != "boss"]
    sorted_list = sorted(
        eligible,
        key=lambda l: (l.fame, l.wins, l.level),
        reverse=True,
    )[:20]
    return [
        {
            "name": l.name,
            "level": l.level,
            "stage": l.stage(),
            "wins": l.wins,
            "losses": l.losses,
            "fame": l.fame,
            "coins": l.coins,
            "personality": l.personality,
            "titles": l.titles,
            "morale_label": l.morale_label(),
            "is_bot": l.is_bot,
        }
        for l in sorted_list
    ]


@app.get("/api/feed")
async def feed_endpoint(limit: int = 20) -> List[Dict[str, Any]]:
    """最近的战报流。"""
    items = STATE.feed[-limit:]
    return list(reversed(items))


@app.get("/api/battles")
async def battles_endpoint(user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """某个玩家参与过（作为挑战者或对手）的最近 N 场战斗，**含完整 narration**。

    Phase 6：前端做战报详情页用。微信侧不要直接读这个接口（数据量大），
    走 get_battle_history 工具拿精简摘要。

    返回字段：
    - id / ts / 标志位（is_pvp / is_clutch / is_upset）
    - challenger_uid / opponent_uid / winner_uid
    - rewards_meta（含双方名字 / 胜方名字 / 结束回合 / exp / coins / fame）
    - narration（完整多回合战报字符串）
    """
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id 不能为空")
    limit = max(1, min(int(limit), 100))
    rows = await storage.load_battles_for_user(user_id, limit=limit)
    logger.info("/api/battles uid=%s 返回 %d 场", user_id[:8], len(rows))
    return rows


@app.get("/api/stats")
async def stats() -> Dict[str, Any]:
    """大盘统计。

    - lobster_count: 全体龙虾数（含人机）
    - npc_count:     人机龙虾数（is_bot=True，Phase 3 引入的种子陪练池）
    - active_bots / dead_bots: 指 ilink wechat 长轮询 session（与人机龙虾无关）
    """
    if STATE.pool is None:
        active = 0
        dead = 0
    else:
        active = sum(1 for s in STATE.pool.sessions.values() if not s.dead)
        dead = sum(1 for s in STATE.pool.sessions.values() if s.dead)
    npc_count = sum(1 for l in STATE.lobsters.values() if l.is_bot)
    return {
        "lobster_count": len(STATE.lobsters),
        "npc_count": npc_count,
        "battle_count": sum(l.wins + l.losses for l in STATE.lobsters.values()),
        "active_bots": active,
        "dead_bots": dead,
        "live_claims": sum(
            1 for c in STATE.claims.values() if c.status in ("wait", "scaned", "confirmed")
        ),
    }


# ============ 前端静态托管 ============

# 静态文件目录：从 app/api/main.py 上溯两层到项目根的 static/
STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/battles")
async def battles_page() -> FileResponse:
    """Phase 6 战斗历史详情页（独立页面）。

    通过 /battles?user_id=<uid>&limit=20 访问，前端 JS 自行从 query string
    读取 user_id 并调 /api/battles 拉数据。不和主页面关联，也不暴露入口。
    """
    return FileResponse(STATIC_DIR / "battles.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
