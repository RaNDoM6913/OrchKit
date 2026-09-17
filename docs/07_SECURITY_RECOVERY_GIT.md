# Безопасность, quiescence, Git и восстановление

**Редакция:** ORCH-000-R1. Это требования будущего инструмента. Основная репа в исследовании не изменяется и не используется для fault injection.

## Модель угроз и границы полномочий

Недоверенные источники: prompts внутри файлов, импортированный backlog, web content, stdout тестов, model receipts, reviewer recommendations. Ни один из них не расширяет policy или approval. Входные данные валидируются по schema/path/size/revision; не исполнять автоматически строку shell из плана/результата. Secret-like данные редактируются до logs, но redaction не заменяет минимизацию доступа.

| Область | Намеренное разделение | Предел текущего RDC |
|---|---|---|
| Worker | Только task workspace + typed helper | Broad shell того же пользователя может обойти policy |
| Verifier | Независимый процесс, реальный approved check registry | Тесты могут исполнять недоверенный код; нужны sandbox/env/egress constraints |
| Reviewer | Read-only frozen export; stdout report | Host hooks живут вне обычного prompt; isolated config обязателен |
| Ledger/approvals | Только coordinator/owner writes | chmod под тем же UID не даёт защиты от worker |
| Publisher | Отдельная capability и credentials | Credentials того же UID не защищены broad shell |
| Чужой проект/процессы | Никаких writes/kill/reconfigure | Prompt alone не OS confinement |

Для hard enforcement нужен отдельный OS principal/изолированная среда worker и bridge, недоступные control-plane state и publisher credentials, capability-gated filesystem/process access. Worktree не изолирует .git common refs/config/hooks. Эти изменения нельзя внедрять в общий RDC в исследовании. Без них возможен только честно обозначенный cooperative disposable режим, а не гарантия adversarial isolation.

## Baseline перед любой будущей записью

Сохранить repo realpath/root, device, branch, HEAD, intended remote identity/ref, tracked mode/blob map, staged bytes, dirty/untracked paths и fingerprints protected files. Использовать Git read с GIT_OPTIONAL_LOCKS=0, no pager, --no-ext-diff/--no-textconv для diff. Не применять raw .git/index SHA: index metadata может меняться без content change.

Если обнаружены чужие staged изменения, безопасный default — pause publication до отдельного разрешения/изоляции; не unstaging за владельца. Чужие dirty paths запрещены к редактированию/включению. Untracked ZIP/handoff не мусор. Не применять reset, checkout/restore, clean, stash, update-index, force-push или массовый add. Scope определяется approved task, а не текущим git status целиком.

Main baseline исследования: HEAD a30a2592885915b6d289606b4738f178fac94d4d; handoff modified; planning ZIP untracked. Нет права коммита даже если появились наши документы в другом каталоге. Remote GitHub не проверялся; local origin/main — только локальное наблюдение.

## Lease и прекращение записи

Lease key = repository identity, owner=run_id+attempt, epoch, creation metadata, managed process set. Atomic claim допускает одного writer. Heartbeat нужен для liveness hints, но expiration не доказывает смерть. PID проверяется вместе с birth identity/boot marker; stale PID нельзя kill.

Протокол: RUNNING → RESULT_SUBMITTED → QUIESCING → helper write capability closed → подтверждённый конец всех managed descendants → launcher turn termination evidence → frozen snapshot → VERIFYING. Submit idempotent, но не освобождает lease. Недоступный launcher или untracked direct RDC process оставляет barrier UNKNOWN.

Helper fencing препятствует новым helper writes старого epoch; прямую RDC shell-команду он не останавливает. Atomic snapshot защищает прочитанные reviewer bytes, но не доказывает отсутствие последующих изменений рабочей копии. Перед выдачей следующей write lease нужно техническое revocation или достаточное доказательство остановки в явно принятой cooperative модели; тишина N секунд недостаточна.

Cancel действует только на managed process group с доказанной принадлежностью, затем собирает exit/descendant status. Не kill общий RDC или пользовательские terminals. Пауза прекращает dispatch; она не объявляет, что уже запущенный worker безопасно завершён. Неподтверждённый cancel = CANCEL_UNKNOWN и writer remains blocked.

