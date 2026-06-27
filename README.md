# 2KAD Yandex Leads

Webhook bridge: **Yandex Forms → Bitrix24** for ООО «2КАД».

## What it does

1. Yandex Form fires `answer.created` to `POST /webhook/yandex`.
2. Service parses the payload (`app/yandex_parser.py`) into canonical fields:
   `fio, phone, email, snils, object_address, object_cadnum, work_main, work_extra, deadline, budget, notes, consent_pdn`.
3. Creates a `crm.deal.add` in the unified 2KAD funnel (`CATEGORY_ID=3`).
4. Posts a brief to the deal timeline via `crm.timeline.comment.add` (works on on-prem Bitrix; `im.*` API is unavailable on this version).
5. Optionally pings the owner on Telegram.

Folder creation on `D:\1.4 Лиды. ЮЛ\` is **not** done here (Dokploy ≠ 2KAD server). A separate cron `2kad-yandex-leads-folder-watcher` on the 2KAD server watches the funnel and creates the project folder.

## Why this exists

Before: form answers came in only via Yandex's email hook to `info@2kad.ru`. No CRM lead, no folder, no operator visibility.

After: every form submission becomes a deal in Bitrix24 automatically, with the same data Yandex would have emailed.

## Endpoints

- `GET  /healthz` — liveness + funnel id.
- `POST /webhook/yandex` — production handler. Header `X-Webhook-Secret` enforced if `WEBHOOK_SHARED_SECRET` env is set.
- `POST /webhook/test` — dry-run; returns parsed fields without creating a deal.

## Auth

Two modes, decided at boot:

- **Webhook token** (preferred): `BITRIX_WEBHOOK_TOKEN=<user_id>/<token>` from Bitrix admin → Приложения → Вебхуки (Исходящий вебхук с правами CRM). Used as `POST https://bitrix.a2kad.ru/rest/<user>/<token>/<method>.json`.
- **Cookie** (fallback): `BITRIX_SESSION_JSON=<paste of .bitrix-session.json>`.

If both are set, webhook takes precedence.

## Deploy (Dokploy)

```bash
cd "D:/11. 2KAD_Soft/My projects/2kad-yandex-leads"
git init && git add . && git commit -m "init: 2kad-yandex-leads webhook bridge"
git remote add origin <repo_url> && git push -u origin main
```

Then in Dokploy: `cuzr_official` → `production` → **+ Application** → type `nixpacks` (or `dockerfile`) → point at the repo → set env from `.env.example` → Deploy.

After first successful deploy, copy the public URL and register it on the form:

```bash
python ~/.mavis/skills/2kad-yandex-form/scripts/yandex_form.py \
    add-hook \
    --survey-id 6a3ec49cbf4fde2e19a7717f \
    --hook-type webhook \
    --event answer.created \
    --url "https://<your-app>.dev.ii4ki.ru/webhook/yandex"
```

## Local test

```bash
python -m venv .venv && . .venv/bin/activate    # or .venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
cp .env.example .env  # fill in BITRIX_WEBHOOK_TOKEN or BITRIX_SESSION_JSON
uvicorn app.main:app --reload --port 8000

# In another shell:
curl -X POST http://localhost:8000/webhook/test \
    -H "Content-Type: application/json" \
    -d @sample_payload.json
```

## Form payload

`sample_payload.json` shows the typical Yandex webhook shape; see `app/yandex_parser.py::normalise_payload` for the supported variations.

## Owner

Zotkin Evgeny (info@2kad.ru). TG: @Evgeninho69 (tg_id 429471588).
