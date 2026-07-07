# kad_yandexFORMs_leads — handover

## Текущее состояние (2026-07-07 23:55 MSK)

Webhook-bridge Яндекс Формы → Bitrix24 **задеплоен и работает** на Dokploy `dev.ii4ki.ru`.
E2E-тест 23:55: тестовый POST → сделка #5490 в воронке 3 + авто-ответ на email доставлен.

## История (короткая хронология)

- **2026-06-27 19:48** — код готов, не задеплоен (serverId=null, Nixpacks build failed без Procfile).
- **2026-07-07 23:35** — получены Bitrix webhook token + Yandex SMTP password.
- **2026-07-07 23:50** — Nixpacks всё ещё падает, добавлены `Procfile` + `nixpacks.toml`, переключили buildType на `dockerfile`.
- **2026-07-07 23:53** — `application.deploy` → `status: done` за 6 итераций polling. `/healthz` отвечает 200.
- **2026-07-07 23:55** — `domain.create` для `kad-yandexFORMs-leads.dev.ii4ki.ru` → `https://kad-yandexFORMs-leads.dev.ii4ki.ru/healthz` ✅. Webhook Яндекс Формы v6 зарегистрирован. E2E: сделка #5490 + email доставлен.
- **2026-07-08 00:00** — skill `kad-it-serv-ii4ki` обновлён (Local Server + Procfile + zodError gotchas), skill уроки зафиксированы в agent memory.
- **2026-07-08 00:05** — форма v6 Яндекса снята с публикации, webhook удалён, форма удалена. Заменена на wizard-форму `kad_yandexFORMs_wizard` (пошаговая, на нашем Dokploy).
- **2026-07-08 00:38** — wizard-форма `kad-yandexFORMs-wizard` задеплоена в Dokploy (app id `HT3G4MUBlV4B3hOlVJDwh`, project `kad_yandexFORMs_leads/production`). `https://kad-yandexFORMs-wizard.dev.ii4ki.ru/` — LIVE. E2E: сделка #5491 + email ✅.

## Что есть на диске

- **Репо:** `D:\11. 2KAD_Soft\My projects\kad_yandexFORMs_leads\`
  - `app/main.py` — FastAPI, эндпоинты `/healthz`, `/webhook/yandex`, `/webhook/test`.
  - `app/bitrix.py` — клиент Bitrix (cookie / webhook-token).
  - `app/yandex_parser.py` — нормализация payload Яндекса → canonical fields → `crm.deal.add` fields.
  - `app/notify.py` — best-effort TG notify.
  - `folder_watcher.py` — poll Bitrix → создаёт папки в `D:\1.4 Лиды. ЮЛ\` (лиды) или `D:\1.3 Проекты ЮЛ. Договоры\2026_Договоры 2кад\` (проекты).
  - `install_watcher_task.ps1` — установка scheduled task (нужен Admin).
  - `Dockerfile`, `requirements.txt`, `sample_payload.json`, `.env.example`, `README.md`.
- **GitHub:** https://github.com/evgeninho69/kad_yandexFORMs_leads (4 коммита, main).
- **Dokploy app:** `kad_yandexFORMs_leads`, id `WM14fnxkZB-I-MAuq70jr`, project `cuzr_official`, env `production`. Build=nixpacks, source=git (https://github.com/evgeninho69/kad_yandexFORMs_leads.git, branch=main). Env установлены (включая `BITRIX_SESSION_JSON` с актуальной сессией `info@2kad.ru`).
- **REGISTRY.md:** `D:\11. 2KAD_Soft\9. Yandex\Forms\REGISTRY.md` — обновлён, есть раздел «Авто-обработка».

## Что работает уже сейчас

- **Folder-watcher** (если запустить руками или из Admin-PowerShell через `install_watcher_task.ps1`):
  - идемпотентный (state.json + `[folder: N_<title>]` маркер в `COMMENTS` сделки),
  - корректно классифицирует лиды vs проекты,
  - 27.06.2026 закрыл 11 исторических сделок (5 лидов №72-75, 6 проектов №3995-4003),
  - STAGE_ID сделки для новой заявки должен быть `C3:NEW` (захардкожен в webhook-сервисе).
- **Webhook-сервис:** код готов, не задеплоен.
- **GitHub:** код публично доступен.

## Что НЕ работает прямо сейчас (блокеры)

1. **Dokploy server not registered** — **РЕШЕНО 2026-07-07**. В v0.29 явная регистрация Local-сервера не нужна — Dokploy-инстанс деплоит «куда угодно» без `serverId`. Достаточно `application.deploy` + `domain.create` для проброса наружу.

2. **Scheduled task для folder-watcher не установлен.**
   - Решение: открыть PowerShell **от Администратора** на 2KAD-сервере и выполнить:
     ```
     powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\11. 2KAD_Soft\My projects\kad_yandexFORMs_leads\install_watcher_task.ps1"
     ```
   - Текущая ошибка: `Register-ScheduledTask : Отказано в доступе. (HRESULT: 0x80070005)` — нужно повышение привилегий.
   - Альтернатива без Admin: `schtasks /create /tn "kad_yandexFORMs_leads-folder-watcher" /tr "..." /sc minute /mo 5 /ru SYSTEM` (если SYSTEM-аккаунт доступен).

## Что нужно сделать после починки блокеров

1. После регистрации Dokploy-сервера:
   - `application.update { applicationId: WM14fnxkZB-I-MAuq70jr, serverId: <new-id> }`
   - Триггернуть `application.deploy`.
   - Дождаться `applicationStatus: done` (poll каждые 5с).
   - Скопировать публичный URL приложения (формат `https://kad-yandexFORMs-leads.<host>/`).
