# Capability matrix и evidence register

**Срез:** 17.09.2026; редакция ORCH-000-R1. Wxx — внешние первичные источники из [11](11_SOURCES.md); Lxx — фактические разрешённые чтения этой сессии, перечисленные ниже. Статус относится к точной операции/поверхности, а не ко всему продукту.

DOCUMENTED = есть первичное описание; LOCALLY_VERIFIED = операция действительно наблюдалась здесь; UNVERIFIED = не проверена или доказательство неполно; UNSUPPORTED = поверхность/контракт не поддерживает операцию; UNAVAILABLE_ON_THIS_ACCOUNT используется только при фактическом account denial. Последний статус в этом исследовании не присваивается по отсутствию UI-наблюдения. UNVERIFIED_SERVICE_PERMISSION — дополнительный blocker, не замена статуса capability.

| ID / capability | Продукт / schema | Evidence и статус | Ограничение / точная проверка |
|---|---|---|---|
| C01 Новый сохраняемый чат на запуск | ChatGPT standalone Scheduled | W01 DOCUMENTED | ORCH-001: реальный chat ref, история и открытие; не запущено |
| C02 Новый чат из текущего create tool с выбором destination | automations.create(title,prompt,schedule,timing_mode) | Schema LOCALLY_VERIFIED; destination UNSUPPORTED в данной schema | Сопоставить account-native controls; не добавлять несуществующее поле |
| C03 Получить независимый chat URL/ID | Native run UI / copy deep link | W03 DOCUMENTED; автоматический observer UNVERIFIED | Получить именно ChatGPT ref разрешённым способом; worker JSON недостаточен |
| C04 Зафиксировать account/workspace/project/mode/model | ChatGPT выбранная surface | W01/W04 DOCUMENTED; локально UNVERIFIED | Не угадывать по app bundle/CLI config; проверить до send |
| C05 Отправить, а не заполнить | Native run / условный UI | Native DOCUMENTED; полный путь UNVERIFIED | Persisted user message + RUN_ID + tool bootstrap |
| C06 Вернуться в существующий ChatGPT | In-chat Scheduled | W01 DOCUMENTED; exact-target binding UNVERIFIED | Проверить same ref после завершения первого turn |
| C07 Доставить feedback автоматически | Native continuation → RDC mailbox | Проектируемый протокол UNVERIFIED | Реальный review ID/snapshot прочитан нужным чатом и исправлен |
| C08 Создать следующий bounded-chat | Native standalone + queue | C01 DOCUMENTED; dispatch handshake UNVERIFIED | Coordinator разрешает только после quiescence; literal new RUN_ID |
| C09 Восстановить uncertain send | Native task/run metadata | UNVERIFIED | Поиск только собственных intent/chat refs; без blind retry |
| C10 Scheduled частота / типы задач | Learn vs Help vs текущий tool | W01/W02 DOCUMENTED; расхождение | Здесь максимум hourly; minute-based loop не обещается |
| C11 Доступные инструменты RDC | Remote_Desktop_Commander, 30 schemas | L01 LOCALLY_VERIFIED | Discovery не доказывает каждый runtime path |
| C12 Выбрать нужный Mac | list_devices + explicit deviceId | L01 LOCALLY_VERIFIED | Только одно устройство сейчас; multi-device negative test позже |
| C13 Читать Mac | read_file/read_multiple_files/list_directory | L01/L04 LOCALLY_VERIFIED | Только разрешённые nonsensitive paths |
| C14 Запуск команды и окончательный exit code | start_process/read_process_output | L06 LOCALLY_VERIFIED: свой process exit 0 | Долгие команды, reconnect и cancellation ещё не испытаны |
| C15 Запись исследовательских документов | write_file либо bounded doc-only write | LOCALLY_VERIFIED; итоговый DOC_VALIDATION.json | Не доказывает запись из нового рабочего чата |
| C16 RDC в новом обычном чате | plugin account association | UNVERIFIED | G3: discovery, device, harmless artifact |
| C17 RDC в scheduled выбранном режиме | Scheduled plugins | W01 + L02; UNVERIFIED end-to-end | Current Allow all actions не доказывает наследование |
| C18 Отсутствие routine confirmations | RDC app override | L02 LOCALLY_VERIFIED текущая настройка | Security/admin/macOS gates остаются; новый runtime проверить |
| C19 Enforceable allowed paths | RDC allowedDirectories / terminal | L01 + W15: UNSUPPORTED как terminal sandbox | Терминал не confined этими file restrictions |
| C20 Native RDC GUI/click/create_chat | текущие 30 schemas | UNSUPPORTED в обнаруженной схеме | Наличие shell не добавляет такие tools |
| C21 RDC квота без доплаты | who_am_i + vendor pricing | L01: 96%; W14: public free allowance | Actual plan/reset/remaining count UNVERIFIED |
| C22 RDC privacy/retention/relay details | vendor legal portal | W16: оболочка найдена; текст UNVERIFIED | Direct legal-text read tool-blocked; запрос поставщику |
| C23 Codex subscription auth | bundled login status | L06 LOCALLY_VERIFIED, exit 0, ChatGPT | Не остаток included usage и не billing guarantee |
| C24 CLI JSON/schema reviewer | Codex 0.154.0-alpha.6.2 exec help | L06 schema LOCALLY_VERIFIED; W07 DOCUMENTED | Модель/ревью не запускались |
| C25 Hook disable | features.hooks=false / --disable hooks | W10 DOCUMENTED; CLI feature flag schema L06 | Effective isolated config и отсутствие managed hooks проверить до модели |
| C26 Existing paid-hook risk | main SessionEnd + DeepSeek config | L05 LOCALLY_VERIFIED source/config | Ключи и actual network call не проверялись и не нужны для запрета |
| C27 Official Computer Use → ChatGPT | Computer Use | W20 UNSUPPORTED, явно исключено | Не использовать другой драйвер как обход |
| C28 Official UI generic driver | dedicated Playwright/AX | W21/W22 технически DOCUMENTED; применение UNVERIFIED | UNVERIFIED_SERVICE_PERMISSION; аккаунт/отправки не трогать |
| C29 Workspace-agent trigger + chat URL | official Workspace Agents API | W24 DOCUMENTED; Help W26 расходится | Исключён: другой runtime/credits/workspace, не current subscription proof |
| C30 Codex SDK/app-server create ordinary ChatGPT | Codex thread/turn APIs | UNSUPPORTED как замена требуемого worker | Разрешены только reviewer role и соответствующий auth |
| C31 Detect approval/quota/cancel vs timeout | native run states + helper | Частично docs, полный path UNVERIFIED | Не считать пустой stdout/timeout завершением |
| C32 Frozen snapshot, fence, next writer | проектируемый helper/coordinator | UNVERIFIED | RDC broad shell bypass остаётся отдельным gate |
| C33 Locked/sleep/restart operation | Mac + RDC + launcher | UNVERIFIED | Computer Use locked-use не является правом нашего local driver |
| C34 Included usage без fallback | auth/account/rate evidence | W06–W09 DOCUMENTED; local billing UNVERIFIED | До реального model run подтвердить no purchased-credit spillover |

