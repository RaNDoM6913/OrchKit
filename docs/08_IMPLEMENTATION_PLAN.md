# Последовательный план разработки

**Редакция:** ORCH-000-R1. Ни один implementation patch не начат. ORCH-000 — данный исследовательский пакет, не скрытый scaffold. После отдельного принятия P-SPIKE routine шаги исполняются последовательно без ручного «Продолжай», но failure gates не обходятся.

## Правила выполнения

Один активный patch и один writer. Каждый patch имеет bounded task brief, actual base, checks, evidence, review policy, результат и checkpoint. Реализация ORCH-002–007 начинается только после полного принятого ORCH-001 E2E; fake launcher не позволяет обойти gate. Git инструмента разрешается только будущим принятым этапом; remote основной репы не заимствуется.

### ORCH-000 — research baseline

- **Цель:** проверить требования и доказательства, выбрать route/gates, подготовить план.
- **Scope:** docs/00–11, evidence manifest и validation; read-only project/tools/status/public docs.
- **Non-goals:** код инструмента, тестовый model run, tasks, Git changes, installations.
- **Dependencies / permissions:** текущий пользовательский RESEARCH_AND_PLAN и разрешённые чтения/новые docs.
- **Checks:** все обязательные документы/ссылки/schemas, no claims of live E2E, фактический Mac readback и protected baseline comparison.
- **DoD / output:** сохранённый согласованный пакет; blockers и P-SPIKE явно названы; runtime feasibility не помечена PASS.
- **Stop:** запись в чужой существующий файл, неразрешённый путь, tool safety block; сохранять остальные документы без обхода.

### ORCH-001 — настоящий two-chat spike

- **Цель:** доказать native ChatGPT → RDC → verifier → subscription Codex → same-chat feedback → ChatGPT-B.
- **Scope:** disposable spike, минимальный foreground supervisor/helper/journal, синтетические fixtures, isolated reviewer config, только свои finite native tasks.
- **Non-goals:** production ledger, main repo, publisher, UI bypass, Workspace Agents API, сервисы/LaunchAgent.
- **Dependencies / permissions:** ORCH-000 owner acceptance + P-SPIKE; G0 billing/security перед моделями, G1 launcher contract перед scaffold.
- **Checks:** G0–G9 в [06](06_FEASIBILITY_SPIKE.md), фактические chat refs/IDs и bytes, manual-relay=0, own task cleanup.
- **DoD / output:** evidence index двух настоящих ChatGPT, actual review, automatic delivery/finalization, новый bootstrap; отдельно классификация реального bug repair.
- **Stop:** любой обязательный gate UNKNOWN/FAIL; сохранить точную surface/operation/response, не строить следующий patch как замену.

### ORCH-002 — task/run schemas и durable ledger

- **Цель:** устойчивые входы, DAG, source/approved revision и транзакционные состояния.
- **Scope:** Python stdlib CLI, SQLite schema/migrations, immutable artifact store, atomic intents/claim, context builder ≤32 KiB, offline/fake tests.
- **Non-goals:** новая модель на timer, product integration, универсальный workflow framework.
- **Dependencies / permissions:** ORCH-001 PASS и принятое продолжение; отдельный инструмент, его собственная Git baseline без network remote по умолчанию.
- **Checks:** U01–U08; duplicate IDs/cycles, missing dependency, malicious paths, invalid schemas, stale approvals, crash before/after commit, context corruption.
- **DoD / output:** typed contract/schema docs и минимальные исполняемые core tests; source backlog immutable; fake adapter явно помечен FAKE.
- **Stop:** migration loss, bypass owner policy, отсутствие действующего G1 contract после обновления клиента.

### ORCH-003 — выбранный native launcher adapter

- **Цель:** productize именно подтверждённые create/send/resume/observe операции.
- **Scope:** native binding, owned task lifecycle, exact RUN_ID prompt, account/mode guard, chat identity observation, send intents/reconciliation, next-block gating.
- **Non-goals:** приватные endpoints, auth extraction, direct model API, Computer Use к ChatGPT, coordinate/sleep automation.
- **Dependencies / permissions:** ORCH-001 и ORCH-002; только finite approved native dispatch. При изменении surface — повторный gate, не новая догадка.
- **Checks:** F01–F04; N01–N09; missing RDC/runtime/model, send ambiguity и changed destination fail closed.
- **DoD / output:** контракт Launcher реализован только для доказанного route; persisted message и ref наблюдаемы, resume возвращается в тот же chat.
- **Stop:** current native contract недоступен, routine manual relay, quota/billing unknown, nondeterministic identity. UI reserve не включается сам.

### ORCH-004 — helper, процессы, quiescence, verifier

- **Цель:** bounded RDC обмен и независимые реальные проверки.
- **Scope:** Unix IPC, registered argv/checks, process supervisor/identity/output cursors, receipt contracts, quiesce/fencing, frozen manifests, actual-module verifier.
- **Non-goals:** широкие terminal restrictions под видом prompt policy, смена общего RDC, OS security bypass.
- **Dependencies / permissions:** ORCH-002/003, disposable-only write/check rights; отдельное решение по enforced vs cooperative profile.
- **Checks:** U09–U14, F05–F08, N10–N20; detached descendant, pid reuse, truncated output, stale lease, symlink race, false PASS, mutated checks.
- **DoD / output:** повторяемая trace от ChatGPT/RDC к frozen snapshot/check exit; неизвестная активность не освобождает writer.
- **Stop:** inability to establish quiescence, direct-RDC bypass invalidates обещанную защиту, credential/egress exposure.

