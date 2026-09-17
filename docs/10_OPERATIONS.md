# Эксплуатация и восстановление

**Редакция:** ORCH-000-R1. Ни coordinator, ни native task, ни LaunchAgent сейчас не включены. Все команды ниже — планируемый интерфейс, а не доступные сегодня бинарники.

## Режим первого запуска

Сначала foreground под контролем владельца, после ORCH-001 и offline tests. В явной конфигурации закрепляются absolute Python/Codex paths, cwd инструмента, runtime dirs, device identity, approved policy/plan, selected ChatGPT surface/model и finite budgets. Не полагаться на интерактивный shell PATH; Codex в RDC PATH не найден, но bundled binary есть.

Startup health проверяет собственный ledger/schema/backup, незавершённые intents, owned task registrations и quiescence, available disk, billing/auth evidence freshness, RDC availability, native launcher contract/version. Health не запускает модель и не изменяет основную репу. UNKNOWN активность блокирует dispatch.

Планируемые команды: `orch doctor --readonly`, `orch status`, `orch run --foreground --approved-plan <ref>`, `orch pause`, `orch reconcile --run-id <id>`, `orch stop`. Они обрабатывают typed parameters и не принимают arbitrary shell из model output. Read-only status не делает hidden sync, provider ping или auth refresh, если это не предусмотрено явным безопасным contract.

## Что должно быть доступно на Mac

Различать питание, awake state, logged-in GUI, unlocked screen, работающий RDC, официальный ChatGPT runtime и сеть. Online сейчас не доказывает ночную работу или recovery. Native cloud turn с RDC всё равно зависит от доступности Mac bridge. Local scheduled task зависит от app/machine по выбранной surface. Keychain/login могут быть отдельным blocker.

Никаких изменений FileVault, SIP, пароля, autologin или отключения lock. Документация Computer Use содержит специальный locked-use механизм, но он не разрешает нашему local driver разблокировать Mac и не устраняет запрет управлять ChatGPT. Такой security plugin не входит в проект/approval (W20/W21).

В случае user takeover/focus change остановить отправку и reconcile. UI reserve не имеет права закрывать/переключать чужие окна. Headless subprocess properties нельзя переносить на GUI route. Реальное locked/sleep испытание возможно только в согласованное окно, не во время другой активной работы.

## Квоты и паузы

Три категории ожидания: ChatGPT/runtime usage, Work/Codex shared usage где применимо, RDC transport usage. Числа не суммируются в выдуманный единый «остаток токенов». Unknown billing отдельно от quota. Never upgrade/buy/reset paid limit, never switch model/runtime/account молча. После reset проверяются новые факты, а не просто старый таймер.

Foreground coordinator опрашивает локальный state дешёвым кодом. Native wake-ups расходуют модель даже при busy/empty queue: ограничить количество, конечный COUNT/expiry, прекратить после completion. Учитывать delay hourly cadence. Не поддерживать бесконечный чат или REPL для иллюзии постоянно работающего исполнителя.

## Логи и evidence

Предлагаемые local caps: context32KiB; stdout chunks16KiB; один run log10MiB; общий runtime evidence1GiB и не более30дней нерелевантных raw logs до owner-reviewed retention. Approved task/review/commit evidence не удалять как обычный временный stdout без принятой retention policy. Эти числа — предлагаемая политика инструмента, не vendor limits.

Не логировать credentials, full environment, cookies/storage, auth.json или unrelated transcript. Предпочитать refs/digests, normalized errors и bounded excerpts. После stdout truncation сохранить признак truncation и cursor; summary не заменяет полный доступный log для расследования. Artifact manifest содержит дату и SHA256, но не выдаёт подпись worker за независимое происхождение.

## Pause, stop, cancel и resume

Pause прекращает dispatch новых работ и owned wake-ups, где доступно supported управление, сохраняя состояние активного run. Stop сначала pause, затем graceful shutdown собственных managed processes с доказанными identities; неизвестный worker остаётся blocker. Не завершать общий RDC, чужие terminals/браузеры или другие ChatGPT-чаты.

Resume/reconcile сверяет durable ledger с реальными owned ChatGPT refs/native task runs, processes, snapshots и Git. Uncertain send/commit/push сначала проверяется; повтор действия не является recovery по умолчанию. Опубликованный commit не откатывается автоматически. Owner decisions не переносить на изменившуюся policy/snapshot.

## Crash loop и startup после сна

При старте процесс не dispatch-ит до reconciliation. Несколько последовательных health failures переводят состояние в PAUSED_HEALTH и прекращают автоматические повторы. Backoff ограничен, usage модели не расходуется на local retry. macOS restart меняет boot identity; PID из прошлого boot не считается своим живым процессом.

Незапланированный sleep/network loss сохраняет точку неопределённости. После wake проверить auth/native tasks/RDC device и end status, а не resend prompt. Любое изменение client version/model availability может инвалидировать G1 и потребовать повторной bounded проверки.

## Optional user LaunchAgent

Только после принятия foreground MVP и отдельного opt-in. Apple описывает per-user LaunchAgent как процесс logged-in пользователя; это не доказательство GUI-unlocked режима (W23). Планировать явный ProgramArguments/cwd/env, ограниченный restart/backoff, standard log paths, health-before-dispatch. Не создавать системный daemon с broad root rights.

В отдельном subpatch указать точный plist path в ~/Library/LaunchAgents, команды enable/disable/uninstall, владельца файлов и отсутствие global side effects. Не устанавливать такой plist сейчас. В отчёте нельзя обещать, что программа продолжит работать после окончания ответа, пока реально не установлен и не включён соответствующий механизм.

## Обслуживание и удаление

Обновления зависимостей, Codex/client/bridge и схем — отдельные проверяемые changes, не silent auto-upgrade. Backup ledger штатным SQLite backup с manifest; recovery сначала на копии. Migration backup не содержит model credentials.

Uninstall отключает только собственные tasks/LaunchAgent, останавливает доказанно свои процессы и удаляет лишь перечисленные disposable runtime files после owner approval. Не удалять реальные ChatGPT-чаты, основной проект, shared RDC, общий Codex profile или пользовательскую библиотеку. Research docs/evidence можно оставить для аудита.

## Текущее состояние по окончании исследования

Разрешено и выполнено только discovery, relevant read-only Mac status/config/source inspection, публичное исследование и создание этого документационного пакета. Live model/spike/tasks/queue, tests/build/hooks/sync основной репы и Git mutations не запускались. Фактическую запись/прочтение пакета и финальный protected comparison см. DOC_VALIDATION.json / FINAL_READONLY_CHECK.json после их формирования.
