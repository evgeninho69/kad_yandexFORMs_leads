"""Yandex Cloud Function handler: Yandex Forms v6 webhook -> Bitrix24 deal + auto-reply.

Reads v6 form payload (ФЛ / ИП / ЮЛ + работы + файлы + согласие на ПДн),
parses with canonical field map, creates crm.deal.add in unified 2KAD funnel
(CATEGORY_ID=3, STAGE_ID=C3:NEW), publishes brief to deal timeline, and
sends an auto-reply email to the customer via Yandex SMTP (info@2kad.ru).

Environment (set by YC Function runtime, populated from Lockbox by the
deploy script):
  BITRIX_BASE_URL         e.g. https://bitrix.a2kad.ru
  BITRIX_WEBHOOK_TOKEN     e.g. 1/abc123def456 (no domain, no leading slash)
  BITRIX_FUNNEL_ID         3
  BITRIX_RESPONSIBLE_ID    1

  YANDEX_SMTP_HOST         smtp.yandex.ru
  YANDEX_SMTP_PORT         587
  YANDEX_SMTP_USER         info@2kad.ru
  YANDEX_SMTP_PASSWORD     <app-password from id.yandex.ru/security/app-passwords>
  YANDEX_SMTP_FROM_NAME    "ООО Центр недвижимости 2КАД"
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage

logger = logging.getLogger("kad_yandex_leads_handler")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


# ---------- Canonical field map (mirrors app/yandex_parser.py) ----------
# Keys are stable canonical names; values are human-readable labels.
DEFAULT_FIELD_MAP: dict[str, str] = {
    "customer_type": "Тип заказчика",
    "org_name": "Название организации",
    "inn": "ИНН",
    "fio": "ФИО контактного лица",
    "phone": "Контактный телефон",
    "email": "Электронная почта",
    "snils": "СНИЛС",
    "contact_pref": "Как удобнее связаться",
    "object_kind": "Что является объектом работ",
    "object_cadnum": "Кадастровый номер",
    "object_address": "Адрес или ориентир объекта",
    "object_area": "Площадь объекта, м²",
    "object_address_official": "Присвоен ли объекту официальный адрес",
    "work_a": "A. Межевые планы",
    "work_b": "B. Технические планы",
    "work_c": "C. ККР и заключения КИ",
    "work_d": "D. Смежные услуги",
    "work_extra": "Дополнительные виды работ",
    "files_list": "Перечень прикреплённых документов",
    "notes": "Описание задачи",
    "deadline": "Желаемая дата завершения",
    "urgency": "Срочность",
    "source": "Откуда узнали о нас",
    "consent_pdn": "Согласие на обработку ПДн",
}


# Справочник работ (из v5, перенесён в handler — клиент выбирает номера через запятую).
# Хранится в handler.py чтобы парсер был самодостаточен.
WORK_CATALOG = {
    "A": [
        "1. Уточнение границ земельного участка (межевание)",
        "2. Раздел земельного участка",
        "3. Объединение земельных участков",
        "4. Перераспределение земельных участков",
        "5. Образование земельного участка из земель государственной/муниципальной собственности",
        "6. Вынос границ земельного участка в натуру",
        "7. Образование части земельного участка",
        "8. Исправление реестровой ошибки в сведениях ЕГРН",
        "9. Объединение земельных участков с сохранением исходных",
        "10. Раздел с сохранением исходного участка в изменённых границах",
        "11. Установление сервитута (части)",
        "12. Межевание с уточнением площади",
        "13. Схема расположения земельного участка на КПТ",
    ],
    "B": [
        "1. Технический план здания",
        "2. Технический план сооружения",
        "3. Технический план объекта незавершённого строительства",
        "4. Технический план машино-места",
        "5. Технический план единого недвижимого комплекса",
        "6. Технический план помещения",
        "7. Технический план многоквартирного дома",
        "8. Акт обследования (снос ОКС)",
        "9. Подготовка ТП для ввода ОКС в эксплуатацию",
        "10. Внесение изменений в ЕГРН (реконструкция, перепланировка)",
        "11. Технический план для регистрации права",
        "12. Технический паспорт (для нотариуса / банка)",
    ],
    "C": [
        "1. Комплексные кадастровые работы (ККР)",
        "2. Заключение кадастрового инженера (ЗКИ)",
        "3. Судебная землеустроительная экспертиза",
        "4. Межевой план для исправления реестровой ошибки",
    ],
    "D": [
        "1. Подготовка схемы расположения ЗУ на КПТ",
        "2. Подготовка схемы границ",
        "3. Получение ГПЗУ / разрешения на строительство",
        "4. Получение уведомления о начале строительства",
        "5. Получение уведомления об окончании строительства",
        "6. Получение разрешения на ввод ОКС в эксплуатацию",
        "7. Подготовка карты-плана территории",
        "8. Геодезическое сопровождение строительства",
        "9. Топографическая съёмка",
        "10. Геоподоснова для проекта",
        "11. Согласование границ со смежными землепользователями",
        "12. Представление интересов в Росреестре",
        "13. Снятие объекта с кадастрового учёта",
        "14. Восстановление правоустанавливающих документов",
        "15. Юридическое сопровождение сделок с недвижимостью",
        "16. Оценка рыночной стоимости",
        "17. Подготовка договора купли-продажи / дарения",
        "18. Регистрация перехода права",
        "19. Согласование перепланировки",
        "20. Перевод земель из одной категории в другую",
        "21. Изменение вида разрешённого использования (ВРИ)",
        "22. Получение градостроительного плана (ГПЗУ)",
        "23. Получение разрешения на ИЖС",
        "24. Согласование границ с муниципалитетом",
        "25. Получение технических условий (ТУ)",
        "26. Подключение к сетям (электро-, газо-, водоснабжение)",
        "27. Постановка на кадастровый учёт линейного объекта",
        "28. Оформление сервитута",
        "29. Согласование с Росреестром исправления ошибок",
        "30. Подготовка XML-документов для ЕГРН",
        "31. Подача документов в Росреестр без доверенности (по 218-ФЗ ст. 18)",
    ],
}


def _coerce_value(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, bool):
        return "Да" if raw else "Нет"
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, list):
        # Choice/multi-choice: list of option labels or ids; file uploads return list of dicts.
        out: list[str] = []
        for x in raw:
            if isinstance(x, dict):
                # File upload shape from Yandex Forms: {name, url, size, ...}
                for k in ("name", "filename", "url", "href"):
                    if k in x and x[k]:
                        out.append(str(x[k]))
                        break
                else:
                    out.append(json.dumps(x, ensure_ascii=False))
            else:
                out.append(str(x))
        return ", ".join(out)
    if isinstance(raw, dict):
        for key in ("label", "value", "text", "name"):
            if key in raw and raw[key]:
                return str(raw[key])
        return json.dumps(raw, ensure_ascii=False)
    return str(raw)


def _expand_work_codes(work_a: str) -> str:
    """Превращает '1, 3, 7' в список человекочитаемых названий из WORK_CATALOG['A']."""
    if not work_a:
        return ""
    out: list[str] = []
    for token in work_a.split(","):
        n = int("".join(c for c in token if c.isdigit()) or 0)
        if 1 <= n <= len(WORK_CATALOG["A"]):
            out.append(WORK_CATALOG["A"][n - 1])
    return "; ".join(out)


def _expand_work_codes_multi(work_str: str, kind: str) -> str:
    """Аналогично для B/C/D."""
    if not work_str:
        return ""
    items = WORK_CATALOG.get(kind, [])
    if not items:
        return work_str
    out: list[str] = []
    for token in work_str.split(","):
        n = int("".join(c for c in token if c.isdigit()) or 0)
        if 1 <= n <= len(items):
            out.append(items[n - 1])
    return "; ".join(out)


def normalise_payload(payload: dict) -> dict[str, str]:
    """Reduce Yandex payload to {canonical_key: value}.

    Yandex Forms webhook payload looks like:
      {
        "event": "answer.created",
        "survey_id": "...",
        "submitted_at": "...",
        "answers": [
          {"question_id": "...", "label": "...", "value": ...},
          ...
        ]
      }
    """
    out: dict[str, str] = {}
    answers = payload.get("answers")
    if not isinstance(answers, list):
        answers = []

    for ans in answers:
        if not isinstance(ans, dict):
            continue
        qid = str(ans.get("question_id") or ans.get("id") or "")
        label = ans.get("label") or ans.get("name") or ""
        value = ans.get("value", ans.get("answer", ans.get("values")))
        if not qid and not label:
            continue

        # Primary: match by question_id (stable, set in the form schema).
        canonical = ""
        if qid in DEFAULT_FIELD_MAP:
            canonical = qid

        # Fallback: match by label substring.
        if not canonical:
            label_lc = str(label).strip().lower()
            for cid, human in DEFAULT_FIELD_MAP.items():
                if human.lower() in label_lc:
                    canonical = cid
                    break

        if not canonical:
            canonical = f"q_{qid}" if qid else f"q_unknown"

        out[canonical] = _coerce_value(value)

    # Pass-through metadata.
    out["_event"] = str(payload.get("event") or payload.get("event_type") or "")
    out["_survey_id"] = str(
        payload.get("survey_id") or payload.get("form_id") or payload.get("id") or ""
    )
    out["_submitted_at"] = str(
        payload.get("submitted_at") or payload.get("created_at") or ""
    )
    return out


def _classify_customer(parsed: dict[str, str]) -> str:
    """Определить тип заказчика: ФЛ / ИП / ЮЛ.

    Правила:
      - явное значение в customer_type (case-insensitive) — приоритет;
      - иначе — эвристика по наличию ИНН/названия организации;
      - дефолт — ФЛ.
    """
    raw = (parsed.get("customer_type") or "").strip().lower()
    if "юр" in raw or "юл" in raw or "ооо" in raw or "организац" in raw:
        return "ЮЛ"
    if "ип" in raw or "предпринимател" in raw:
        return "ИП"
    if "физ" in raw or "фл" in raw or "лиц" in raw:
        return "ФЛ"
    # Эвристика по заполненности.
    if (parsed.get("inn") or "").strip() and (parsed.get("org_name") or "").strip():
        # 10 цифр = ЮЛ, 12 = ИП
        inn = (parsed.get("inn") or "").strip()
        if inn.isdigit() and len(inn) == 10:
            return "ЮЛ"
        if inn.isdigit() and len(inn) == 12:
            return "ИП"
    if (parsed.get("inn") or "").strip() or (parsed.get("org_name") or "").strip():
        # ИНН или название есть, но эвристика не сработала — пусть будет ИП.
        return "ИП"
    return "ФЛ"


def _format_work_summary(parsed: dict[str, str]) -> str:
    """Собирает человекочитаемый список работ из A/B/C/D + дополнительные."""
    parts: list[str] = []
    for kind, key in (("A", "work_a"), ("B", "work_b"), ("C", "work_c"), ("D", "work_d")):
        if key in ("work_a", "work_b", "work_c"):
            expanded = _expand_work_codes_multi(parsed.get(key, ""), kind)
        else:
            # Для D строка свободная (без кодов), выводим как есть.
            expanded = (parsed.get(key) or "").strip()
        if expanded:
            parts.append(f"[{kind}] {expanded}")
    extra = (parsed.get("work_extra") or "").strip()
    if extra:
        parts.append(f"[+] {extra}")
    return " | ".join(parts) if parts else "(не выбраны)"


def build_deal_fields(parsed: dict[str, str], funnel_id: int, responsible_id: int) -> dict:
    customer_type = _classify_customer(parsed)
    work_summary = _format_work_summary(parsed)
    addr = (parsed.get("object_address") or "").strip()

    # TITLE: «{work} — {addr} — {customer_type}»
    title_parts: list[str] = []
    if work_summary and work_summary != "(не выбраны)":
        # Берём первый пункт списка.
        first = work_summary.split("|")[0].strip()
        # Убираем префикс [A] / [B] и т.п. для краткости.
        for prefix in ("[A] ", "[B] ", "[C] ", "[D] ", "[+] "):
            if first.startswith(prefix):
                first = first[len(prefix):]
                break
        title_parts.append(first[:80])
    if addr:
        title_parts.append(addr[:80])
    if not title_parts:
        title_parts.append("Заявка с Яндекс Формы")
    title_parts.append(customer_type)
    title = " — ".join(title_parts)[:255]

    # COMMENTS: многострочный бриф.
    comments: list[str] = []
    comments.append(f"Тип заказчика: {customer_type}")
    org_name = (parsed.get("org_name") or "").strip()
    if org_name:
        comments.append(f"Организация: {org_name}")
    inn = (parsed.get("inn") or "").strip()
    if inn:
        comments.append(f"ИНН: {inn}")
    comments.append("")
    comments.append("=== ЗАКАЗЧИК ===")
    for key in ("fio", "phone", "email", "snils", "contact_pref"):
        val = (parsed.get(key) or "").strip()
        if val:
            comments.append(f"{DEFAULT_FIELD_MAP[key]}: {val}")
    comments.append("")
    comments.append("=== ОБЪЕКТ ===")
    for key in ("object_kind", "object_cadnum", "object_address", "object_area", "object_address_official"):
        val = (parsed.get(key) or "").strip()
        if val:
            comments.append(f"{DEFAULT_FIELD_MAP[key]}: {val}")
    comments.append("")
    comments.append("=== РАБОТЫ ===")
    comments.append(work_summary)
    comments.append("")
    comments.append("=== ДОКУМЕНТЫ ===")
    files_list = (parsed.get("files_list") or "").strip()
    if files_list:
        comments.append(f"Перечень: {files_list}")
    notes = (parsed.get("notes") or "").strip()
    if notes:
        comments.append(f"Описание: {notes}")
    comments.append("")
    comments.append("=== СРОКИ ===")
    for key in ("deadline", "urgency", "source"):
        val = (parsed.get(key) or "").strip()
        if val:
            comments.append(f"{DEFAULT_FIELD_MAP[key]}: {val}")
    consent = (parsed.get("consent_pdn") or "").strip()
    comments.append("")
    comments.append(f"Согласие на обработку ПДн: {consent if consent else 'НЕТ'}")
    comment_text = "\n".join(comments)

    fields: dict = {
        "TITLE": title,
        "CATEGORY_ID": funnel_id,
        "STAGE_ID": f"C{funnel_id}:NEW",
        "RESPONSIBLE_ID": responsible_id,
        "OPENED": "Y",
        "COMMENTS": comment_text,
        "SOURCE_ID": "WEB",
        "SOURCE_DESCRIPTION": "Яндекс Форма v6 (webhook answer.created)",
    }

    phone = (parsed.get("phone") or "").strip()
    if phone:
        fields["UF_CRM_RT_TEL_CONT"] = phone
    email = (parsed.get("email") or "").strip()
    if email:
        fields["UF_CRM_RT_EMAIL_CONT"] = email

    return fields


# ---------- Bitrix client (webhook-token mode only) ----------
def bx_call(base_url: str, webhook_token: str, method: str, params: dict) -> dict:
    if "/" not in webhook_token:
        raise RuntimeError("BITRIX_WEBHOOK_TOKEN must be in 'USER/TOKEN' format")
    url = f"{base_url.rstrip('/')}/rest/{webhook_token}/{method}.json"
    body = json.dumps(params, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", errors="replace"))
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(
            f"Bitrix {method}: {data.get('error')} — {data.get('error_description', '')}"
        )
    return data.get("result", data)


# ---------- SMTP auto-reply ----------
def send_auto_reply(to_email: str, fio: str, deal_id: int, work_summary: str) -> bool:
    """Best-effort: отправляет клиенту подтверждение получения заявки.

    Возвращает True если письмо ушло, False при любой ошибке (никогда не падает).
    """
    host = os.environ.get("YANDEX_SMTP_HOST", "smtp.yandex.ru")
    port = int(os.environ.get("YANDEX_SMTP_PORT", "587"))
    user = os.environ.get("YANDEX_SMTP_USER", "info@2kad.ru")
    password = os.environ.get("YANDEX_SMTP_PASSWORD", "").strip()
    from_name = os.environ.get("YANDEX_SMTP_FROM_NAME", "ООО Центр недвижимости 2КАД")

    if not to_email or not password:
        logger.warning("auto-reply skipped: no recipient or no SMTP password")
        return False

    first_name = (fio or "").split()[1] if fio and len(fio.split()) >= 2 else (fio or "")
    if not first_name:
        first_name = "уважаемый клиент"

    subject = f"Заявка #{deal_id} принята — 2КАД"
    body = (
        f"Здравствуйте, {first_name}!\n\n"
        f"Спасибо за обращение в ООО «Центр недвижимости 2КАД».\n\n"
        f"Ваша заявка №{deal_id} успешно зарегистрирована. "
        f"В ближайшее время (в течение 1 рабочего дня) с вами свяжется наш специалист "
        f"для уточнения деталей и подготовки коммерческого предложения.\n\n"
        f"Вид работ: {work_summary[:200]}\n\n"
        f"Если у вас срочный вопрос — звоните +7 (4822) 41-57-68 или пишите info@2kad.ru.\n\n"
        f"С уважением,\n"
        f"ООО «Центр недвижимости 2КАД»\n"
        f"ИНН 6950167545\n"
        f"г. Тверь, ул. Медниковская, д. 53, пом. 6, оф. 22\n"
    )

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{user}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body, subtype="plain", charset="utf-8")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(user, password)
            smtp.send_message(msg)
        logger.info("auto-reply sent to %s", to_email)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto-reply failed: %s", exc)
        return False


# ---------- YC Function handler ----------
def handler(event, context):  # noqa: ARG001 - context is YC runtime arg
    # YC Functions can be triggered via HTTP API gateway (event has body) or directly.
    body = ""
    if isinstance(event, dict):
        body = event.get("body") or event.get("rawBody") or event.get("payload") or ""
        # HTTP API gateway wraps body as base64 by default; check for isBase64Encoded.
        if event.get("isBase64Encoded") and isinstance(body, str):
            import base64
            try:
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            except Exception:
                pass
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")

    try:
        payload = json.loads(body) if body else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("invalid JSON: %s", exc)
        return {
            "statusCode": 200,
            "body": json.dumps({"ok": False, "error": "invalid json"}, ensure_ascii=False),
        }

    parsed = normalise_payload(payload)
    logger.info("parsed keys: %s", list(parsed.keys()))

    base_url = os.environ.get("BITRIX_BASE_URL", "https://bitrix.a2kad.ru")
    token = os.environ.get("BITRIX_WEBHOOK_TOKEN", "").strip()
    funnel_id = int(os.environ.get("BITRIX_FUNNEL_ID", "3"))
    responsible_id = int(os.environ.get("BITRIX_RESPONSIBLE_ID", "1"))

    if not token:
        logger.error("BITRIX_WEBHOOK_TOKEN not set")
        return {
            "statusCode": 200,
            "body": json.dumps({"ok": False, "error": "no token"}, ensure_ascii=False),
        }

    fields = build_deal_fields(parsed, funnel_id, responsible_id)
    try:
        deal_id = bx_call(base_url, token, "crm.deal.add", {"fields": fields})
        deal_id = int(deal_id)
        logger.info("deal created: %s", deal_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Bitrix error")
        return {
            "statusCode": 200,
            "body": json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
        }

    # Brief in timeline.
    try:
        bx_call(
            base_url,
            token,
            "crm.timeline.comment.add",
            {
                "fields": {
                    "ENTITY_ID": deal_id,
                    "ENTITY_TYPE": "deal",
                    "COMMENT": (
                        f"Заявка из Яндекс Формы v6\n"
                        f"Survey: {parsed.get('_survey_id', '?')}\n"
                        f"Event: {parsed.get('_event', 'answer.created')}\n"
                        f"Submitted: {parsed.get('_submitted_at', '?')}\n"
                        f"--- Полные данные заявки ---\n{fields.get('COMMENTS', '(нет данных)')}"
                    ),
                }
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("timeline failed (non-fatal): %s", exc)

    # Auto-reply to customer.
    customer_email = (parsed.get("email") or "").strip()
    customer_fio = (parsed.get("fio") or "").strip()
    work_summary = _format_work_summary(parsed)
    auto_reply_ok = send_auto_reply(customer_email, customer_fio, deal_id, work_summary)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "ok": True,
                "deal_id": deal_id,
                "title": fields.get("TITLE"),
                "customer_type": _classify_customer(parsed),
                "auto_reply_sent": auto_reply_ok,
            },
            ensure_ascii=False,
        ),
    }