### ORCH-005 — reviewer, repairs, approvals и publisher

- **Цель:** независимый periodic Codex review и публикация проверенных bytes.
- **Scope:** isolated exec adapter, billing guards, review policy/schema, feedback mailbox, same-chat repair ≤2, owner approvals, exact allowlist commit/push в disposable bare remote.
- **Non-goals:** Codex edits, broad add, shared credentials to tests, publishing main repo, forced updates.
- **Dependencies / permissions:** ORCH-003/004; included Codex usage; approved local Git fixture publication only.
- **Checks:** U15–U18, F09–F12, N21–N28; external hook, purchased-credit overflow, stale review/approval, mismatching staged bytes, uncertain commit/push.
- **DoD / output:** реальное review связано с snapshot, repair выполняет ChatGPT, actual remote ref подтверждает expected commit.
- **Stop:** review quota, inherited hook, changed base/remote, no owner acceptance, invalid actual report — обязательный review не пропускается.

### ORCH-006 — recovery и три настоящих чата

- **Цель:** доказать целевой MVP, а не только happy path.
- **Scope:** три зависимые задачи в отдельной Git fixture с local bare remote; controlled restart/fault injection только собственных процессов; actual ChatGPT chats + review/feedback/publish.
- **Non-goals:** outage общего RDC, реальные торговые данные, настоящий GitHub push основной репы, удаление user chats.
- **Dependencies / permissions:** ORCH-002–005; конечный approved task bundle и synthetic owner acceptance pause.
- **Checks:** E01–E03 и весь применимый negative набор [09](09_ACCEPTANCE_TESTS.md); exact same baseline, не старые tests.
- **DoD / output:** три разных saved ChatGPT refs, три intended commits/remote refs, zero routine manual relay, один реальный review+automatic repair/finalization, protected dirty+ZIP неизменны, restart no duplicate writer/send/publish.
- **Stop:** supervised-only path, lost identity or evidence, unknown cancellation, copied-module checks, stale PASS, any billable fallback.

### ORCH-007 — эксплуатация и status CLI

- **Цель:** понятные запуск/пауза/reconcile/stop и безопасное обслуживание.
- **Scope:** foreground commands, health/status, budget accounting, log rotation/redaction, disk caps, sleep/reconnect behaviour, backup/recovery/uninstall docs.
- **Non-goals:** автоматическое включение LaunchAgent, изменение FileVault/SIP/lock, незаметные обновления зависимостей/моделей.
- **Dependencies / permissions:** ORCH-006 PASS; autostart отдельный opt-in после owner acceptance.
- **Checks:** O01–O08: explicit PATH/cwd, crash-loop, disk full, readonly status, owner takeover, idle quota, pause/stop, artifact retention.
- **DoD / output:** runnable foreground MVP и runbook; нет фонового процесса без explicit enable. Optional user LaunchAgent — отдельный subpatch/approval.
- **Stop:** навязанный unlock/security downgrade, uncontrollable worker, повторяющиеся пустые model runs.

### <separately-authorized-integration> — подключение <protected-project>

- **Цель:** применить уже доказанный инструмент к основной репе без потери чужой работы.
- **Scope:** отдельный approved automation contract, fresh read-only inventory, source mapping, owner-visible PROJECT_PLAN sync, protected paths, allowed task classes/check/publication policy.
- **Non-goals:** blanket approval всех 38 MW-задач, изменение prop/live-account rules, авто-принятие UI, disabled DeepSeek общей сессии без разрешения.
- **Dependencies / permissions:** ORCH-006/007 приняты; конкурирующая работа завершена; явное отдельное разрешение main write/publication; isolation/credential boundary решена.
- **Checks:** fresh Git/content baseline, complete current task docs, actual DAG/blockers, inherited lifecycle/verify hooks, scope+owner acceptance, remote identity verification по permission.
- **DoD / output:** принят repository-specific contract; первая небольшая разрешённая задача проходит полный trace без touched foreign files.
- **Stop:** active other writer, MW blocker, unavailable original spec, credentials/hooks risk, plan conflict, owner acceptance missing.

## Порядок и защита от разрастания

ORCH-000 → ORCH-001 → ORCH-002 → ORCH-003 → ORCH-004 → ORCH-005 → ORCH-006 → ORCH-007. <separately-authorized-integration> только отдельной командой. Реализацию UI reserve или замену bridge нельзя незаметно спрятать в ORCH-003/004.

Для каждого patch context pack содержит только актуальный scope, evidence предыдущего и expected base. Candidate implementation choices пересматриваются на version drift, но неизменные R01/R05/R09 не ослабляются. Не обещать фиксированный срок/объём модели: бюджеты ограничивают выполнение, не создают обязанность пропускать проверки.
