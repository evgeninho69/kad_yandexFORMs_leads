"""Telegram notifier — best-effort. Failure to notify never blocks the webhook."""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


def notify(text: str) -> None:
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_OWNER_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.info("TG notify skipped (no token/chat_id)")
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
        if response.status_code >= 400:
            logger.warning("TG notify failed: %s %s", response.status_code, response.text[:200])
        else:
            logger.info("TG notify sent")
    except Exception as exc:  # noqa: BLE001
        logger.warning("TG notify exception: %s", exc)