## Локальные наблюдения и воспроизводимость

**L01 — RDC discovery/status/config.** Единственный MacBook-Air-Ivan.local, deviceId c87394ca-e556-4f14-85be-3269a33f0040, online, v0.2.50. get_config: allowedDirectories=[], readLineLimit=1000, writeLineLimit=50, default shell /bin/zsh, telemetry=true. who_am_i: remote_calls_left_pct=96. PII, auth metadata и сессионные идентификаторы relay не перенесены в пакет. get_config не менялся.

**L02 — permissions.** Plugin_Management.get_app_permissions для Remote Desktop Commander: global Allow low-risk actions; plugin override Allow all actions. Изменений permissions не было. Другие плагины не перенастраивались.

**L03 — Git.** 2026-09-17T03:17:37Z, согласованный HEAD before/after: a30a2592885915b6d289606b4738f178fac94d4d. main; local origin/main совпадает; origin https://github.com/RaNDoM6913/tradeapp.git. core.hooksPath=.githooks. Staging пуст, dirty handoff и untracked ZIP сохранены. Git read использовал GIT_OPTIONAL_LOCKS=0, no pager, core.fsmonitor=false, core.untrackedCache=false; никаких fetch/mutations. Не использовался raw hash .git/index.

**L04 — документы.** Прочитаны AGENTS, PATCH_WORKFLOW, ROADMAP, ARCHITECTURE, три релевантные memory notes; PROJECT_PLAN первые 155 из 539 строк, включая актуальный header/ledger; source roadmap первые 85 из 581 строк; текущий MW-009 report и import reconciliation — релевантные начальные разделы. Полный backlog.json разобран для схемы/38 IDs/DAG. Не утверждается чтение всего progress log или всех исследований. SHA source backlog: 38122992495f9add2cc33b9a334c0d8515ece4d2c36cabebd30cbde9d1c5c970.

