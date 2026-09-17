# Архитектура и протоколы

**Редакция:** ORCH-000-R1. Выбран минимальный local coordinator; implementation отсутствует. Feasibility launcher — обязательный внешний gate, а не предполагаемая функция helper.

## Состав и выбор стека

| Компонент | Ответственность | Не имеет права |
|---|---|---|
| Plan Adapter | Нормализовать owner-approved tasks/DAG/source refs | Исполнять инструкции из Markdown как shell |
| Durable Ledger | Runs/events/intents/leases/checks/reviews/approvals | Делать worker self-report доказательством |
| Coordinator | State transitions, budgets, очередь, reconciliation | Самостоятельно менять разрешённый план |
| ChatGPT Launcher | Доказанное native create/send/resume/observe | Выдавать Codex thread за ChatGPT |
| Context Builder | Bounded актуальные правила/цели/files/evidence | Экспортировать всю чужую историю |
| RDC / Helper | Typed bridge на выбранный Mac | Обходить approval или выдавать hard sandbox без boundary |
| Verifier | Scope, actual checks, snapshot fingerprints | Доверять фразе worker «tests passed» |
| Codex Adapter | Подписочный review immutable input | Писать продукт/публиковать/быть primary |
| Approval Boundary | Owner decisions bound to revisions/result | Принимать подпись самого worker за owner |
| Publisher | Exact verified bytes → commit/push/remote check | Массовый add, force/reset чужой работы |
| Status CLI | Показывать trace, wait reason, pause/reconcile | Создавать фиктивную историю ChatGPT |

Python3.9 stdlib (argparse/json/sqlite3/subprocess/socket/hashlib) уже доступен и достаточен core. Нет SDK/dependency requirement. TypeScript/Node24 допустим только для отдельно разрешённого UI adapter, если доказана необходимость. Native config без coordinator не даёт durable queue/Git verification; универсальный workflow framework не даёт launcher. Codex SDK/app-server усложняют первый reviewer по сравнению с существующим exec; app-server рассмотреть только для официальной auth/rate telemetry без shared daemon (W06/W07/W11/W12).

## Хранение

SQLite на локальном диске выбран для production: транзакции состояния, uniqueness и crash reconciliation между task/run/lease/approval. Atomic JSON проще для ORCH-001; при нескольких взаимосвязанных сущностях один набор файлов требует самодельного transaction journal. Production сочетает SQLite ledger и content-addressed файлы артефактов; большие logs не хранятся в rows.

Таблицы дизайна: plans, policies, tasks, dependency_edges, runs, launch_intents, chat_bindings, leases, processes, snapshots, checks, reviews, feedback_deliveries, approvals, publication_intents, publications, events. Foreign keys, monotonic event sequence, unique(task_id,attempt), unique(send_intent_id), одна active writer lease на repo identity. `BEGIN IMMEDIATE` для claim; timeout/clock не удаляют live lease. schema migration требует backup и отдельного разрешённого patch.

Артефакты: approved immutable source; contexts/<digest>.json; runs/<run_id>/receipts; snapshots/<digest>/manifest; logs/<process_ref>; reviews/<id>.json. Сначала fsync content/temp→atomic rename, затем транзакционный reference. После crash orphaned objects допустимы, dangling successful rows — нет. SQLite/WAL backup делается штатным backup API, не копией живого файла. Cloud-synced folder не выбирается для runtime ledger.

## Схемы

Task: schema_version, id, goal, non_goals, source_refs+hashes, approved_plan_revision, policy_revision, dependencies, allowed/protected paths, registered checks+timeouts, required_review, owner_acceptance, publication_policy, retry/budget limits, expected_base.

Run: task_id/run_id/attempt; launcher_kind; observed_chat_ref и evidence_ref; observed account/workspace/project/mode/model; device_identity; auth/billing evidence refs без секретов; lease_epoch и process refs; base/snapshot; receipt/check/review/approval/publication refs; timestamps; wait_reason; resume cursor; intent keys. Unknown сохраняется null/UNKNOWN, не заполняется предположением.

Review: reviewer_runtime/version/auth_mode/model; task/run/snapshot digests; actual verdict; findings[{id,severity,path,line_range,requirement,evidence,impact,suggested_check}]; uncertainty; stdout event refs; exit code; began/ended times. Finding — проверяемое замечание, не приказ изменить policy. Report validity отдельно от validity выводов.

Approval: owner_identity, operation class, task/run/plan/policy/snapshot digest, allowed paths/check basis/publication destination, expiration/revocation/event ref. Нет универсального «approve everything» из worker prompt. Approval task class может быть заранее принят, но visual/behavior acceptance результата остаётся отдельным gate по contract.

## Независимые машины состояния

Task: PROPOSED → APPROVED → READY → IN_PROGRESS → DONE; отдельно ACCEPTED/BLOCKED/SUPERSEDED. Run: CHAT_STARTING → CHAT_BOUND → RUNNING → RESULT_SUBMITTED → QUIESCING → VERIFYING → REVIEWING → NEEDS_FIX / WAITING_OWNER / READY_TO_PUBLISH → COMPLETE. NEEDS_FIX возвращает тот же chat/run в новый bounded attempt phase с новым snapshot; новый task получает новый chat.

Chat: NOT_CREATED / SEND_INTENT / SEND_UNKNOWN / BOUND / ACTIVE / WAITING_TRIGGER / FINISHED / CANCEL_UNKNOWN. Publication: not_requested / intent_recorded / committed / pushed / remote_verified / conflict. DONE не равно ACCEPTED; exit0 model turn не равно run COMPLETE; локальный commit не равен remote_verified.

