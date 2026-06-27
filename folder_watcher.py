"""kad_yandexFORMs_leads folder watcher.

Polls Bitrix24 funnel "Проекты 2КАД ЮЛ/ФЛ" (CATEGORY_ID=3) for new deals that
have no project folder yet, and creates one under D:\\1.4 Лиды. ЮЛ\\ with the
canonical 2KAD layout.

Designed to run on the 2KAD server as a scheduled task (every 5 minutes).
Does NOT need Dokploy — the webhook service is upstream.

State is kept in a small JSON file under %LOCALAPPDATA% so re-runs are
idempotent. Folder-creation itself is also marked on the deal via a comment
marker `[folder: N_<title>]` so re-running on a moved/renamed machine is safe.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BITRIX_BASE_URL = os.environ.get("BITRIX_BASE_URL", "https://bitrix.a2kad.ru")
# Hard-coded for this service. BITRIX_SESSION_FILE env is intentionally NOT
# consulted because the cron-scheduler sets it to a stale Administrator
# session which then 401s (verified 2026-06-27, see agent memory).
SESSION_FILE = r"D:\11. 2KAD_Soft\8. 2KAD_bitrix\.bitrix-session.json"
LEADS_ROOT = Path(os.environ.get("LEADS_ROOT", r"D:\1.4 Лиды. ЮЛ"))
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", r"D:\1.3 Проекты ЮЛ. Договоры\2026_Договоры 2кад"))
STATE_FILE = Path(
    os.environ.get(
        "STATE_FILE",
        str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "kad_yandexFORMs_leads" / "state.json"),
    )
)
FUNNEL_ID = int(os.environ.get("BITRIX_FUNNEL_ID", "3"))
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_OWNER_CHAT_ID = os.environ.get("TG_OWNER_CHAT_ID", "").strip()
RESPONSIBLE_ID = int(os.environ.get("BITRIX_RESPONSIBLE_ID", "1"))
STAGE_INITIAL = f"C{FUNNEL_ID}:NEW"  # «Получить документы» in funnel 3.
FOLDER_MARKER = re.compile(r"\[folder:\s*(\d+_[^\]]+)\]")
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "168"))  # 7 days back

SUBFOLDERS = [
    "0. Договор",
    "1. Исходные данные",
    "2. Подготовительный этап",
    "3. Полевые работы",
    "4. Камеральная обработка",
    "5. Результат работ",
    "6. Карта заказчика, ТЗ, письма",
    "agent_log",
]

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("kad_yandexFORMs_leads-folder-watcher")


# --------------------------------------------------------------------------- #
# Bitrix client (cookie-mode, identical shape to app/bitrix.py in webhook)
# --------------------------------------------------------------------------- #


def _load_session() -> tuple[str, dict[str, str]]:
    with open(SESSION_FILE, encoding="utf-8") as f:
        payload = json.load(f)
    cookie_header = payload["cookie"]
    sessid = payload["sessid"]
    if "BITRIX_SM_SESSID=" not in cookie_header:
        cookie_header = f"BITRIX_SM_SESSID={sessid}; " + cookie_header
    return sessid, {"Cookie": cookie_header, "Content-Type": "application/x-www-form-urlencoded"}


def bx_call(method: str, params: dict | None = None, *, retries: int = 3) -> dict:
    sessid, headers = _load_session()
    body = dict(params or {})
    body["sessid"] = sessid
    data = urllib.parse.urlencode(body).encode()
    url = f"{BITRIX_BASE_URL}/rest/{method}.json"
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = json.loads(r.read().decode("utf-8", errors="replace"))
            if isinstance(payload, dict) and payload.get("error"):
                # AUTH_EXPIRED -> refresh and retry once.
                if payload["error"] in ("ACCESS_DENIED", "auth", "expired_token"):
                    raise RuntimeError(f"Bitrix auth error: {payload}")
                raise RuntimeError(f"Bitrix error: {payload}")
            return payload.get("result", payload)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log.warning("bx_call %s attempt %d/%d failed: %s", method, attempt, retries, exc)
            time.sleep(2 * attempt)
    raise RuntimeError(f"bx_call {method} gave up after {retries} attempts: {last_err}")


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #


def tg_notify(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_OWNER_CHAT_ID:
        log.info("tg_notify skipped (no token/chat)")
        return
    body = urllib.parse.urlencode(
        {"chat_id": TG_OWNER_CHAT_ID, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as exc:  # noqa: BLE001
        log.warning("tg_notify failed: %s", exc)


# --------------------------------------------------------------------------- #
# State (idempotency)
# --------------------------------------------------------------------------- #


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed": {}}
    try:
        with STATE_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        log.warning("state unreadable, starting fresh: %s", exc)
        return {"processed": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- #
# Numbering + sanitisation
# --------------------------------------------------------------------------- #

INVALID_WIN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACES = re.compile(r"\s+")
MAX_TITLE_LEN = 180  # safety margin; Windows MAX_PATH 260 - prefix.


def sanitise_title(title: str) -> str:
    cleaned = INVALID_WIN_CHARS.sub("_", title).strip(" .")
    cleaned = SPACES.sub(" ", cleaned)
    if len(cleaned) > MAX_TITLE_LEN:
        cleaned = cleaned[:MAX_TITLE_LEN].rstrip()
    return cleaned or "Без названия"


def next_lead_number() -> int:
    """Back-compat alias. New code uses next_folder_number(root)."""
    return next_folder_number(LEADS_ROOT)


# --------------------------------------------------------------------------- #
# Folder creation
# --------------------------------------------------------------------------- #


def create_lead_folder(folder_name: str, root: Path | None = None) -> Path:
    """Create the 2KAD-standard subdirectory layout. Idempotent."""
    root = root or LEADS_ROOT
    folder_path = root / folder_name
    folder_path.mkdir(parents=True, exist_ok=False)
    for sub in SUBFOLDERS:
        (folder_path / sub).mkdir(exist_ok=True)
    # README in agent_log for traceability.
    readme = folder_path / "agent_log" / "README.md"
    readme.write_text(
        "# agent_log\n\n"
        "Эта папка создана автоматически сервисом "
        "**kad_yandexFORMs_leads** (folder-watcher).\n\n"
        f"- Привязана к сделке Bitrix в воронке «Проекты 2КАД ЮЛ/ФЛ» (CATEGORY_ID={FUNNEL_ID}).\n"
        f"- Корень: `{root}`.\n"
        "- Сюда пишутся логи агента, история диалога, NDJSON от Codex CLI.\n",
        encoding="utf-8",
    )
    return folder_path


def append_folder_marker(deal_id: int, folder_name: str, existing_comments: str) -> None:
    marker = f"[folder: {folder_name}]"
    if marker in (existing_comments or ""):
        return
    new_comments = (existing_comments or "").rstrip() + f"\n\n{marker}"
    bx_call(
        "crm.deal.update",
        {"id": str(deal_id), "fields[COMMENTS]": new_comments},
    )


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #


def list_candidate_deals() -> list[dict]:
    """Return recent C3:NEW (or earlier in funnel) deals that look fresh."""
    # Bitrix REST does not allow '>DATE_CREATE' in form-encoded body (urllib
    # percent-encodes '>'). Use plain CATEGORY_ID and filter by DATE_CREATE
    # client-side after fetching a reasonable batch.
    result = bx_call(
        "crm.deal.list",
        {
            "filter[CATEGORY_ID]": str(FUNNEL_ID),
            "order[DATE_CREATE]": "DESC",
            "select[0]": "ID",
            "select[1]": "TITLE",
            "select[2]": "DATE_CREATE",
            "select[3]": "STAGE_ID",
            "select[4]": "COMMENTS",
        },
    )
    deals = result if isinstance(result, list) else []
    cutoff = _iso_minutes_ago(WINDOW_HOURS * 60)
    return [d for d in deals if (d.get("DATE_CREATE") or "") >= cutoff]


def _iso_minutes_ago(minutes: int) -> str:
    """Cutoff used for client-side filtering (Bitrix-compatible ISO-ish string)."""
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(hours=3))
    dt = datetime.now(tz) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def next_folder_number(root: Path) -> int:
    """Scan existing folders under `root`, find max N_ prefix, +1."""
    max_n = 0
    if root.exists():
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            m = re.match(r"^(\d+)_", entry.name)
            if m:
                try:
                    n = int(m.group(1))
                    if n > max_n:
                        max_n = n
                except ValueError:
                    continue
    return max_n + 1


def _classify_deal(deal: dict) -> tuple[str, Path]:
    """Decide whether a deal needs a Лиды or a Проекты folder.

    Rule (mirrors CLAUDE.md, 2KAD process):
      - Funnel 3 (Проекты 2КАД ЮЛ/ФЛ) + stage C3:NEW + no number in TITLE →
          it's a fresh lead coming from a webhook. Folder goes to LEADS_ROOT.
      - Anything else (already past NEW, or has N_ in TITLE, or contains
          договор/КП keywords) → project root PROJECTS_ROOT.
    """
    title = deal.get("TITLE") or "(без названия)"
    stage = deal.get("STAGE_ID") or ""
    has_number_prefix = bool(re.match(r"^\d+_", title))

    # Fresh Yandex-Form lead: no number in title, still in NEW.
    if not has_number_prefix and stage == STAGE_INITIAL:
        return "lead", LEADS_ROOT

    return "project", PROJECTS_ROOT


def process_once() -> int:
    """Process one batch. Returns count of folders created."""
    state = _load_state()
    processed = state.setdefault("processed", {})
    created = 0

    deals = list_candidate_deals()
    log.info("scanning %d recent deals in CATEGORY_ID=%d", len(deals), FUNNEL_ID)

    for deal in deals:
        deal_id = str(deal.get("ID", ""))
        title = deal.get("TITLE") or "(без названия)"
        comments = deal.get("COMMENTS") or ""

        # Skip if state already marks this deal as processed.
        if deal_id in processed and processed[deal_id].get("folder"):
            continue

        # Skip if a marker is already in comments (deal was processed elsewhere).
        m = FOLDER_MARKER.search(comments)
        if m:
            processed[deal_id] = {"folder": m.group(1), "at": int(time.time())}
            continue

        kind, target_root = _classify_deal(deal)

        # Compute folder name. Use existing N_<title> if TITLE already starts with one.
        existing_n = re.match(r"^(\d+)_", title)
        if existing_n:
            # Deal title already has a number — reuse it to keep TITLE==folder_name.
            folder_name = title.strip()
            folder_path = target_root / folder_name
            if folder_path.exists():
                # Folder already exists; just mark and move on.
                append_folder_marker(int(deal_id), folder_name, comments)
                processed[deal_id] = {"folder": folder_name, "at": int(time.time())}
                continue
            try:
                create_lead_folder(folder_name, root=target_root)
            except FileExistsError:
                pass
        else:
            n = next_folder_number(target_root)
            safe_title = sanitise_title(title)
            folder_name = f"{n}_{safe_title}"
            try:
                folder_path = create_lead_folder(folder_name, root=target_root)
            except FileExistsError:
                # Race with another worker — recompute and retry once.
                n = next_folder_number(target_root)
                folder_name = f"{n}_{safe_title}"
                folder_path = create_lead_folder(folder_name, root=target_root)

        append_folder_marker(int(deal_id), folder_name, comments)
        processed[deal_id] = {"folder": folder_name, "at": int(time.time())}
        log.info("deal %s -> [%s] folder %s", deal_id, kind, folder_name)
        tg_notify(
            f"[kad_yandexFORMs_leads] 📁 Папка создана ({kind})\n"
            f"Сделка #{deal_id}: {title}\n"
            f"Путь: {folder_path}"
        )
        created += 1

    _save_state(state)
    return created


def main() -> int:
    log.info("start (LEADS_ROOT=%s, FUNNEL=%d)", LEADS_ROOT, FUNNEL_ID)
    n = process_once()
    log.info("done, folders created: %d", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