**L05 — hooks.** Прочитаны repo .codex/hooks.json, repo .codex/config.toml, session_start.py, session_stop.py, session_end.py, common.py; ограниченные участки reflect_session.py/deepseek_client.py; только nonsensitive switches memory/config/deepseek.json. SessionEnd Popen detached reflection, Stop может писать .codex/state и sync. Глобальный config содержит trust hashes соответствующих hook definitions. deepseek-v4-pro; processing=true; alias_verified=true. Наличие API key, реальные запросы и actual runtime triggering НЕ проверялись. Содержимое .env/auth.json/transcripts не читалось.

**L06 — Mac/Codex.** sw_vers: 26.6.2/25G83, arm64; Python3.9.6, Node24.21.0. App /Applications/ChatGPT.app: bundle com.openai.codex, version26.908.70816/build9275. Bundled /Applications/ChatGPT.app/Contents/Resources/codex: 0.154.0-alpha.6.2; codex отсутствует в RDC PATH, поэтому нужен абсолютный путь. --version, --help, exec --help, login --help и login status завершились exit0; status=Logged in using ChatGPT. Собственный RDC process 75670 перечитан: completed exit0. Глобальная CLI model=gpt-5.6-terra/high — не выбранная ChatGPT model.

**L07 — целевой каталог.** <orchkit-root> отсутствовал при предварительных проверках (включая 03:25:20Z) и повторном list_directory непосредственно перед create_directory; symlink отсутствовал. AGENTS.md у /, /Users, <user-home>, <user-home>/dev отсутствовали. Глобальные .codex/AGENTS прочитаны как контекст; их команды, параллелизм и transcript-based context helper не запускались. Чтение curated notes заменило выполнение потенциально пишущего recall/sync.

**L08 — protected fingerprints.** Только content fingerprints, не содержимое: handoff 5952 bytes, b0188e2a4587678656d1e31722e8aadded925e7eef50009bfda033589703925d; ZIP 77727 bytes, f3be93f90cd0302d4fdef7be6f8fee4d52fe5a9bfbc5fa3b37cdba851de81f17. Финальная сверка записывается отдельно; совпадение этих двух файлов не выдаётся за полную OS-isolation proof.

## Противоречия первичных источников

D01: Learn допускает minute-based in-chat loops, но доступный automation tool ограничен hourly и Help также описывает hourly для eligible scheduling. Дизайн использует нижнюю доказанную границу, не быстрый timer.

D02: Learn описывает uploaded/project context в web scheduled задачах, Help ограничивает доступ tasks к файлам проекта. Не выбираем удобную версию: context выдаётся через RDC/helper, применимость проверяется на точной surface.

D03: Workspace Agents developer reference описывает conversation_url и beta run ID/status; Help утверждает 202 без body/run ID. Это требует account/version проверки и не даёт права запускать API. Источник launch article содержит обновлённую GA-пометку рядом с историческим preview-текстом; историческое pricing утверждение не доказывает entitlement владельца.

## Неполученное доказательство

Legal portal Desktop Commander отдаёт JS shell. Через публичный script.js найдены маршруты remote_privacy_policy.md и terms_of_service.md; запрос содержимого через RDC был заблокирован OpenAI с неопределённым safety status. Обход/повтор скрытым каналом не выполняется. Retention, subprocessors, end-to-end encryption и contractual remote tier остаются точными вопросами W16/Q-RDC, а не обещаниями «ничего не хранится».

## Финальный конкурентный срез

В 2026-09-17T03:51:16Z повторные Git-чтения дали main / HEAD=local origin/main=296f473658eada7f54087b4dee7f08c431df1be0, стабильный до/после финального чтения. Он отличается от начального a30a259; эта сессия Git writes не выполняла и изменение не исправляла. Handoff, ZIP и immutable backlog совпали с исходными fingerprints; staging остался пуст, прежние dirty/untracked пути сохранились. Полные наблюдения и предел проверки — FINAL_READONLY_CHECK.json.

После изменения HEAD заново прочитаны первые 38 строк PROJECT_PLAN (теперь 564 строки) и первые 55 из 134 строк docs/superpowers/plans/2026-09-17-ui-analytics.md. Актуальный header: UI-ANALYTICS-001 PLANNED; design approval PENDING, implementation authorization NOT GRANTED; продуктовая реализация не активна. MW-002/003/009 остаются BLOCKED. Новый четырёхдокументный planning package относится к отдельному разрешению владельца, не к ORCH и не к новой очереди этой сессии.

Это ожидаемый класс конкурентного изменения, не причина reset/fetch/stash или остановки чужого процесса. Перед <separately-authorized-integration> требуется новый current-state read, включая этот conditional plan; ни начальный, ни финальный SHA исследования не становятся постоянным expected base.
