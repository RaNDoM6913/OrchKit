# Подписка, hooks и разрешения

**Редакция:** ORCH-000-R1. Правило владельца важнее удобства интеграции: model_api_budget=0. Источники W05–W10, W14–W16, W20, W24–W27; факты L01/L02/L05/L06.

## Политика будущего инструмента

```text
primary_worker = real_chatgpt_conversation
workspace_bridge_preference = existing_remote_desktop_commander
reviewer = codex_with_chatgpt_sign_in
model_api_budget = 0
allow_paid_model_api = false
allow_external_ai = false
allow_extra_paid_credits = false
allow_subscription_upgrade = false
allow_primary_worker_substitution = false
on_quota_exhausted = pause
on_unknown_billing = pause_before_model_run
```

Это поля нашей policy, а не существующие параметры OpenAI. Нельзя представлять конфиг как provider-enforced cap, если сервис не даёт такого управления. Финансовая граница проверяется до запуска, а не только по итоговой стоимости.

## Что подтверждено о расходах

ChatGPT и Platform billing разделены (W09). Вход Codex через ChatGPT поддерживает подписочный доступ (W06/W08); локальный status действительно подтвердил этот тип входа. Но auth mode не доказывает remaining included usage, отсутствие extra credits или их автоматического использования. Work и Codex могут разделять usage pool (W04); вести отдельные wait reasons, не обещая независимые дополнительные квоты.

Перед первой моделью: owner-observed current plan/workspace, model eligibility, included allowance/reset, purchased-credit balance/auto recharge behaviour, отсутствие API/custom-provider fallback. Использовать официальные account/rate interfaces или доступный billing UI без чтения токенов; если критерий нельзя проверить без запрещённого действия, поставить BILLING_UNKNOWN. Не покупать и не «попробовать один запрос» для выяснения цены.

API keys, OPENAI_BASE_URL/proxy/provider overrides, внешние endpoint credentials и ключи сторонних моделей не наследуются в reviewer child. Передавать allowlisted environment, не dump всей среды. Не выводить найденные секреты; при обнаружении только report категории и pause. Не копировать OAuth credentials из auth.json/keychain в coordinator/SDK/proxy.

У RDC собственная квота и тариф. Initial remaining=96%, public free tier=10 000/month, но account plan/reset не установлен. Уровень OpenAI не делает remote bridge бесплатным или безлимитным. Paid RDC upgrade не включается автоматически.

## Текущий платный-hook риск

В основной репе SessionEnd вызывает detached memory/scripts/reflect_session.py. Nonsensitive config подтверждает provider=deepseek, api_model=deepseek-v4-pro, session_processing_enabled=true, model_alias_verified=true; глобальный Codex config содержит trust records этих hooks. При выполнении вызываемый client ищет key в environment или memory/.env. Сам key и этот файл не читались; успешный network вызов не проверялся. Уже наличие включённого пути достаточно, чтобы запретить reviewer session в основной репе.

Stop также может создавать .codex/state и запускать sync; repo MCP hybridtrader_chrome включён. Поэтому «readonly review prompt» или только read-only shell sandbox не гарантируют отсутствие host lifecycle side effects. Не менять существующие глобальные/repo hooks: они обслуживают другую работу.

## Изолированный подписочный reviewer — предлагаемый контракт

Создать после P-SPIKE отдельный CODEX_HOME вне основной репы; owner выполняет официальный login один раз, без copying auth cache. Свежий home отделяет config/plugins/notify/history, но системные/managed layers всё равно нужно инвентаризировать. При конфликте account restriction не логинить/логаутить общий профиль. Не использовать общий app-server daemon.

Указанный binary фиксируется абсолютным путём и версией; upgrade не выполняется автоматически. Snapshot экспортируется в отдельный каталог **без .git, .codex, .env, hooks, launch scripts и symlinks в основную репу**. Исключённые текстовые изменения конфигурации можно передать reviewer как inert diff с явной инструкцией, но не как активные config files. Анализируемый actual module не заменяется фальшивой копией для verifier.

