"""SMTP auto-reply to Yandex Forms customers.

Best-effort: failure to send never blocks the webhook. Used after
crm.deal.add succeeds, so the customer gets an instant acknowledgement
while we work on the actual deal.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def is_configured() -> bool:
    """True если SMTP полностью настроен (пароль непустой)."""
    return bool(_get_env("YANDEX_SMTP_PASSWORD")) and bool(_get_env("YANDEX_SMTP_USER"))


def send_customer_reply(
    to_email: str,
    *,
    fio: str,
    deal_id: int,
    work_summary: str,
) -> bool:
    """Отправляет клиенту подтверждение получения заявки.

    Returns True если письмо ушло, False при любой ошибке (никогда не падает).
    """
    if not to_email:
        logger.info("auto-reply skipped: empty recipient")
        return False
    if not is_configured():
        logger.info("auto-reply skipped: SMTP not configured")
        return False

    host = _get_env("YANDEX_SMTP_HOST", "smtp.yandex.ru")
    port = int(_get_env("YANDEX_SMTP_PORT", "587"))
    user = _get_env("YANDEX_SMTP_USER", "info@2kad.ru")
    password = _get_env("YANDEX_SMTP_PASSWORD")
    from_name = _get_env("YANDEX_SMTP_FROM_NAME", "ООО Центр недвижимости 2КАД")

    first_name = "уважаемый клиент"
    if fio:
        parts = fio.split()
        if len(parts) >= 2:
            first_name = parts[1]
        elif parts:
            first_name = parts[0]

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
        logger.info("auto-reply sent to %s (deal %s)", to_email, deal_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto-reply failed: %s", exc)
        return False