Wait reasons: quota_chatgpt, quota_work_codex, quota_rdc, auth, permissions, billing, missing_tool, unknown_send, bridge_offline, active_worker, conflict, service_permission, unknown_chat_identity. Cause хранится отдельно от состояния; автоматическое «перепрыгивание» причины запрещено.

## Нативный message flow — условный к gates

1. Owner-approved plan → coordinator фиксирует task/run/intent и literal RUN_ID. Нет model poll, пока task не READY.
2. **Launcher** через доказанный native task destination отправляет prompt в настоящий ChatGPT. Это единственная граница, которая инициирует turn; IPC/helper её не заменяет.
3. Launcher observation связывает persisted message / ChatGPT ref / mode selection. Рабочий ChatGPT через RDC получает nonce/context и claim. Только после двусторонней привязки даётся write lease.
4. ChatGPT работает на allowlist, процессы регистрируются. Submit добавляет worker receipt, не меняет результат на PASS.
5. QUIESCING: закрываются новые helper writes, подтверждаются terminal descendants и состояние чата. Verifier строит snapshot и сам выполняет approved checks.
6. При review_policy trigger coordinator запускает isolated Codex reviewer, проверяет IDs/schema/exit, сохраняет actual feedback. На каждый timer tick Codex не нужен.
7. **Native in-chat continuation**, заранее корректно связанное с этим ref, возобновляет ChatGPT после окончания предыдущего turn. Оно читает feedback mailbox через RDC. Запись файла coordinator сама turn не создаёт.
8. ChatGPT исправляет; новый snapshot → fresh verification → required re-review/owner gate. После завершения/публикации coordinator допускает следующий task и launcher создаёт другой настоящий ChatGPT.

Шаги 2/3/7/8 пока не подтверждены вместе. Static recurring dispatcher может читать следующий READY task, но не гарантирует literal unique RUN_ID в initial prompt, программный task update, наблюдаемый chat ref, отсутствие idle chat и targeted resume. Эти свойства нельзя скрыть в реализации. В ORCH-001 они проверяются раньше production ledger. Не заменять их `sleep(N)` или «ChatGPT продолжит себя».

## Native timing и полезные ограничения

Timer — только wake-up. Claim проверяет dependency closure, lease, quota и разрешённый диапазон попыток. Busy/empty wake-up не получает права писать; он учитывается как расход и ограничивается отдельным budget. Повторяющаяся задача без конечного COUNT/expiry способна создавать лишние чаты: для spike это запрещено.

Гипотеза A_NATIVE для recurring production требует механизма публикации следующего prompt envelope и контроля owned tasks без участия owner на каждом блоке. Если current native controls дают только ручное редактирование, этот вариант supervised и не проходит целевую приёмку. Event triggers Gmail/Slack/GitHub не использовать как фиктивный local-file webhook и не генерировать сторонние сообщения ради таймера.

## UI reserve — только дизайн

После отдельного service permission B_UI мог бы использовать отдельный browser profile с ручным login, один собственный page/window, semantic unique locators, account/project/mode/model guard, observable URL and sent message marker. Не читать чужие tabs/transcripts или network/private endpoints; не копировать auth storage. Composer filled ≠ sent; timeout после click = SEND_UNKNOWN; перед retry reconcile.

Native desktop shortcuts и copy deep link полезны для manual setup, но не являются send/resume API. Official Computer Use исключён для ChatGPT. Если UI terms gate закрыт, этот adapter не строится и не используется как workaround. User takeover, unexpected focus/dialog, CAPTCHA/2FA/approval = immediate pause, не новые горячие клавиши.

## Review policy и context

Codex обязателен для сложного плана, продуктового diff, изменения tests/check scripts, security/authority boundaries, повторного verifier failure. Тривиальные документационные изменения допустимы с обычными локальными проверками, если contract не требует review. Максимум два correction cycles, затем owner decision. Required review при quota не пропускается.

Context pack по умолчанию ≤32 KiB, один крупный блок на чат, новый context на task boundary. Он содержит действующие rules/task/base/files, решения, checks, last feedback и следующий шаг. Логи доступны по refs. Byte budget — наш консервативный cap, не точный token context модели. Не использовать 400k/500k из истории как реальную capacity. Project memory может переносить контекст между чатами: проверить, не менять без approval.

## PROJECT_PLAN и immutable source backlog

В <separately-authorized-integration> source backlog импортируется read-only с digest; tasks map хранится отдельно. На основной репе PROJECT_PLAN остаётся owner-visible execution source of truth, ledger — operational evidence. Нельзя иметь две независимо редактируемые версии статуса.

Sync protocol: сохранить plan digest/base → создать pending transition в ledger → подготовить exact patch PROJECT_PLAN с operation_id и нужным evidence, не трогая исторические записи → independent review/checks → publication snapshot включает plan patch → после content/commit reconciliation пометить transition applied. Dispatch следующего MW task только после согласованного plan+ledger. Crash посередине = pending reconciliation, не автоматическое повторное DONE. Любой concurrent plan change = conflict/re-read, не overwrite.

Текущие MW-002/003/009 blockers и owner gates не исчезают из-за успешного ORCH MVP. Встраивание orchestration в repository требует отдельного automation contract и окончания конкурирующей записи.