## Verifier и защита проверяющей базы

Verifier сам запускает зарегистрированные commands в correct cwd, сохраняет действительный exit, stdout/stderr refs и snapshot_id. Проверить resolved import path и module origin: тест копии формулы не доказывает actual exported domain function. Эта опасность есть в историческом MW-001 inventory, поэтому negative test обязательный.

Scope diff, file type/symlink/hardlink, protected content, whitespace, secret scan, required test outputs и review completeness проверяются отдельно. Изменение tests/scripts/config, используемых для проверки, требует review самой проверяющей базы. Самоподписанный receipt не даёт независимости, если key доступен worker.

Нельзя запускать непроверенный npm lifecycle/hook автоматически под видом «только tests». Check registry содержит exact argv/env/network policy и approved check-basis digest. Verifier/test process не получает publisher auth или model credentials. Недоверенный network egress может вызвать побочные действия даже при readonly filesystem — sandbox включает этот риск или gate остаётся открытым.

## Publisher — отдельная транзакция

До публикации нужны: quiescence, свежий base/branch/remote, verified snapshot, required review по этому snapshot, owner gate по этому результату, exact staged allowlist и staged bytes равные проверенным. Staged чужие bytes недопустимы. Проверить исполняемые Git hooks и их external AI/network side effects; не выключать repo policy молча, а стоп/отдельное согласование.

Publication intent фиксирует expected parent, exact tree, message, allowed ref, operation_id. Один ожидаемый commit, обычный non-force push в подтверждённый remote/ref. После push — проверить actual remote ref штатным чтением; успешная строка stdout или local origin недостаточны. В MVP remote — disposable local bare, не GitHub основной репы.

Если после check base изменился, не cherry-pick/rebase автоматически поверх чужой работы: conflict и новая проверка. После изменения diff старые review/approval устаревают. Remote change после commit, до push — pause, не force. Опубликованный commit не откатывается автоматически.

## Reconciliation после неопределённого исхода

| Точка сбоя | Что сверить | Что запрещено |
|---|---|---|
| До create/send | launch_intent, native task definition, message digest | Создать новый intent без проверки старого |
| После send без ответа | Owned run/chat identity + actual sent marker | Повторно нажать Send вслепую |
| Claim timeout | Coordinator unique key + lease epoch | Выдать второй lease |
| После write до receipt | Actual bytes/scope/process state | Принять RUNNING как успех |
| Submit timeout | receipt digest/idempotency record | Начать новый writer |
| Во время quiesce | Managed descendants, launcher state, direct-write ambiguity | Освободить lease по timer |
| Review crash | Codex process+auth+snapshot/report completion | Подделать PASS или пропустить mandatory review |
| Feedback send unknown | Bound chat, envelope ID, delivered ack | Дублировать fix task без reconciliation |
| Commit timeout | Actual HEAD/parent/tree/message vs intent | Выполнить commit всего повторно |
| Push timeout | Actual remote ref and exact commit | Force-push или считать network timeout failure без проверки |
| Plan sync crash | Pending operation_id, plan digest, commit/ledger status | Два независимых статуса DONE |
| Disk full/corrupt ledger | Last committed transaction, artifact digests, backup | Молча обнулить очередь |

UI exactly-once не обещается. Для внешних действий: persist intent before side effect, stable operation key, observe, dedupe, bounded retry только при доказанном отсутствующем результате. Непроверяемый исход — пауза с конкретными evidence, а не бесконечное восстановление моделью.

## Owner takeover и пересмотр policy

Owner takeover немедленно останавливает dispatch; текущий writer выводится из активной работы только через подтверждённый stop/barrier. Владелец может изменить план, но новая revision делает старые approvals/contexts invalid. Approved queue — не право агента менять собственные protections или нажимать confirmations.

Для resumed task нужно восстановить только свой bounded context, не читать посторонние conversations. Исторические snapshots/receipts сохраняются как evidence с датой; runtime state перечитывается. Recovery не сбрасывает quotas и не меняет модель для обхода лимитов.
