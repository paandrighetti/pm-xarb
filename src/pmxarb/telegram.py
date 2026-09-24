"""Minimal Telegram Bot API sender. Silent no-op when the token or chat id is missing."""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


async def send(text: str, enabled: bool = True) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not enabled or not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = text[:3900]
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
            if r.status_code >= 400:
                log.warning("telegram %s: %s", r.status_code, r.text[:200])
                return False
        return True
    except httpx.HTTPError as exc:
        log.warning("telegram failed: %s", exc)
        return False