Документированный disable: features.hooks=false; CLI принимает --disable FEATURE. Применять `--disable hooks` на invocation, а не надеяться на пустой `[hooks]`: источники hooks могут объединяться. `--ignore-user-config` игнорирует лишь user config и не является общей гарантией отключения project/system/plugin layers. `--ignore-rules` и dangerously-bypass flags **не применять**.

Проектный argv, НЕ выполненная команда:
```text
<bundled-codex> --disable hooks -a never exec --ignore-user-config
  --sandbox read-only --skip-git-repo-check --json
  --output-schema <approved-review-schema> -C <readonly-export> -
```

Parent передаёт brief через stdin, собирает JSONL/exit/stderr, пишет отчёт сам. `never` означает отсутствие эскалации с возвратом denial, а не обход OS/security approvals. Требуемые sandbox/admin правила нельзя выключать. Model/provider/forced_login_method задаются только после проверки effective config изолированного профиля; forced login mismatch может завершить auth session, поэтому общий профиль не трогать.

До model run проверяются hook source set, feature flag, notify, plugins/MCP, shell startup/env, custom provider/service tier и billing evidence. При managed hook, неотключаемом стороннем запуске или неизвестном egress — blocker. Для последующих tests verifier не получает reviewer/publisher credentials. Нельзя обещать perfect network isolation, пока она не доказана технически.

## Service permission для launcher

Нативный поддерживаемый scheduling выбран раньше UI. EU Terms содержат ограничение на automated/programmatic extraction of data/Output и обход защит; применимость договора зависит от фактического account/workspace (W05). Это не юридическое заключение и не разрешение на browser bot. Перенос результатов через RDC не является лазейкой в условиях сервиса.

Computer Use официально исключает сам ChatGPT и terminal apps (W20). Для этого launcher он UNSUPPORTED. Нельзя пробовать тем же plugin другую формулировку или использовать внешний driver для обхода. B_UI остаётся неактивным концептуальным резервом исключительно при отдельном ясном разрешённом основании именно для выбранного интерфейса/способа.

**Q-OPENAI-UI — точный вопрос поддержке:** разрешён ли обычный локальный non-AI driver в личной учётной записи с существующей подпиской, который только создаёт/продолжает собственные сохраняемые ChatGPT-чаты, отправляет утверждённые prompts, читает минимальные chat identity/completion metadata, а локальные результаты получает через подключённый RDC? Как это соотносится с extraction clause и запретом Computer Use управлять ChatGPT? Нужна точная поддерживаемая поверхность без security bypass, private endpoints и новых расходов. До ответа/эквивалентного первичного основания — не запускать.

**Q-OPENAI-NATIVE:** какая доступная подписочному аккаунту native surface позволяет выбрать standalone/current-chat destination, закрепить mode/model/project, получить run→chat ref, обновить literal RUN_ID и продолжить конкретный chat после coordinator feedback? Есть ли поддерживаемый внешний control interface, не Platform model API/Workspace Agents credits? Привести account/version применимость и cadence. Если его нет — это точный gap, не повод использовать приватный endpoint.

## Допустимые и недопустимые approvals

Одноразовые login, выбор текущего режима, ограниченная настройка собственных native tasks и тестового профиля возможны после P-SPIKE. Обязательные security/macOS/service prompts подтверждает владелец. Unknown billing, quota, changed policy, scope escalation, unexpected provider, concurrent writer, missing tool требуют pause; routine проверки и IPC не должны запрашивать новое разрешение каждый раз.

Workspace Agents API не включён: другой agent runtime, admin tokens, credit-based offering и расхождения API docs не подтверждают существующие included limits и нужный режим. Никакие credentials для него не создаются. Принятие этого плана не меняет запрет API fallback и Codex-primary.
