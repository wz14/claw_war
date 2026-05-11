"""龙虾斗兽场 FastAPI 主入口。

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
"""

from __future__ import annotations

# 在所有 import 之前加载 .env，让 ChatOpenAI 等模块能读到环境变量。
# 不写 fallback：如果项目里有 .env 就加载；没有就直接走系统 env。
from dotenv import load_dotenv as _load_dotenv  # noqa: E402

_load_dotenv()

import asyncio
import base64
import io
import logging
import random
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import qrcode
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import ai_handler, bot_pool, content, game, storage
from . import weixin_client as wx
from .bot_pool import BotPool, BotSession, session_from_qr_confirmation

logger = logging.getLogger(__name__)

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
    lobsters: Dict[str, game.Lobster] = field(default_factory=dict)
    pool: Optional[BotPool] = None
    feed: List[Dict[str, Any]] = field(default_factory=list)
    persist_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ai: Optional[ai_handler.AIHandler] = None


STATE = AppState()


# ============ 持久化辅助 ============


def _persist_lobsters_sync_only() -> Dict[str, Dict[str, Any]]:
    return {uid: l.to_dict() for uid, l in STATE.lobsters.items()}


async def _persist_all() -> None:
    """统一落盘。"""
    async with STATE.persist_lock:
        await storage.save_lobsters(_persist_lobsters_sync_only())
        if STATE.pool is not None:
            await storage.save_bots({uid: s.to_dict() for uid, s in STATE.pool.sessions.items()})
        await storage.save_feed(STATE.feed)


def _load_initial() -> None:
    """启动时把磁盘上的旧数据加载回内存。"""
    for uid, raw in storage.load_lobsters().items():
        try:
            STATE.lobsters[uid] = game.Lobster.from_dict(raw)
        except Exception as exc:
            logger.warning("load lobster %s failed: %s", uid[:8], exc)
    STATE.feed = storage.load_feed()
    logger.info(
        "启动加载完成：龙虾 %d 只，战报 %d 条", len(STATE.lobsters), len(STATE.feed),
    )


# ============ 入站消息处理 ============


async def on_inbound(
    session: BotSession,
    sender: str,
    text: str,
    context_token: Optional[str],
) -> Optional[str]:
    """长轮询收到一条用户消息时的回调。

    流程：
    1. 确保该玩家有一只龙虾（没就现造）
    2. 如果 bot session 还没发过欢迎语（扫码确认时主动发可能失败），
       这次入站就把欢迎语补发一次，介绍清楚斗兽场玩法
    3. 把消息交给 ai_handler 处理。AI 会自己决定调哪个 tool。
       tool 内部会读/改 Lobster，对应的"游戏判定结果"原样返回给 AI，
       AI 再戏剧化包装。
    4. 持久化。
    """
    if STATE.ai is None:
        return "（AI 主持人还没起来，请先配置 OPENAI_API_KEY 再重试。）"

    lobster = STATE.lobsters.get(sender)
    if lobster is None:
        lobster = game.create_lobster(user_id=sender)
        STATE.lobsters[sender] = lobster
        logger.info("on_inbound: 为新玩家 %s 即时创建龙虾 %s", sender[:8], lobster.name)

    reply_parts: List[str] = []
    if not session.welcomed:
        welcome = content.WELCOME_TEMPLATE.format(
            name=lobster.name,
            breed=lobster.breed,
            personality=lobster.personality,
            claw=lobster.claw,
            shell=lobster.shell,
            speed=lobster.speed,
            stamina=lobster.stamina,
            luck=lobster.luck,
            skills="、".join(lobster.skills),
        )
        reply_parts.append(welcome)
        session.welcomed = True
        logger.info("on_inbound: 补发欢迎语给 %s（首次入站）", sender[:8])
        for claim in STATE.claims.values():
            if claim.user_id == sender and not claim.lobster_name:
                claim.lobster_name = lobster.name
                claim.status = "lobster_ready"

    try:
        ai_reply = await STATE.ai.handle(sender, text)
    except Exception as exc:
        logger.error("on_inbound: AI 主持人异常 uid=%s: %s", sender[:8], exc, exc_info=True)
        ai_reply = "（兽场解说嗓子哑了，喝口水再来。请稍后再发一次。）"

    if ai_reply:
        reply_parts.append(ai_reply)

    await _persist_all()
    return "\n\n———\n\n".join(reply_parts) if reply_parts else None


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
            # 尝试主动发一条欢迎语（首次发不一定通，没关系）
            existing = STATE.lobsters.get(bot_session.user_id)
            if existing is None:
                lobster = game.create_lobster(user_id=bot_session.user_id)
                STATE.lobsters[bot_session.user_id] = lobster
            else:
                lobster = existing
            claim.lobster_name = lobster.name
            welcome = content.WELCOME_TEMPLATE.format(
                name=lobster.name,
                breed=lobster.breed,
                personality=lobster.personality,
                claw=lobster.claw,
                shell=lobster.shell,
                speed=lobster.speed,
                stamina=lobster.stamina,
                luck=lobster.luck,
                skills="、".join(lobster.skills),
            )
            try:
                await STATE.pool.send(bot_session.user_id, welcome)
                bot_session.welcomed = True
                claim.status = "lobster_ready"
                logger.info("welcome message sent to %s", bot_session.user_id[:8])
            except Exception as exc:
                # 主动发不一定通：session.welcomed 维持 False，
                # 等玩家在微信里主动说第一句话时由 on_inbound 补发
                logger.warning(
                    "主动欢迎发送失败（已标记 welcomed=False，下次入站会补发）：%s", exc,
                )
                claim.status = "lobster_ready"
            await _persist_all()
            return
        await asyncio.sleep(1.5)

    # 超时
    if claim.status not in ("lobster_ready", "confirmed"):
        claim.status = "expired"
        claim.error = "等候超时，请重新认领。"