2. Зарегистрировать webhook на Яндекс Форме:
   ```
   python ~/.mavis/skills/2kad-yandex-form/scripts/yandex_form.py add-hook \
     --survey-id 6a3ec49cbf4fde2e19a7717f \
     --hook-type webhook \
     --event answer.created \
     --url "https://<dokploy-app-url>/webhook/yandex"
   ```
3. E2E тест: заполнить форму в браузере → проверить, что сделка появилась в воронке 3 (`C3:NEW`) → проверить, что folder-watcher создал папку (после следующего 5-мин тика).

## Известные gotchas (для следующего разработчика)

- **PowerShell 5.1 + кириллица в путях** ломает парсинг heredoc/скриптов. Решение: `PYTHONIOENCODING=utf-8` + `sys.stdout.reconfigure(encoding="utf-8")` в Python-скриптах; избегать кириллицы в PowerShell-скриптах.
- **`BITRIX_SESSION_FILE` env** на этом хосте указывает на `C:\Users\Administrator\.bitrix-session.json` (чужая сессия, 401). Folder-watcher **жёстко прописан** на правильный путь `D:\11. 2KAD_Soft\8. 2KAD_bitrix\.bitrix-session.json` — env override игнорируется. Это зафиксировано в коде.
- **Webhook токен предпочтительнее cookie-mode.** Cookie работает, но протухает. Когда владелец выдаст webhook-token — вставить его в `BITRIX_WEBHOOK_TOKEN` env в Dokploy.
- **STAGE_ID** в `crm.deal.add` для воронки 3 = `C3:NEW` (НЕ `NEW`). Если когда-нибудь добавится ещё одна воронка — нужно пересмотреть.
- **Dokploy API требует все 5 полей buildType** (`dockerfile`, `dockerContextPath`, `dockerBuildStage`, `herokuVersion`, `railpackVersion`) даже если используется только одно. Хелпер `ii4ki.mjs create-app` шлёт неполный payload — workaround: создавать через `raw application.create`, потом `application.saveBuildType` (тоже неполноценно) или через UI.
