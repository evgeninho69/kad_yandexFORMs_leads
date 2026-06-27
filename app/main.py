"""FastAPI entry point for the 2KAD Yandex Forms -> Bitrix webhook bridge.

Endpoints:
  GET  /healthz                liveness
  POST /webhook/yandex         Yandex Forms answer.created handler
  POST /webhook/test           manual smoke test (JSON body shaped like Yandex payload)

The service creates deals in the unified 2KAD funnel (CATEGORY_ID=3) for
every form submission. Folder creation on D:\ is handled by a separate
cron running on the 2KAD server (see skills/2kad-yandex-form).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

from app.bitrix import BitrixClient, BitrixError, from_env as bitrix_from_env
from app.notify import notify as tg_notify
from app.yandex_parser import build_deal_fields, normalise_payload

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("kad_yandexFORMs_leads")

app = FastAPI(
    title="kad_yandexFORMs_leads",
    version="0.1.0",
    description="Yandex Forms answer.created -> Bitrix24 deal.add (CATEGORY_ID=3)",
)


def _bitrix() -> BitrixClient:
    return bitrix_from_env()


def _funnel_id() -> int:
    return int(os.environ.get("BITRIX_FUNNEL_ID", "3"))


def _responsible_id() -> int:
    return int(os.environ.get("BITRIX_RESPONSIBLE_ID", "1"))


def _verify_secret(secret_header: str | None) -> None:
    expected = os.environ.get("WEBHOOK_SHARED_SECRET", "").strip()
    if not expected:
        return
    if not secret_header or secret_header != expected:
        raise HTTPException(status_code=401, detail="invalid webhook secret")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "service": "kad_yandexFORMs_leads", "funnel": _funnel_id()}


@app.post("/webhook/yandex")
async def webhook_yandex(
    request: Request,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict[str, Any]:
    """Handle Yandex Forms `answer.created` webhook.

    Yandex retries on non-2xx. We always aim for 200 unless the payload is
    unusable — Bitrix errors are logged and surfaced but still return 200
    so Yandex stops retrying, otherwise we'd spam.
    """
    _verify_secret(x_webhook_secret)
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("non-JSON payload: %s", exc)
        raise HTTPException(status_code=400, detail="body must be JSON")

    logger.info("yandex webhook: keys=%s", sorted(payload.keys()))
    parsed = normalise_payload(payload)
    logger.info("parsed: %s", parsed)

    fields = build_deal_fields(
        parsed,
        funnel_id=_funnel_id(),
        responsible_id=_responsible_id(),
    )
    logger.info("deal fields: %s", fields)

    try:
        with _bitrix() as bx:
            deal_id = bx.crm_deal_add(fields)
            logger.info("deal created: id=%s", deal_id)
    except BitrixError as exc:
        logger.exception("Bitrix error")
        # Notify owner; still return 200 to stop Yandex retries on a known-bad
        # shape — re-dispatch handled by the folder-watcher cron, not Yandex.
        tg_notify(f"[kad_yandexFORMs_leads] ❌ Bitrix error: {exc}\nPayload keys: {sorted(payload.keys())}")
        return {"ok": False, "error": str(exc), "parsed_keys": list(parsed.keys())}

    # Best-effort: publish full request data into deal timeline (works on
    # on-prem Bitrix; im.* API unavailable, crm.timeline.comment.add is the
    # correct path — verified 2026-06-18, see 2kad-bitrix-start-project notes).
    try:
        with _bitrix() as bx:
            # 1) Compact header with metadata.
            bx.crm_timeline_comment_add(
                entity_type="deal",
                entity_id=deal_id,
                comment=(
                    f"Заявка из Яндекс Формы\n"
                    f"Survey: {parsed.get('_survey_id', '?')}\n"
                    f"Event: {parsed.get('_event', 'answer.created')}\n"
                    f"Submitted: {parsed.get('_submitted_at', '?')}\n"
                    f"--- Полные данные заявки ---\n"
                    f"{fields.get('COMMENTS', '(нет данных)')}"
                ),
            )
            # 2) Add a task to fetch documents (matches the C3:NEW stage
            # semantics — "Получить документы").
            bx.call(
                "crm.todo.add",
                {
                    "fields": {
                        "OWNER_ID": deal_id,
                        "OWNER_TYPE": "D",
                        "TITLE": "Запросить документы у заказчика",
                        "DESCRIPTION": (
                            f"Заявка #{deal_id} с Яндекс Формы. "
                            f"Связаться с {parsed.get('fio', '—')}, тел. "
                            f"{parsed.get('phone', '—')}, e-mail "
                            f"{parsed.get('email', '—')}."
                        ),
                    }
                },
            )
    except BitrixError as exc:
        logger.warning("timeline comment / todo failed (non-fatal): %s", exc)

    tg_notify(
        f"[kad_yandexFORMs_leads] ✅ Новая заявка → сделка #{deal_id}\n"
        f"Заголовок: {fields.get('TITLE', '?')}\n"
        f"ФИО: {parsed.get('fio', '—')}\n"
        f"Телефон: {parsed.get('phone', '—')}"
    )

    return {
        "ok": True,
        "deal_id": deal_id,
        "title": fields.get("TITLE"),
        "parsed_keys": list(parsed.keys()),
    }


@app.post("/webhook/test")
async def webhook_test(payload: dict[str, Any]) -> dict[str, Any]:
    """Dry-run: parse + show what we'd send to Bitrix, without creating a deal."""
    parsed = normalise_payload(payload)
    fields = build_deal_fields(
        parsed,
        funnel_id=_funnel_id(),
        responsible_id=_responsible_id(),
    )
    return {
        "ok": True,
        "dry_run": True,
        "parsed": parsed,
        "deal_fields": fields,
    }
