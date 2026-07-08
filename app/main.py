"""FastAPI entry point for the 2KAD Yandex Forms -> Bitrix webhook bridge.

Endpoints:
  GET  /healthz                liveness
  POST /webhook/yandex         Wizard form submission handler (multipart)
  POST /webhook/test           manual smoke test (JSON body shaped like Yandex payload)

The service creates deals in the unified 2KAD funnel (CATEGORY_ID=3) for
every form submission. Folder creation on D:\\ is handled by a separate
cron running on the 2KAD server (see skills/2kad-yandex-form).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.bitrix import BitrixClient, BitrixError, from_env as bitrix_from_env
from app.email_smtp import send_customer_reply
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
    version="0.2.0",
    description="Wizard form -> Bitrix24 deal.add (CATEGORY_ID=3) + contact/company + chat + files",
)

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "https://kad-yandexforms-wizard.dev.ii4ki.ru,https://kad-yandexFORMs-wizard.dev.ii4ki.ru,"
        "https://kad-yandexforms-leads.dev.ii4ki.ru,https://kad-yandexFORMs-leads.dev.ii4ki.ru",
    ).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Webhook-Secret"],
    expose_headers=["*"],
    max_age=3600,
)


def _bitrix() -> BitrixClient:
    return bitrix_from_env()


def _funnel_id() -> int:
    return int(os.environ.get("BITRIX_FUNNEL_ID", "3"))


def _responsible_id() -> int:
    return int(os.environ.get("BITRIX_RESPONSIBLE_ID", "1"))


def _chat_id() -> str | None:
    """ID чата «Запуск новых проектов» в Bitrix24.

    Im messenger в Bitrix24 использует CHAT_ID (числовой), но
    im.message.add принимает DIALOG_ID в формате "chat<id>".
    По умолчанию 0 — чат будет создан автоматически Битриксом,
    либо его можно переопределить через env CHAT_ID.
    """
    return os.environ.get("BITRIX_NEW_PROJECT_CHAT_ID", "").strip() or None


def _disk_root_folder_id() -> int:
    """ID корневой папки в Битрикс.Диске, куда сохранять файлы заявок.

    0 = диск текущего пользователя (по умолчанию).
    """
    return int(os.environ.get("BITRIX_DISK_ROOT_FOLDER_ID", "0"))


def _verify_secret(secret_header: str | None) -> None:
    expected = os.environ.get("WEBHOOK_SHARED_SECRET", "").strip()
    if not expected:
        return
    if not secret_header or secret_header != expected:
        raise HTTPException(status_code=401, detail="invalid webhook secret")


def _is_legal_entity(customer_type: str) -> bool:
    """True если заказчик — ЮЛ или ИП (нужна компания)."""
    return bool(re.search(r"юр|ип|ооо|зао|оао|ао|предприним", customer_type or "", re.IGNORECASE))


def _normalise_phone(raw: str) -> str:
    """Нормализуем телефон к виду +7XXXXXXXXXX (без пробелов)."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if not digits.startswith("7") and len(digits) == 10:
        digits = "7" + digits
    return "+" + digits if digits else ""


def _find_contact_id(bx: BitrixClient, phone: str, email: str) -> int | None:
    """Ищем существующий контакт по телефону или email.

    Приоритет: сначала телефон (точнее), потом email.
    Возвращает ID первого найденного, либо None.
    """
    if phone:
        try:
            found = bx.crm_contact_list(
                {"PHONE": phone}, select=["ID", "NAME", "LAST_NAME", "PHONE", "EMAIL"]
            )
            if found:
                return int(found[0]["ID"])
        except BitrixError as exc:
            logger.warning("contact list by phone failed: %s", exc)
    if email:
        try:
            found = bx.crm_contact_list(
                {"EMAIL": email}, select=["ID", "NAME", "LAST_NAME", "PHONE", "EMAIL"]
            )
            if found:
                return int(found[0]["ID"])
        except BitrixError as exc:
            logger.warning("contact list by email failed: %s", exc)
    return None


def _find_company_id(bx: BitrixClient, inn: str, title: str) -> int | None:
    """Ищем существующую компанию по ИНН или названию.

    ИНН — основной ключ. Название — fallback (менее надёжно,
    но полезно когда ИНН не указан).
    """
    if inn:
        # Попробуем пользовательское поле UF_CRM_INN (если настроено)
        try:
            found = bx.crm_company_list(
                {"UF_CRM_INN": inn}, select=["ID", "TITLE", "UF_CRM_INN"]
            )
            if found:
                return int(found[0]["ID"])
        except BitrixError as exc:
            logger.warning("company list by UF_CRM_INN failed: %s", exc)
        # Попробуем через PHONE (для ИП без UF_CRM_INN)
        # No-op: нет ИНН у контакта не подходит — у компании нет PHONE.
    if title:
        try:
            found = bx.crm_company_list(
                {"TITLE": title}, select=["ID", "TITLE"]
            )
            if found:
                return int(found[0]["ID"])
        except BitrixError as exc:
            logger.warning("company list by TITLE failed: %s", exc)
    return None