# ============ FastAPI 生命周期 ============


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("启动龙虾斗兽场...")
    STATE.http = aiohttp.ClientSession(
        trust_env=True,
        connector=wx.make_ssl_connector(),
    )
    _load_initial()
    STATE.pool = BotPool(STATE.http, inbound_cb=on_inbound)

    # 初始化 AI 主持人。没配 OPENAI_API_KEY 就直接报错并继续——
    # bot 端会回提示让用户去配。这是允许"前端能用、bot 暂时回报错"的折衷，
    # 而不是写隐式 fallback 让玩家以为一切正常。
    try:
        STATE.ai = ai_handler.AIHandler(STATE)
    except Exception as exc:
        logger.error("AIHandler 初始化失败：%s", exc)
        STATE.ai = None

    # 恢复历史 bot session（如果有的话）→ 重启长轮询
    for uid, raw in storage.load_bots().items():
        try:
            sess = BotSession.from_dict(raw)
            await STATE.pool.register_and_start(sess)
        except Exception as exc:
            logger.warning("恢复 bot session %s 失败: %s", uid[:8], exc)

    yield

    logger.info("关闭龙虾斗兽场...")
    for task in list(STATE.claim_tasks.values()):
        task.cancel()
    if STATE.pool is not None:
        await STATE.pool.stop_all()
    if STATE.http is not None and not STATE.http.closed:
        await STATE.http.close()


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
    # 加 cache-buster 友好型响应头
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/leaderboard")
async def leaderboard() -> List[Dict[str, Any]]:
    """全平台龙虾名气榜。"""
    sorted_list = sorted(
        STATE.lobsters.values(),
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
        }
        for l in sorted_list
    ]


@app.get("/api/feed")
async def feed_endpoint(limit: int = 20) -> List[Dict[str, Any]]:
    """最近的战报流。"""
    items = STATE.feed[-limit:]
    return list(reversed(items))


@app.get("/api/stats")
async def stats() -> Dict[str, Any]:
    """大盘统计。"""
    return {
        "lobster_count": len(STATE.lobsters),
        "battle_count": sum(l.wins + l.losses for l in STATE.lobsters.values()),
        "active_bots": len(STATE.pool.sessions) if STATE.pool else 0,
        "live_claims": sum(
            1 for c in STATE.claims.values() if c.status in ("wait", "scaned", "confirmed")
        ),
    }


# ============ 前端静态托管 ============

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# 静态资源（如果以后加 css/js 单独文件，会自动 serve）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
