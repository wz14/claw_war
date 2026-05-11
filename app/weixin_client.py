"""精简版微信 iLink Bot 客户端。

仅保留龙虾对战平台需要的能力：
1. 拉取登录二维码（每次「认领龙虾」生成一张新的）
2. 轮询二维码状态（等候用户在微信里扫码并确认）
3. 长轮询接收消息（每只「龙虾 = 一个 bot」自己跑一份长轮询）
4. 发送文本消息（bot 回复）

设计依据：参考 /Users/yufengzhang/Workplace/hermes-agent/gateway/platforms/weixin.py。
不做 fallback：能报错的地方直接抛，调用方记日志即可。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import ssl
import struct
from typing import Any, Dict, Optional, Tuple

import aiohttp
import certifi

logger = logging.getLogger(__name__)

# ============ 协议常量 ============

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

# 端点
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

# 超时
LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
QR_TIMEOUT_MS = 35_000

# 消息体类型
ITEM_TEXT = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

# 错误码
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2


# ============ HTTP 工具 ============


def make_ssl_connector() -> aiohttp.TCPConnector:
    """使用 certifi 的 CA bundle 创建 TCPConnector。

    Tencent 的 ilinkai.weixin.qq.com 在部分系统 CA 上验证不过，
    强制走 certifi 才能稳。
    """
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


def _random_wechat_uin() -> str:
    """生成一次性 X-WECHAT-UIN 头。iLink 要求每次请求都换。"""
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    """构造 iLink API 标准头。"""
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def _api_post(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    token: Optional[str],
    timeout_ms: int,
) -> Dict[str, Any]:
    body = _json_dumps({**payload, "base_info": {"channel_version": CHANNEL_VERSION}})
    url = f"{base_url.rstrip('/')}/{endpoint}"
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.post(url, data=body, headers=_headers(token, body), timeout=timeout) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"iLink POST {endpoint} HTTP {response.status}: {raw[:200]}")
        return json.loads(raw)


async def _api_get(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.get(url, headers=headers, timeout=timeout) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {response.status}: {raw[:200]}")
        return json.loads(raw)


# ============ 二维码相关 ============


async def fetch_qrcode(session: aiohttp.ClientSession, bot_type: str = "3") -> Tuple[str, str]:
    """获取一张新的登录二维码。

    返回 (qrcode_value, qrcode_url)：
    - qrcode_value: 用于后续状态轮询的 hex token
    - qrcode_url:   一个 weixin://lite/... 完整可扫 URL（前端要把它渲染成二维码）
    """
    resp = await _api_get(
        session,
        base_url=ILINK_BASE_URL,
        endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
        timeout_ms=QR_TIMEOUT_MS,
    )
    qrcode_value = str(resp.get("qrcode") or "")
    qrcode_url = str(resp.get("qrcode_img_content") or "")
    if not qrcode_value:
        raise RuntimeError(f"iLink 没有返回 qrcode 字段：{resp}")
    logger.info("weixin: 获取到二维码 token=%s url=%s", qrcode_value[:8], qrcode_url[:40])
    return qrcode_value, qrcode_url


async def poll_qrcode_status(
    session: aiohttp.ClientSession,
    *,
    qrcode_value: str,
    base_url: str = ILINK_BASE_URL,
) -> Dict[str, Any]:
    """单次拉取二维码状态。

    status 取值：
    - wait                  : 等待扫码
    - scaned                : 已扫码，等待用户在微信里确认
    - scaned_but_redirect   : 已扫码，需要切到新的 host（响应里带 redirect_host）
    - confirmed             : 已确认（含 ilink_bot_id / bot_token / baseurl / ilink_user_id）
    - expired               : 已过期，需要重新拉一张
    """
    return await _api_get(
        session,
        base_url=base_url,
        endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
        timeout_ms=QR_TIMEOUT_MS,
    )


# ============ 消息收发 ============


async def get_updates(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    sync_buf: str,
    timeout_ms: int = LONG_POLL_TIMEOUT_MS,
) -> Dict[str, Any]:
    """长轮询拉取新消息。

    iLink 的 long-poll 会一直 hang 到有新消息或者超时；超时返回空 msgs 即可。
    """
    try:
        return await _api_post(
            session,
            base_url=base_url,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=timeout_ms,
        )
    except asyncio.TimeoutError:
        logger.debug("weixin: long-poll 超时，正常现象")
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def send_text(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    text: str,
    context_token: Optional[str] = None,
) -> Dict[str, Any]:
    """发送一条纯文本消息。

    iLink 推荐每次回复都带上对方最新的 context_token；
    没有的话也可以发，但属于「降级模式」，可能不太稳。
    """
    if not text or not text.strip():
        raise ValueError("send_text: text 不能为空")
    client_id = f"claw-{secrets.token_hex(8)}"
    message: Dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    resp = await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={"msg": message},
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )
    ret = resp.get("ret", 0)
    errcode = resp.get("errcode", 0)
    if ret not in (0, None) or errcode not in (0, None):
        errmsg = resp.get("errmsg") or resp.get("msg") or "unknown"
        raise RuntimeError(f"iLink sendmessage 失败 ret={ret} errcode={errcode} errmsg={errmsg}")
    return resp


def extract_text_from_message(msg: Dict[str, Any]) -> str:
    """从一条入站消息中拎出纯文本。其他媒体类型本游戏不需要。"""
    for item in msg.get("item_list") or []:
        if item.get("type") == ITEM_TEXT:
            return str((item.get("text_item") or {}).get("text") or "")
    return ""