def _format_price_rub(raw: str) -> str | None:
    """Превращает «25 000» или «25000» в «25000» (как хранится в БД)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    return digits


def _make_chat_message(parsed: dict[str, Any], deal_id: int, files_count: int) -> str:
    """Формирует текст сообщения в чат «Запуск новых проектов»."""
    from app.yandex_parser import _format_work_summary
    ct = parsed.get("customer_type", "?")
    fio = parsed.get("fio", "—")
    # Раскрываем коды работ: A1 → "Уточнение границ земельного участка (межевание)"
    title = _format_work_summary(parsed) or parsed.get("object_kind") or "?"
    addr = parsed.get("object_address") or "—"
    phone = parsed.get("phone") or "—"
    cadnum = parsed.get("object_cadnum") or ""
    cad_block = f"\n📍 Кадастровый: {cadnum}" if cadnum else ""
    price = parsed.get("agreed_price", "")
    price_block = f"\n💰 Согласованная стоимость: {price} ₽" if price else ""
    files_block = f"\n📎 Файлов: {files_count}" if files_count else ""
    return (
        f"🚀 В работу запущена новая сделка #{deal_id}\n"
        f"\n"
        f"👤 Заказчик: {fio} ({ct})\n"
        f"📞 Телефон: {phone}\n"
        f"📋 Работы: {title}\n"
        f"🏠 Адрес: {addr}{cad_block}{price_block}{files_block}\n"
        f"\n"
        f"Ссылка: {os.environ.get('BITRIX_BASE_URL', 'https://bitrix.a2kad.ru')}/crm/deal/details/{deal_id}/"
    )


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "kad_yandexFORMs_leads",
        "version": "0.2.0",
        "funnel": _funnel_id(),
        "chat_id_configured": bool(_chat_id()),
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


@app.post("/webhook/yandex")
async def webhook_yandex(
    request: Request,
    payload: str = Form(..., description="JSON-encoded wizard payload (event, survey_id, answers[])"),
    files: list[UploadFile] = File(default=[], description="Прикреплённые документы"),
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict[str, Any]:
    """Handle wizard form submission.

    Multipart payload:
      - payload: JSON with {event, survey_id, submitted_at, answers: [{question_id, value}]}
      - files: один или несколько UploadFile (документы заказчика)
    """
    _verify_secret(x_webhook_secret)

    # ---- Parse JSON payload ----
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.warning("non-JSON payload: %s", exc)
        raise HTTPException(status_code=400, detail=f"payload must be JSON: {exc}") from exc

    logger.info(
        "yandex webhook: keys=%s, files=%d", sorted(data.keys()), len(files)
    )
    parsed = normalise_payload(data)
    logger.info("parsed: %s", parsed)

    fields = build_deal_fields(
        parsed,
        funnel_id=_funnel_id(),
        responsible_id=_responsible_id(),
    )
    logger.info("deal fields: %s", fields)

    customer_type = parsed.get("customer_type", "")
    fio = (parsed.get("fio") or "").strip()
    phone_raw = parsed.get("phone") or ""
    phone = _normalise_phone(phone_raw)
    email = (parsed.get("email") or "").strip()
    org_name = (parsed.get("org_name") or "").strip()
    inn = (parsed.get("inn") or "").strip()
    agreed_price_raw = parsed.get("agreed_price") or ""
    agreed_price = _format_price_rub(agreed_price_raw)

    contact_id: int | None = None
    company_id: int | None = None
    deal_id: int | None = None

    # ---- 1. Contact: search by phone/email, then create ----
    try:
        with _bitrix() as bx:
            contact_id = _find_contact_id(bx, phone, email)
            if contact_id:
                logger.info("contact found by dedup: id=%s", contact_id)
            else:
                name_parts = fio.split(maxsplit=1)
                last_name = name_parts[0] if name_parts else ""
                first_name = name_parts[1] if len(name_parts) > 1 else ""
                contact_fields: dict[str, Any] = {
                    "NAME": first_name,
                    "LAST_NAME": last_name,
                    "ASSIGNED_BY_ID": _responsible_id(),
                }
                if phone:
                    contact_fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
                if email:
                    contact_fields["EMAIL"] = [{"VALUE": email, "VALUE_TYPE": "WORK"}]
                if _is_legal_entity(customer_type) and org_name:
                    contact_fields["COMPANY_TITLE"] = org_name
                contact_id = bx.crm_contact_add(contact_fields)
                logger.info("contact created: id=%s", contact_id)
    except BitrixError as exc:
        logger.exception("contact step failed")
        tg_notify(f"[kad_yandexFORMs_leads] ❌ contact error: {exc}")
        contact_id = None

    # ---- 2. Company (only for legal entities) ----
    if _is_legal_entity(customer_type) and (org_name or inn):
        try:
            with _bitrix() as bx:
                company_id = _find_company_id(bx, inn, org_name)
                if company_id:
                    logger.info("company found by dedup: id=%s", company_id)
                else:
                    company_fields: dict[str, Any] = {
                        "TITLE": org_name or inn,
                        "ASSIGNED_BY_ID": _responsible_id(),
                    }
                    if inn:
                        company_fields["UF_CRM_INN"] = inn
                    company_id = bx.crm_company_add(company_fields)
                    logger.info("company created: id=%s", company_id)
        except BitrixError as exc:
            logger.exception("company step failed")
            tg_notify(f"[kad_yandexFORMs_leads] ❌ company error: {exc}")
            company_id = None

    # ---- 3. Create deal ----
    # Привязка контакта/компании задаётся в fields ДО создания (так дешевле).
    if contact_id:
        fields["CONTACT_ID"] = contact_id
    if company_id:
        fields["COMPANY_ID"] = company_id
    if agreed_price:
        fields["OPPORTUNITY"] = agreed_price
        fields["OPPORTUNITY_CURRENCY_ID"] = "RUB"

    try:
        with _bitrix() as bx:
            deal_id = bx.crm_deal_add(fields)
            logger.info("deal created: id=%s", deal_id)
    except BitrixError as exc:
        logger.exception("Bitrix deal.add error")
        tg_notify(f"[kad_yandexFORMs_leads] ❌ Bitrix deal error: {exc}\nPayload keys: {sorted(data.keys())}")
        return {"ok": False, "error": str(exc), "parsed_keys": list(parsed.keys())}

    # ---- 4. Bindings (на всякий случай — если в fields не сработало) ----
    if deal_id:
        try:
            with _bitrix() as bx:
                if contact_id:
                    bx.crm_deal_contact_add(deal_id, contact_id)
                if company_id:
                    bx.crm_deal_company_add(deal_id, company_id)
        except BitrixError as exc:
            logger.warning("binding step (non-fatal): %s", exc)

    # ---- 5. Timeline: бриф + сообщение о стоимости + пинг чата ----
    if deal_id:
        timeline_lines = [
            f"Заявка из wizard-формы 2КАД",
            f"Submitted: {parsed.get('_submitted_at', '?')}",
            f"--- Полные данные заявки ---",
            f"{fields.get('COMMENTS', '(нет данных)')}",
        ]
        if agreed_price:
            timeline_lines.append(
                f"\n💰 Согласованная стоимость (от заказчика): {agreed_price} ₽"
            )
        if contact_id:
            timeline_lines.append(f"\n👤 Контакт: #{contact_id}")
        if company_id:
            timeline_lines.append(f"🏢 Компания: #{company_id}")
        if files:
            timeline_lines.append(f"📎 Прикреплено файлов: {len(files)}")
        try:
            with _bitrix() as bx:
                bx.crm_timeline_comment_add(
                    entity_type="deal",
                    entity_id=deal_id,
                    comment="\n".join(timeline_lines),
                )
        except BitrixError as exc:
            logger.warning("timeline comment (non-fatal): %s", exc)

    # ---- 6. Files: upload to Bitrix.Disk + bind to deal via timeline ----
    uploaded_files: list[dict[str, Any]] = []
    if deal_id and files:
        try:
            uploaded_files = _upload_files_to_disk(files, deal_id, parsed.get("fio", "—"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("disk upload failed")
            tg_notify(f"[kad_yandexFORMs_leads] ⚠ disk upload: {exc}")
        if uploaded_files:
            try:
                with _bitrix() as bx:
                    links = "\n".join(
                        f"  • {f['name']} ({f['size_kb']} КБ) — {f['url']}"
                        for f in uploaded_files
                    )
                    bx.crm_timeline_comment_add(
                        entity_type="deal",
                        entity_id=deal_id,
                        comment=f"📎 Прикреплённые документы ({len(uploaded_files)}):\n{links}",
                    )
            except BitrixError as exc:
                logger.warning("files timeline comment (non-fatal): %s", exc)

    # ---- 7. Чат «Запуск новых проектов» (от Зоткина Евгения) ----
    if deal_id:
        chat_id_raw = _chat_id()
        chat_msg = _make_chat_message(parsed, deal_id, len(uploaded_files))
        chat_sent = False
        if chat_id_raw:
            # 7a) Попробуем через отдельный IM-webhook токен (если настроен)
            im_token = os.environ.get("BITRIX_IM_WEBHOOK_TOKEN", "").strip()
            if im_token:
                try:
                    im_bx = bitrix_from_env()  # для любых других вызовов
                    im_bx.close()
                except Exception:  # noqa: BLE001
                    pass
                # Создаём отдельный BitrixClient с IM-токеном
                from app.bitrix import BitrixClient as _BX
                im_bx = _BX(
                    base_url=os.environ.get("BITRIX_BASE_URL", "https://bitrix.a2kad.ru"),
                    webhook_token=im_token,
                )
                try:
                    im_bx.im_message_add(f"chat{chat_id_raw}", chat_msg)
                    logger.info("chat message sent to chat%s via IM webhook", chat_id_raw)
                    chat_sent = True
                except BitrixError as exc:
                    logger.warning("IM-webhook im.message.add failed: %s", exc)
                finally:
                    im_bx.close()
            # 7b) Если IM-webhook не настроен — пробуем текущим (скорее всего, не сработает)
            if not chat_sent:
                try:
                    with _bitrix() as bx:
                        bx.im_message_add(f"chat{chat_id_raw}", chat_msg)
                        logger.info("chat message sent to chat%s via default webhook", chat_id_raw)
                        chat_sent = True
                except BitrixError as exc:
                    logger.warning(
                        "im.message.add failed (need IM scope on webhook). "
                        "Set BITRIX_IM_WEBHOOK_TOKEN with im scope to enable chat. Error: %s",
                        exc,
                    )
        if not chat_sent:
            # 7c) Fallback: timeline-комментарий (видно владельцу сделки)
            try:
                with _bitrix() as bx:
                    bx.crm_timeline_comment_add(
                        entity_type="deal",
                        entity_id=deal_id,
                        comment=f"[Для чата сделки]\n{chat_msg}",
                    )
            except BitrixError as exc:
                logger.warning("chat fallback (timeline) failed: %s", exc)
        # 7d) Дополнительный ping: TG с прямой ссылкой на чат
        try:
            base = os.environ.get("BITRIX_BASE_URL", "https://bitrix.a2kad.ru")
            chat_link = f"{base}/online/?IM_DIALOG=chat{chat_id_raw}" if chat_id_raw else ""
            tg_notify(
                f"[kad_yandexFORMs_leads] 💬 Новая сделка #{deal_id} в Bitrix-чате\n"
                f"Ссылка: {chat_link}\n"
                f"Заказчик: {fio or '—'} | Тип: {customer_type or '—'}\n"
                f"Адрес: {parsed.get('object_address', '—')}\n"
                f"Стоимость: {agreed_price or '—'}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("tg chat ping failed (non-fatal): %s", exc)

    # ---- 8. Todo: запросить документы ----
    if deal_id:
        try:
            with _bitrix() as bx:
                bx.call(
                    "crm.todo.add",
                    {
                        "fields": {
                            "OWNER_ID": deal_id,
                            "OWNER_TYPE": "D",
                            "TITLE": "Запросить документы у заказчика",
                            "DESCRIPTION": (
                                f"Заявка #{deal_id} с wizard-формы. "
                                f"Связаться с {fio or '—'}, тел. "
                                f"{phone_raw or '—'}, e-mail {email or '—'}."
                            ),
                        }
                    },
                )
        except BitrixError as exc:
            logger.warning("todo add (non-fatal): %s", exc)

    # ---- 9. Owner TG-уведомление ----
    tg_notify(
        f"[kad_yandexFORMs_leads] ✅ Новая заявка → сделка #{deal_id}\n"
        f"Заголовок: {fields.get('TITLE', '?')}\n"
        f"ФИО: {fio or '—'}\n"
        f"Телефон: {phone_raw or '—'}\n"
        f"Контакт: #{contact_id or '—'} | Компания: #{company_id or '—'}\n"
        f"Файлов: {len(uploaded_files)}"
    )

    # ---- 10. Auto-reply клиенту на email ----
    customer_email = email
    customer_fio = fio
    # Раскрываем коды: A1 → "Уточнение границ земельного участка (межевание)"
    from app.yandex_parser import _format_work_summary
    work_summary = _format_work_summary(parsed) or "(см. список работ в брифе)"
    auto_reply_ok = send_customer_reply(
        customer_email,
        fio=customer_fio,
        deal_id=deal_id,
        work_summary=work_summary,
    )
    if auto_reply_ok:
        tg_notify(
            f"[kad_yandexFORMs_leads] 📧 Авто-ответ отправлен на {customer_email}"
        )

    return {
        "ok": True,
        "deal_id": deal_id,
        "title": fields.get("TITLE"),
        "customer_type": customer_type,
        "contact_id": contact_id,
        "company_id": company_id,
        "files_uploaded": len(uploaded_files),
        "auto_reply_sent": auto_reply_ok,
        "agreed_price": agreed_price,
    }


# ---- helpers ---------------------------------------------------------------

def _upload_files_to_disk(
    files: list[UploadFile],
    deal_id: int,
    customer_label: str,
) -> list[dict[str, Any]]:
    """Загружает файлы в Битрикс.Диск и привязывает к сделке через timeline.

    Структура:
      /<disk_root>/
        Заявки 2КАД/
          <customer_label>/
            <deal_id>/
              <files>

    Returns: список словарей {name, size_kb, file_id, url}.
    """
    import re as _re
    safe = _re.sub(r"[^A-Za-zА-Яа-я0-9 _\-]+", "", customer_label or "client")[:64].strip() or "client"
    uploaded: list[dict[str, Any]] = []
    with _bitrix() as bx:
        # 1) Найти/создать корневую папку «Заявки 2КАД»
        root_id = _disk_root_folder_id()
        # 2) Создать подпапки по пути
        folder_path = f"Заявки 2КАД / {safe} / {deal_id}"
        # Упрощённо: поднимаемся по уровням через disk.folder.addsubfolder
        current_parent = root_id
        for piece in ["Заявки 2КАД", safe, str(deal_id)]:
            try:
                sub = bx.disk_folder_get_subfolder_id(current_parent)
            except BitrixError:
                sub = None
            if sub is None:
                try:
                    sub = bx.disk_folder_add_subfolder(current_parent, piece)
                except BitrixError as exc:
                    logger.warning("disk: cannot create %s under %s: %s", piece, current_parent, exc)
                    raise
            current_parent = sub
        # 3) Загрузить каждый файл в созданную папку
        for f in files:
            try:
                content = f.file.read()
                if not content:
                    continue
                # REST: disk.folder.uploadfile принимает файл через POST.
                # Проще: используем crm.deal.update с UTM и привязываем через
                # crm.timeline.bind — но это сложно. Делаем прямую загрузку
                # в disk.folder.uploadfile через _bitrix().call напрямую.
                import httpx
                # webhook-режим: используем webhook_url
                bx_inst = bx
                if bx_inst._mode == "webhook":
                    url = f"{bx_inst._webhook_url}/disk.folder.uploadfile.json"
                    # multipart: id, file
                    upload_resp = bx_inst._client.post(
                        url,
                        data={"id": current_parent, "data": json.dumps({"NAME": f.filename or "file"})},
                        files={"file": (f.filename or "file", content, f.content_type or "application/octet-stream")},
                    )
                else:
                    # cookie: тоже через disk.folder.uploadfile
                    url = f"{bx_inst.base_url}/rest/disk.folder.uploadfile.json"
                    upload_resp = bx_inst._client.post(
                        url,
                        data={"id": current_parent, "data": json.dumps({"NAME": f.filename or "file"}), "sessid": bx_inst._sessid},
                        files={"file": (f.filename or "file", content, f.content_type or "application/octet-stream")},
                        cookies=bx_inst._cookies,
                    )
                if upload_resp.status_code >= 400:
                    logger.warning("disk upload HTTP %s: %s", upload_resp.status_code, upload_resp.text[:200])
                    continue
                udata = upload_resp.json()
                if isinstance(udata, dict) and udata.get("error"):
                    logger.warning("disk upload error: %s", udata.get("error_description", udata.get("error")))
                    continue
                file_id = (udata.get("result") or {}).get("ID") if isinstance(udata, dict) else None
                file_url = (udata.get("result") or {}).get("DETAIL_URL") if isinstance(udata, dict) else None
                uploaded.append(
                    {
                        "name": f.filename or "file",
                        "size_kb": max(1, len(content) // 1024),
                        "file_id": file_id,
                        "url": file_url or "",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("file upload %s failed", f.filename)
                tg_notify(f"[kad_yandexFORMs_leads] ⚠ upload {f.filename}: {exc}")
    return uploaded
