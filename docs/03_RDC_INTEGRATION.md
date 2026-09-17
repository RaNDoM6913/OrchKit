# Интеграция Remote Desktop Commander

**Редакция:** ORCH-000-R1. Связанные решения: [02](02_CAPABILITIES_AND_EVIDENCE.md), [05](05_ARCHITECTURE.md), [07](07_SECURITY_RECOVERY_GIT.md). RDC — bridge рабочего ChatGPT, не chat launcher.

## Устройство и текущая поверхность

Привязка: c87394ca-e556-4f14-85be-3269a33f0040 / MacBook-Air-Ivan.local / v0.2.50. Каждое обращение указывает deviceId. Несовпадение identity, offline или выбор нового Mac не исправляются выбором «первого online» устройства. Если устройство переименовано, доверие не выводится только из имени: сверяются утверждённая запись и повторный bootstrap.

| Задача | Реальный обнаруженный tool | Что доказано / что проектируется |
|---|---|---|
| Устройства | list_devices | Текущее устройство online |
| План/остаток | who_am_i | Процент remote calls; не invoice/точный reset |
| Конфигурация | get_config | Read/write limits, allowedDirectories, telemetry |
| Чтение | read_file, read_multiple_files, get_file_info, list_directory | Работают на разрешённых абсолютных Mac paths |
| Запись | write_file, create_directory | Только docs сейчас; writer fixture позже |
| Команда | start_process(command,timeout_ms,shell,deviceId) | Безопасные metadata/status subprocess завершились |
| Результат команды | read_process_output(pid,offset,length,timeout_ms,deviceId) | Один собственный process подтверждён completed/exit0 |
| Долгое взаимодействие | interact_with_process и связанные process tools | Не используются как бесконечный production REPL |
| UI / новый ChatGPT | В обнаруженной schema таких tools нет | Shell и MCP сами это не предоставляют |

Общий RDC обслуживает другой чат. Не менять его config, selected global device, транспорт или приложение; не вызывать shutdown/restart, не завершать чужие процессы, не читать общий recent tool history. Все наши process refs содержат run_id и доказательство принадлежности.

## Права и ограничения

allowedDirectories=[] означает отсутствие узкой файловой allowlist в текущей конфигурации. Репозиторий поставщика отдельно предупреждает: file restrictions не распространяются на terminal commands (W15). app permission Allow all actions относится к ChatGPT plugin layer и не отменяет системные/сервисные подтверждения. Ни одно из этих наблюдений не доказывает новый scheduled runtime.

Prompt с allowed_paths, worktree и post-diff validation — организационная защита. Worker с прямым broad shell может обойти helper и добраться до control-plane/credentials того же пользователя. «RDC sandbox» в документах инструмента не заявляется. Не сужать общий RDC ради эксперимента: отдельная enforceable bridge/OS identity может изучаться позже при отдельном approval и без обязательного нового тарифа.

## Предлагаемый минимальный helper

Будущий `orch` — локальный клиент coordinator, а не shell evaluator. Концептуальные операции: doctor --readonly, task claim, context show, result submit, review show, status, process start/status/output, quiesce. Их сейчас нет; пример в брифе не доказывает установленный CLI.

| Операция | Вход | Ответ / смысл |
|---|---|---|
| claim | run_id, task_id, attempt, policy/plan revision, bootstrap nonce | Lease epoch только на READY с совпадающими revisions; duplicate claim возвращает ту же запись |
| context | run_id + scoped capability | Bounded JSON pack и hashes; не полная история |
| process start | registered check_id + typed params | Managed process_ref; argv из reviewed registry, не из Markdown |
| process status/output | process_ref + offset + max_bytes | running/completed/cancelled/unknown; stdout/stderr refs, final exit code |
| submit | run_id + receipt path/bytes + lease_epoch | Accepted claim, не PASS; immutable receipt digest |
| review show | run_id + snapshot_id + after_sequence | Только соответствующий feedback; stale report отвергается |
| quiesce | run_id + owned process refs | Закрыть helper write capability; ждать подтверждённые процессы; вернуть barrier state |

