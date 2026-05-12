"""每只龙虾对应一个 iLink Bot session 的调度器。

为什么这么设计：
- 用户扫一次「认领龙虾」二维码 → iLink 创建一个新 bot，bot 自动成为该用户的微信好友
- 后端拿到 bot 凭证后，启动一个长轮询协程，专门收发这个用户的消息
- 一只龙虾 ↔ 一对 (bot_token, user_id) ↔ 一个 poll_task

这是黑客松的取巧方案：每次扫码 = 一个新 bot，不需要多用户共用同一个 bot。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

import aiohttp

from . import weixin_client as wx

logger = logging.getLogger(__name__)


# 入站消息回调签名： (bot, user_id, text, context_token) -> awaitable[reply_text or None]
InboundCallback = Callable[["BotSession", str, str, Optional[str]], Awaitable[Optional[str]]]


@dataclass
class BotSession:
    """一个已激活的 iLink bot 会话。"""

    account_id: str            # ilink_bot_id (xxx@im.bot)
    token: str
    base_url: str
    user_id: str               # 扫码方的 ilink_user_id（也是 chat 对端）

    sync_buf: str = ""
    context_token: str = ""
    last_seen_at: float = field(default_factory=time.time)
    running: bool = True
    # 是否已经发送过欢迎语（介绍玩法的开场白）
    # 扫码确认时会主动发一次，主动发成功置 True；失败则下次玩家入站补发
    welcomed: bool = False
    # 失活标记：连续 SESSION_EXPIRED_ERRCODE 后自动停 poll，避免无限刷屏
    dead: bool = False
    dead_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "token": self.token,
            "base_url": self.base_url,
            "user_id": self.user_id,
            "sync_buf": self.sync_buf,
            "context_token": self.context_token,
            "welcomed": self.welcomed,
            "dead": self.dead,
            "dead_reason": self.dead_reason,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BotSession":
        return cls(
            account_id=d["account_id"],
            token=d["token"],
            base_url=d.get("base_url", wx.ILINK_BASE_URL),
            user_id=d["user_id"],
            sync_buf=d.get("sync_buf", ""),
            context_token=d.get("context_token", ""),
            welcomed=bool(d.get("welcomed", False)),
            dead=bool(d.get("dead", False)),
            dead_reason=str(d.get("dead_reason", "")),
        )


# 长轮询的失活判定阈值
# 连续命中 errcode=-14 (session timeout) 这么多次就视为这只 bot 凭证已死，
# 直接停掉它的 poll 循环并打 fatal log。后续要重新认领得用户重新扫码。
DEAD_SESSION_THRESHOLD_ERRCODE_14 = 5

# 普通错误最大退避秒数（指数退避封顶）
MAX_BACKOFF_SECONDS = 60


class BotPool:
    """管理所有 bot session 的生命周期。"""

    def __init__(self, http: aiohttp.ClientSession, inbound_cb: InboundCallback):
        self._http = http
        self._inbound_cb = inbound_cb
        self._sessions: Dict[str, BotSession] = {}        # user_id -> session
        self._tasks: Dict[str, asyncio.Task] = {}         # user_id -> poll task
        # session_died 回调：当某个 session 被判失活时触发，让上层做持久化等收尾
        # 没注册就什么也不做（不写隐式 fallback：调用方自己决定要不要订阅）
        self._on_session_died: Optional[Callable[[BotSession], Awaitable[None]]] = None

    def set_on_session_died(
        self, cb: Callable[[BotSession], Awaitable[None]],
    ) -> None:
        """注册 session 失活的收尾回调（持久化、通知等）。"""
        self._on_session_died = cb

    @property
    def sessions(self) -> Dict[str, BotSession]:
        return self._sessions

    def get_by_user(self, user_id: str) -> Optional[BotSession]:
        return self._sessions.get(user_id)

    async def register_and_start(self, session: BotSession) -> None:
        """登记一个 bot session 并启动它的长轮询循环。

        如果传进来的 session 在持久化里已经被标记为 dead，就只登记不启动 poll，
        避免一启动就刷一堆 errcode=-14。
        """
        prev = self._sessions.get(session.user_id)
        if prev is not None:
            logger.info("bot_pool: 替换 user=%s 的旧 session", session.user_id[:8])
            await self.stop(prev.user_id)
        self._sessions[session.user_id] = session
        if session.dead:
            logger.warning(
                "bot_pool: 跳过 dead session bot=%s user=%s reason=%s",
                session.account_id[:12], session.user_id[:8], session.dead_reason,
            )
            return
        task = asyncio.create_task(self._poll_loop(session), name=f"poll-{session.user_id[:8]}")
        self._tasks[session.user_id] = task
        logger.info(
            "bot_pool: 启动 bot=%s user=%s base=%s",
            session.account_id[:12], session.user_id[:8], session.base_url,
        )

    async def stop(self, user_id: str) -> None:
        session = self._sessions.pop(user_id, None)
        if session is not None:
            session.running = False
        task = self._tasks.pop(user_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def stop_all(self) -> None:
        for uid in list(self._sessions.keys()):
            await self.stop(uid)

    async def send(self, user_id: str, text: str) -> None:
        """向某个 user 发送文本消息。"""
        session = self._sessions.get(user_id)
        if session is None:
            raise RuntimeError(f"bot_pool: 没有 user_id={user_id[:8]} 的活跃 session")
        await wx.send_text(
            self._http,
            base_url=session.base_url,
            token=session.token,
            to_user_id=session.user_id,
            text=text,
            context_token=session.context_token or None,
        )
        logger.info("bot_pool: 向 %s 发送 %d 字", session.user_id[:8], len(text))

    async def _poll_loop(self, session: BotSession) -> None:
        """对单个 bot 的长轮询循环。

        - 收到消息后调用 inbound_cb 处理
        - 把回调返回的回复文本通过 send_text 发回去
        - 顺手刷新 context_token 和 sync_buf
        - 出现连续 SESSION_EXPIRED_ERRCODE(-14) 多次则判定凭证已死，
          停掉 poll 并回调 _on_session_died 让上层持久化 dead 状态
        """
        consecutive_failures = 0
        consecutive_session_expired = 0
        while session.running:
            try:
                resp = await wx.get_updates(
                    self._http,
                    base_url=session.base_url,
                    token=session.token,
                    sync_buf=session.sync_buf,
                )
                ret = resp.get("ret", 0)
                errcode = resp.get("errcode", 0)
                if ret not in (0, None) or errcode not in (0, None):
                    consecutive_failures += 1
                    if errcode == wx.SESSION_EXPIRED_ERRCODE:
                        consecutive_session_expired += 1
                        if consecutive_session_expired >= DEAD_SESSION_THRESHOLD_ERRCODE_14:
                            await self._mark_dead(
                                session,
                                reason=(
                                    f"连续 {consecutive_session_expired} 次 errcode=-14 "
                                    f"(session timeout)，bot 凭证已失效"
                                ),
                            )
                            return
                    else:
                        # 其它错误清零 expire 计数，但走指数退避
                        consecutive_session_expired = 0
                    logger.warning(
                        "bot_pool: %s getUpdates 错误 ret=%s errcode=%s errmsg=%s "
                        "(失败 %d 次, expire %d 次)",
                        session.user_id[:8], ret, errcode, resp.get("errmsg"),
                        consecutive_failures, consecutive_session_expired,
                    )
                    await asyncio.sleep(min(MAX_BACKOFF_SECONDS, 2 ** consecutive_failures))
                    continue

                consecutive_failures = 0
                consecutive_session_expired = 0
                new_buf = str(resp.get("get_updates_buf") or "")
                if new_buf:
                    session.sync_buf = new_buf

                for msg in resp.get("msgs") or []:
                    await self._handle_msg(session, msg)
            except asyncio.CancelledError:
                logger.info("bot_pool: %s poll loop cancelled", session.user_id[:8])
                break
            except Exception as exc:
                consecutive_failures += 1
                logger.error(
                    "bot_pool: %s poll loop 异常 (%d): %s",
                    session.user_id[:8], consecutive_failures, exc,
                )
                await asyncio.sleep(min(MAX_BACKOFF_SECONDS, 2 ** consecutive_failures))

    async def _mark_dead(self, session: BotSession, *, reason: str) -> None:
        """把一个 session 标记为失活：停 poll、记录原因、调用收尾回调。"""
        session.dead = True
        session.dead_reason = reason
        session.running = False
        logger.error(
            "bot_pool: 🪦 session 失活 bot=%s user=%s 原因: %s",
            session.account_id[:12], session.user_id[:8], reason,
        )
        # 主动从 task 表里清掉，避免 stop_all 时再 cancel 自己
        self._tasks.pop(session.user_id, None)
        if self._on_session_died is not None:
            try:
                await self._on_session_died(session)
            except Exception as exc:
                logger.error("bot_pool: on_session_died 回调异常: %s", exc, exc_info=True)

    async def _handle_msg(self, session: BotSession, msg: Dict) -> None:
        sender = str(msg.get("from_user_id") or "").strip()
        if not sender:
            return
        # bot 自己发的回声不处理
        if sender == session.account_id:
            return

        text = wx.extract_text_from_message(msg)
        context_token = str(msg.get("context_token") or "").strip()
        if context_token:
            session.context_token = context_token

        session.last_seen_at = time.time()
        if not text:
            return

        logger.info(
            "bot_pool: inbound from=%s len=%d preview=%s",
            sender[:8], len(text), text[:30].replace("\n", " "),
        )

        try:
            reply = await self._inbound_cb(session, sender, text, context_token or None)
        except Exception as exc:
            logger.error("bot_pool: inbound_cb 抛错: %s", exc, exc_info=True)
            reply = "（系统出了点问题，龙虾正在找借口。请稍等再试一次。）"

        if reply:
            try:
                await wx.send_text(
                    self._http,
                    base_url=session.base_url,
                    token=session.token,
                    to_user_id=sender,
                    text=reply,
                    context_token=session.context_token or None,
                )
            except Exception as exc:
                logger.error("bot_pool: 发送回复失败 to=%s: %s", sender[:8], exc)


def session_from_qr_confirmation(status_resp: Dict) -> BotSession:
    """把 iLink QR 'confirmed' 状态响应转成 BotSession。"""
    account_id = str(status_resp.get("ilink_bot_id") or "").strip()
    token = str(status_resp.get("bot_token") or "").strip()
    base_url = str(status_resp.get("baseurl") or wx.ILINK_BASE_URL).strip().rstrip("/")
    user_id = str(status_resp.get("ilink_user_id") or "").strip()
    if not account_id or not token or not user_id:
        raise RuntimeError(f"QR confirmed 但凭证残缺: {status_resp}")
    return BotSession(
        account_id=account_id,
        token=token,
        base_url=base_url,
        user_id=user_id,
    )
