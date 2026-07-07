"""Yandex Forms payload -> structured lead dict.

Yandex Forms webhook payloads come in two flavours. We support both:
  1. answer.created with full form snapshot (newest API)
  2. legacy payloads with `value` arrays per question

Both shapes are normalised into a flat dict keyed by a human label, so the rest
of the pipeline (deal creation, folder watcher, TG notify) doesn't have to know
about Yandex internals.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# question_id -> (canonical key, human label)
# Set these in production once we have the actual form schema. Default mapping
# matches the v6 form for "Заявка на кадастровые работы — 2КАД (ФЛ/ИП/ЮЛ)".
DEFAULT_FIELD_MAP: dict[str, tuple[str, str]] = {
    "customer_type": ("customer_type", "Тип заказчика"),
    "org_name": ("org_name", "Название организации"),
    "inn": ("inn", "ИНН"),
    "fio": ("fio", "ФИО контактного лица"),
    "phone": ("phone", "Контактный телефон"),
    "email": ("email", "Электронная почта"),
    "snils": ("snils", "СНИЛС"),
    "contact_pref": ("contact_pref", "Как удобнее связаться"),
    "object_kind": ("object_kind", "Что является объектом работ"),
    "object_cadnum": ("object_cadnum", "Кадастровый номер"),
    "object_address": ("object_address", "Адрес или ориентир объекта"),
    "object_area": ("object_area", "Площадь объекта, м²"),
    "object_address_official": ("object_address_official", "Присвоен ли объекту официальный адрес"),
    "work_a": ("work_a", "A. Межевые планы"),
    "work_b": ("work_b", "B. Технические планы"),
    "work_c": ("work_c", "C. ККР и заключения КИ"),
    "work_d": ("work_d", "D. Смежные услуги"),
    "work_main": ("work_main", "Основной вид работ (TITLE)"),
    "work_extra": ("work_extra", "Дополнительные виды работ"),
    "objects": ("objects", "Объекты (multi-object JSON)"),
    "files_list": ("files_list", "Перечень прикреплённых документов"),
    "notes": ("notes", "Описание задачи"),
    "deadline": ("deadline", "Желаемая дата завершения"),
    "urgency": ("urgency", "Срочность"),
    "source": ("source", "Откуда узнали о нас"),
    "consent_pdn": ("consent_pdn", "Согласие на обработку ПДн"),
}


def _coerce_value(raw: Any) -> str:
    """Yandex answers come as strings, lists, or dicts (for choice/radio)."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (int, float, bool)):
        return str(raw)
    if isinstance(raw, list):
        # Choice/multi-choice: list of option labels or ids.
        return ", ".join(_coerce_value(x) for x in raw if x is not None)
    if isinstance(raw, dict):
        # Some answer types nest value/label.
        for key in ("label", "value", "text", "name"):
            if key in raw:
                return _coerce_value(raw[key])
        return json_dumps_safe(raw)
    return str(raw)