Transport — Unix-domain socket с явным protocol/schema version, request_id/idempotency key, maximum request bytes и peer identity. Для spike допустим один foreground process и атомарный append-only journal. Для production SQLite изменяет только coordinator; worker не получает «отредактируй status.json для DONE». Однако same-user filesystem permission не защищает от broad shell; это отдельный unresolved security gate.

Пути нормализуются относительно заданного workspace, запрещаются absolute client paths там, где нужен relative path, .., symlink escape, неожиданные hardlinks и rename-race. Открытие через безопасные descriptor-relative операции предпочтительнее check-then-open; файловый manifest хранит type/mode/content hash. Размер request ≤64 KiB, context ≤32 KiB, output chunk ≤16 KiB — предлагаемые лимиты инструмента, не факты RDC.

## Команды, timeout и quiescence

start_process возвращает PID и первоначальный вывод, иногда «process running» даже после последних печатных строк. Завершение подтверждается read_process_output/managed process supervisor, а не тишиной. В исследовании последующее чтение process75670 дало completed exit0. Не переносим этот единичный тест на outage/reconnect или detached descendants.

Production process identity: run_id, process_uuid, PID, start time, executable identity, cwd, parent/process-group/job handle, boot/session marker, lease epoch. PID без start identity может быть повторно использован. Supervisor запускает один раз, хранит stdout/stderr в отдельных ограниченных файлах и exit status отдельно; polling обычным кодом, не моделью. Не завершать процесс только по совпадению PID из старого ledger.

После RESULT_SUBMITTED worker прекращает выдавать новые write requests и переходит в QUIESCING. Coordinator ждёт все собственные descendants и launcher completion evidence, закрывает helper lease, формирует snapshot. При прямых RDC-командах, не учтённых supervisor, barrier может остаться UNKNOWN; тогда next writer запрещён. Таймер не заменяет доказательство остановки. Snapshot-copy сохраняет данные для reviewer, но не доказывает отсутствие дальнейших записей в исходный workspace.

## Квота, размер и поведение транспорта

Текущие настройки: readLineLimit1000, writeLineLimit50; для write_file использовать небольшие chunks согласно tool guidance. Большие logs остаются на Mac, в чат возвращаются bounded excerpts и cursor. Публичный Free tier заявляет 10 000 calls/month, но фактический account tier/reset не установлен. Не обещать безлимит. Записывать usage_before/after с доступной точностью; если есть только проценты — не превращать их в точные calls.

В ORCH-001 проверить: размер команды/аргументов; Unicode; pagination/end-of-stream; deadline без убийства; reconnect Mac/network; device mismatch; concurrent other-chat activity без вмешательства; repeated approval; locked screen. Любое исчерпание RDC отдельно от model quota ставит bridge pause. Backoff и bounded retry не должны создавать новый model turn на каждом poll.

## Данные у поставщика

Поток предположительно включает cloud client → Remote MCP relay → локальное приложение → ответы; точная transport/privacy гарантия требует W16. Поставщику задать Q-RDC: какие request/response payloads и telemetry сохраняются, где/сколько, кто subprocessors, как удалить, есть ли E2EE, условия free remote usage/reset, распространяется ли allowedDirectories на terminal, поддерживается ли отдельная restricted connection без влияния на общую.

В логах инструмента не хранить tokens, полные environment/process argv, весь stdout без redaction, чужие conversations. URL или metadata без чувствительного payload достаточно для связи evidence. Пока contractual details неполны, spike использует только синтетический локальный artifact без продуктовых/аккаунтных данных.

## Бесплатная альтернатива при ограничении RDC

Сначала pause до восстановления включённой квоты. Open-source local stdio MCP сам по себе недоступен облачному ChatGPT. Нативный local Work file/terminal доступ можно оценить только при сохранении принятого настоящего ChatGPT-режима, действующих прав и нулевой доплаты; это отдельная проверка bridge parity, не автоматическая подмена RDC или переход на Codex. Новый tunnel/MCP/server не входят в обязательный MVP.
