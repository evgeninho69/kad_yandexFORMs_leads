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
# matches the v5 form for "Заявка на кадастровые / землеустроительные работы".
DEFAULT_FIELD_MAP: dict[str, tuple[str, str]] = {
    "fio": ("fio", "ФИО"),
    "phone": ("phone", "Телефон"),
    "email": ("email", "Email"),
    "snils": ("snils", "СНИЛС"),
    "object_address": ("object_address", "Адрес объекта"),
    "object_cadnum": ("object_cadnum", "Кадастровый номер"),
    "work_main": ("work_main", "Основной вид работ"),
    "work_extra": ("work_extra", "Дополнительные виды работ"),
    "cadastral_engineer": ("cadastral_engineer", "Кадастровый инженер"),
    "deadline": ("deadline", "Срок"),
    "budget": ("budget", "Бюджет"),
    "notes": ("notes", "Комментарий"),
    "consent_pdn": ("consent_pdn", "Согласие на ПДн"),
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


def build_deal_fields(
    parsed: dict[str, str],
    *,
    funnel_id: int,
    responsible_id: int,
) -> dict[str, Any]:
    """Compose crm.deal.add fields from the parsed payload."""
    title_parts: list[str] = []
    work_main = parsed.get("work_main", "").strip()
    if work_main:
        title_parts.append(work_main[:80])
    addr = parsed.get("object_address", "").strip()
    if addr:
        title_parts.append(addr[:80])
    fio = parsed.get("fio", "").strip()
    if not title_parts:
        title_parts = ["Заявка с Яндекс Формы"]
    title = " — ".join(title_parts)

    comments: list[str] = []
    for key, label in DEFAULT_FIELD_MAP.values():
        val = parsed.get(key, "").strip()
        if not val:
            continue
        comments.append(f"{label}: {val}")
    extra_notes = parsed.get("notes", "").strip()
    if extra_notes:
        comments.append(f"--- Доп. комментарий ---\n{extra_notes}")
    comment_text = "\n".join(comments) if comments else "(пусто)"

    fields: dict[str, Any] = {
        "TITLE": title[:255],
        "CATEGORY_ID": funnel_id,
        # In funnel 3 (Проекты 2КАД ЮЛ/ФЛ) the first stage id is "C3:NEW".
        # Hardcoding "NEW" would create the deal outside the funnel. Verified
        # 2026-06-27 via crm.status.list DEAL_STAGE_3.
        "STAGE_ID": f"C{funnel_id}:NEW",
        "RESPONSIBLE_ID": responsible_id,
        "OPENED": "Y",
        "COMMENTS": comment_text,
        "SOURCE_ID": "WEB",
        "SOURCE_DESCRIPTION": "Яндекс Форма (webhook answer.created)",
    }

    phone = parsed.get("phone", "").strip()
    if phone:
        fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}]
    email = parsed.get("email", "").strip()
    if email:
        fields["EMAIL"] = [{"VALUE": email, "VALUE_TYPE": "WORK"}]
    # Custom fields commonly used by 2KAD CRM — write only if the operator
    # confirms the UF codes exist on CATEGORY_ID=3. Safe-no-op if absent.
    # These are intentionally commented to avoid crm.deal.add 400s on
    # unknown userfields until we confirm codes.
    # fields["UF_CRM_OBJECT_CADDRESS"] = addr
    # fields["UF_CRM_WORK_MAIN"] = work_main
    # fields["UF_CRM_OBJECT_CADNUMBER"] = parsed.get("object_cadnum", "")
    return fields