def json_dumps_safe(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(obj)


def normalise_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Reduce Yandex payload to {canonical_key: value}.

    Strategy: walk the payload looking for keys shaped like 'answers' or
    'questions' and extract values per question_id (or label). Anything else
    is captured under 'extra.<key>'.
    """
    out: dict[str, str] = {}

    # Try to locate answers array in the payload.
    candidates: list[dict[str, Any]] = []
    for key in ("answers", "data", "form_response", "response", "items"):
        node = payload.get(key)
        if isinstance(node, list):
            candidates.extend(x for x in node if isinstance(x, dict))
        elif isinstance(node, dict):
            candidates.append(node)

    for ans in candidates:
        qid = str(ans.get("question_id") or ans.get("id") or ans.get("questionId") or "")
        label = ans.get("label") or ans.get("name") or ans.get("question") or ""
        value = ans.get("value", ans.get("answer", ans.get("values")))
        if not qid and not label:
            continue
        canonical, fallback_label = DEFAULT_FIELD_MAP.get(qid, ("", ""))
        if not canonical:
            # Fall back to label-based detection.
            label_lc = str(label).strip().lower()
            for _qid, (canon, human) in DEFAULT_FIELD_MAP.items():
                if human.lower() in label_lc:
                    canonical = canon
                    fallback_label = human
                    break
        if not canonical:
            canonical = f"q_{qid}" if qid else re.sub(r"\W+", "_", str(label)).strip("_") or "q_unknown"
        out[canonical] = _coerce_value(value)

    # Fall back to flat key/value if nothing was extracted.
    if not out and isinstance(payload, dict):
        for k, v in payload.items():
            if k in ("event", "event_type", "form_id", "survey_id", "id", "created_at", "submitted_at"):
                continue
            if isinstance(v, (str, int, float, bool)):
                out[str(k)] = _coerce_value(v)

    # Pass-through metadata.
    out["_event"] = str(payload.get("event") or payload.get("event_type") or "")
    out["_survey_id"] = str(payload.get("survey_id") or payload.get("form_id") or payload.get("id") or "")
    out["_submitted_at"] = str(payload.get("submitted_at") or payload.get("created_at") or "")
    return out


def _classify_customer(parsed: dict[str, str]) -> str:
    """Определить тип заказчика: ФЛ / ИП / ЮЛ.

    Приоритеты:
      1. явное значение в customer_type (case-insensitive) — приоритет;
      2. эвристика по ИНН (10 цифр = ЮЛ, 12 = ИП) при наличии;
      3. наличие org_name + inn → ИП;
      4. дефолт — ФЛ.
    """
    raw = (parsed.get("customer_type") or "").strip().lower()
    if "юр" in raw or "юл" in raw or "ооо" in raw or "организац" in raw or "обществ" in raw:
        return "ЮЛ"
    if "ип" in raw or "предпринимател" in raw:
        return "ИП"
    if "физ" in raw or "фл" in raw or "лиц" in raw:
        return "ФЛ"
    inn = (parsed.get("inn") or "").strip()
    org = (parsed.get("org_name") or "").strip()
    if inn.isdigit():
        if len(inn) == 10:
            return "ЮЛ"
        if len(inn) == 12:
            return "ИП"
    if inn or org:
        return "ИП"
    return "ФЛ"


# Справочник работ для раскрытия кодов A/B/C/D в TITLE и COMMENTS.
WORK_CATALOG = {
    "A": [
        "Уточнение границ земельного участка (межевание)",
        "Раздел земельного участка",
        "Объединение земельных участков",
        "Перераспределение земельных участков",
        "Образование ЗУ из земель гос/муниципальной собственности",
        "Вынос границ земельного участка в натуру",
        "Образование части земельного участка",
        "Исправление реестровой ошибки в ЕГРН",
        "Объединение ЗУ с сохранением исходных",
        "Раздел с сохранением исходного ЗУ в изменённых границах",
        "Установление сервитута (части)",
        "Межевание с уточнением площади",
        "Схема расположения ЗУ на КПТ",
    ],
    "B": [
        "Технический план здания",
        "Технический план сооружения",
        "Технический план ОНС",
        "Технический план машино-места",
        "Технический план единого недвижимого комплекса",
        "Технический план помещения",
        "Технический план МКД",
        "Акт обследования (снос ОКС)",
        "Подготовка ТП для ввода ОКС в эксплуатацию",
        "Внесение изменений в ЕГРН (реконструкция, перепланировка)",
        "Технический план для регистрации права",
        "Технический паспорт",
    ],
    "C": [
        "Комплексные кадастровые работы (ККР)",
        "Заключение кадастрового инженера (ЗКИ)",
        "Судебная землеустроительная экспертиза",
        "Межевой план для исправления реестровой ошибки",
    ],
    "D": None,  # D — свободный текст
}


def _expand_codes(s: str, kind: str) -> str:
    if not s:
        return ""
    items = WORK_CATALOG.get(kind)
    if not items:
        return s
    out: list[str] = []
    for token in s.split(","):
        n = int("".join(c for c in token if c.isdigit()) or 0)
        if 1 <= n <= len(items):
            out.append(items[n - 1])
    return "; ".join(out)


def _format_work_summary(parsed: dict[str, str]) -> str:
    parts: list[str] = []
    a = _expand_codes(parsed.get("work_a", ""), "A")
    if a:
        parts.append(f"[A] {a}")
    b = _expand_codes(parsed.get("work_b", ""), "B")
    if b:
        parts.append(f"[B] {b}")
    c = _expand_codes(parsed.get("work_c", ""), "C")
    if c:
        parts.append(f"[C] {c}")
    d = (parsed.get("work_d") or "").strip()
    if d:
        parts.append(f"[D] {d}")
    extra = (parsed.get("work_extra") or "").strip()
    if extra:
        parts.append(f"[+] {extra}")
    return " | ".join(parts) if parts else "(не выбраны)"


def _format_objects_block(parsed: dict[str, str]) -> str:
    """Multi-object block from wizard v6+. Returns "" if no objects field."""
    raw = (parsed.get("objects") or "").strip()
    if not raw:
        return ""
    try:
        import json
        objs = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(objs, list) or not objs:
        return ""
    lines: list[str] = []
    for i, o in enumerate(objs, 1):
        if not isinstance(o, dict):
            continue
        lines.append(f"--- Объект #{i} ---")
        t = (o.get("object_type") or "").strip()
        if t:
            lines.append(f"Тип: {t}")
        cad = (o.get("object_cadnum") or "").strip()
        if cad:
            lines.append(f"Кадастровый номер: {cad}")
        addr = (o.get("object_address") or "").strip()
        if addr:
            lines.append(f"Адрес: {addr}")
        area = (o.get("object_area") or "").strip()
        if area:
            lines.append(f"Площадь: {area} м²")
        off = (o.get("object_address_official") or "").strip()
        if off:
            lines.append(f"Официальный адрес: {off}")
        # Works for this object
        work_lines: list[str] = []
        for prefix_key, prefix_letter in (("work_a", "A"), ("work_b", "B"), ("work_c", "C"), ("work_d", "D")):
            arr = o.get(prefix_key) or []
            if not isinstance(arr, list) or not arr:
                continue
            for code in arr:
                expanded = _expand_codes(str(code), prefix_letter)
                if expanded:
                    work_lines.append(f"[{prefix_letter}] {expanded}")
        if work_lines:
            lines.append("Работы: " + "; ".join(work_lines))
        lines.append("")
    return "\n".join(lines).strip()


def _first_work(work_summary: str) -> str:
    if not work_summary or work_summary == "(не выбраны)":
        return ""
    first = work_summary.split("|")[0].strip()
    for prefix in ("[A] ", "[B] ", "[C] ", "[D] ", "[+] "):
        if first.startswith(prefix):
            return first[len(prefix):]
    return first


def build_deal_fields(
    parsed: dict[str, str],
    *,
    funnel_id: int,
    responsible_id: int,
) -> dict[str, Any]:
    """Compose crm.deal.add fields from the parsed payload (v6 form / wizard)."""
    customer_type = _classify_customer(parsed)
    work_summary = _format_work_summary(parsed)
    objects_block = _format_objects_block(parsed)
    addr = (parsed.get("object_address") or "").strip()
    org = (parsed.get("org_name") or "").strip()

    # TITLE: «{первая работа} — {адрес} — {тип}».
    title_parts: list[str] = []
    first = _first_work(work_summary)
    if first:
        title_parts.append(first[:80])
    if addr:
        title_parts.append(addr[:80])
    if not title_parts:
        title_parts.append("Заявка с Яндекс Формы")
    title_parts.append(customer_type)
    title = " — ".join(title_parts)[:255]

    # COMMENTS: многострочный бриф, удобный для просмотра в карточке сделки.
    comments: list[str] = []
    comments.append(f"Тип заказчика: {customer_type}")
    if org:
        comments.append(f"Организация: {org}")
    inn = (parsed.get("inn") or "").strip()
    if inn:
        comments.append(f"ИНН: {inn}")
    comments.append("")
    comments.append("=== ЗАКАЗЧИК ===")
    for key in ("fio", "phone", "email", "snils", "contact_pref"):
        val = (parsed.get(key) or "").strip()
        if val:
            comments.append(f"{DEFAULT_FIELD_MAP[key][1]}: {val}")
    comments.append("")
    if objects_block:
        # Multi-object wizard v6+
        comments.append("=== ОБЪЕКТЫ И РАБОТЫ ===")
        comments.append(objects_block)
    else:
        # Single-object legacy form
        comments.append("=== ОБЪЕКТ ===")
        for key in ("object_kind", "object_cadnum", "object_address", "object_area", "object_address_official"):
            val = (parsed.get(key) or "").strip()
            if val:
                comments.append(f"{DEFAULT_FIELD_MAP[key][1]}: {val}")
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
            comments.append(f"{DEFAULT_FIELD_MAP[key][1]}: {val}")
    consent = (parsed.get("consent_pdn") or "").strip()
    comments.append("")
    comments.append(f"Согласие на обработку ПДн: {consent if consent else 'НЕТ'}")
    comment_text = "\n".join(comments)

    fields: dict[str, Any] = {
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
