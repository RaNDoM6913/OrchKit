# Приёмка, offline tests и реальные негативные сценарии

**Редакция:** ORCH-000-R1. Всё ниже — план. Ни один model/E2E/product test в текущем RESEARCH_AND_PLAN не запускался. Проверка самого набора Markdown не является тестом инструмента.

## Уровни доказательства

Unit проверяет чистые алгоритмы. Fake adapter проверяет state machine и crash recovery. Real integration подтверждает доступный native ChatGPT/RDC/Codex contract. Real E2E проверяет целевой процесс с saved conversations. PASS fake не переносится на real; старый exit0 не применяется к новому snapshot.

## Offline unit/fake contracts

| ID | Проверка | Ожидаемое доказательство |
|---|---|---|
| U01 | Task/run schema, unknown/extra fields, bound revisions | Валидатор отвергает malformed input |
| U02 | DAG cycle/missing dependency/status projection | Невозможный READY не создаётся |
| U03 | Literal run identity/attempt uniqueness | Duplicate не получает новый execution |
| U04 | Atomic claim и competing coordinators | Ровно одна writer lease |
| U05 | Source-backlog immutability | До/после одинаковые hashes |
| U06 | Plan→ledger pending transition | Crash не создаёт два независимых DONE |
| U07 | Context bounds/corrupt digest/project memory flag | Context слишком большой/битый не отправляется |
| U08 | Approval revision/snapshot binding | Stale/revoked approval не применяется |
| U09 | Typed argv registry / no shell from Markdown | Injection input не становится subprocess |
| U10 | Path/symlink/hardlink/traversal/rename race | Outside-scope write не разрешён helper |
| U11 | Process identity/PID reuse/descendant tracking | Чужой процесс не остановлен |
| U12 | Output pagination/truncation/final exit | Missing output не означает success |
| U13 | Receipt != finished, active children | Lease не отпущен преждевременно |
| U14 | Actual module import/check basis | Тест дубля не считается тестом продукта |
| U15 | Reviewer JSON/verdict/findings/snapshot | Wrong snapshot и invented PASS rejected |
| U16 | Included-only/external-hook guard | Модель не запускается при unknown billing |
| U17 | Publisher exact staged bytes/parent/tree | Foreign content не включён |
| U18 | Commit/push reconciliation | Не создаётся повторный commit/publish |

F01–F04: fake launcher create/send/resume/observe; F05–F08: fake RDC/process/offline/truncated output; F09–F12: fake reviewer/quota/approvals/publisher uncertainty. Fake outputs маркируются FAKE в ledger и не могут удовлетворить real evidence gate.

## Real ORCH-001

G0–G9 из [06](06_FEASIBILITY_SPIKE.md) обязательны. На каждом gate сохраняется actual observation, source/runtime/version, exact IDs и ограничение. Нельзя засчитать bootstrap из этого исследовательского чата вместо будущего ChatGPT-A или local Codex thread вместо ChatGPT-B.

## Три зависимые задачи MVP

| ID | Содержательный worker результат | Зависимость / публикация |
|---|---|---|
| E01 / FIXTURE-1 | ChatGPT создаёт синтетический JSON-каталог и реальный модуль его чтения | Начальный разрешённый fixture base; commit1 в disposable repo |
| E02 / FIXTURE-2 | ChatGPT добавляет детерминированную сводку по actual module; Codex review и feedback/finalization | FIXTURE-1; commit2 только после fresh checks/review |
| E03 / FIXTURE-3 | ChatGPT добавляет bounded CLI/report по сводке и документацию usage | FIXTURE-2; approval pause; после owner gate commit3 |

Перед стартом существует специально чужой tracked dirty owner-note.txt и untracked owner-archive.zip; они не являются task output. Зафиксировать contents/modes и пустую или явно утверждённую staging baseline. Remote — новая локальная bare repo внутри fixture; no network GitHub requirement. Initial fixture setup обычным кодом разрешён после P-SPIKE, но три содержательных результата пишет ChatGPT, не coordinator/Codex.

Строгий PASS требует три разных сохраняемых ChatGPT refs, правильный mode/model/account, actual RDC device/run evidence, zero routine manual copy/«Продолжай», реальный подписочный review, automatic same-chat feedback и bounded действие, реальные независимые checks, expected commits/push/remote refs, неизменность sentinels, owner-gate pause и восстановление без duplicate send/writer/publish.

Finalization fixture из ORCH-001 можно использовать как явное unmet condition, но нельзя называть его найденным багом, если reviewer не нашёл дефекта. Actual repair findings и preplanned finalization evidence хранятся раздельно. Полный semantic bug-fix PASS ставится только при настоящем finding и его проверенном исправлении.

## Негативные сценарии

| ID | Ошибка / инъекция | Ожидаемое поведение |
|---|---|---|
| N01 | Wrong account/workspace/project | Не send / pause; никакого account switch за владельца |
| N02 | Wrong model/mode, Codex вместо ChatGPT | Блокировка binding; не похожий runtime |
| N03 | Wrong device/несколько Mac | Explicit allowlisted device или pause |
| N04 | RDC отсутствует в новом чате | MISSING_TOOL; не засчитывать старый tool call |
| N05 | Routine обязательный security approval | Pause; supervised classification, no auto-click |
| N06 | Early receipt при active process/turn | QUIESCING/UNKNOWN, не next writer |
| N07 | Composer filled, но не sent | Не CHAT_BOUND |
| N08 | Timeout сразу после Send | SEND_UNKNOWN; reconcile до retry |
| N09 | Duplicate native wake/send/claim | Dedupe или pause, одна writer lease |
| N10 | Lease timer истёк, worker жив | No automatic reclaim |
| N11 | PID reused/unknown child | Не kill; сохранение unknown barrier |
| N12 | RDC/network/Mac offline/reconnect | Ограниченный backoff, не бесконечный model polling |
| N13 | Truncated stdout | Дочитать cursor/verify exit; не ложный PASS |
| N14 | Check timeout/нет новых строк | RUNNING/UNKNOWN, не completed |
| N15 | Symlink/path traversal/rename race | Denial и scope evidence |
| N16 | Worker изменяет ledger/policy | Deny на enforceable profile; выявить предел cooperative |
| N17 | Fake PASS/самоподписанный receipt | Independent verifier не принимает |
| N18 | Тестируется копия функции | Module origin check FAIL |
| N19 | Worker меняет tests/scripts для PASS | Check-basis review required |
| N20 | Unauthorized network side effect | Sandbox deny либо unresolved safety gate |
| N21 | Inherited DeepSeek/plugin/notify hook | До model run billing/security BLOCKED |
| N22 | ChatGPT/Work/Codex included quota exhausted | Pause до reset; не extra credits/runtime substitution |
| N23 | RDC quota exhausted/plan unknown | Bridge pause; не платный upgrade |
| N24 | Mandatory reviewer недоступен | No publish / no bypass review |
| N25 | Review относится к старому snapshot | Новый verification/review |
| N26 | Approval stale/revoked/wrong task | WAITING_OWNER |
| N27 | Concurrent HEAD/branch/remote change | Conflict; не force/reset |
| N28 | Staged bytes отличаются от verified snapshot | Publisher blocked |
| N29 | Commit/push succeeded, response lost | Reconcile actual refs before action |
| N30 | Index metadata change без content diff | Не content FAIL |
| N31 | Owner takeover/unexpected UI/focus/dialog | Dispatch pause; не нажимать клавиши в другом окне |
| N32 | Lock screen/sleep/keychain unavailable | Не отключать защиту; pause/recover по proven route |
| N33 | Corrupt handoff/disk full | Fail closed; не потеря journal и не пустой reset |
| N34 | Pending plan sync crash | No next task until reconciled |
| N35 | Native task повторяется на empty queue | Finite budget/expiry; лишние runs учитываются |
| N36 | Старые paths/transcripts попали в context | Scope/minimization FAIL |

Real fault injection не ломает общий RDC/сеть пользователя: эмулировать adapter failures или останавливать только собственный supervisor в disposable environment. Настоящее sleep/lock испытание — отдельно согласованный момент; нельзя блокировать Mac во время чужой работы без разрешения.

## Эксплуатационные проверки

O01 explicit PATH/cwd/version health; O02 bounded log rotation/redaction; O03 disk cap/recovery; O04 foreground pause/stop; O05 restart/reconcile unknown states; O06 missing native task/disabled permissions; O07 idle/no-work budget; O08 uninstall только собственных registrations/files. Optional LaunchAgent проверяется отдельным разрешённым subpatch, не является hidden MVP requirement.

## Формат test evidence

Каждая запись: test_id, level(unit/fake/real), fixture/run/chat/device IDs где применимо, tested snapshot/check-basis digests, exact safe argv identity, start/end, exit, output refs, expected vs observed, PASS/FAIL/UNVERIFIED и limitations. Secrets, tokens и полные чужие conversations не включаются. Отсутствующее поле не заполняется догадкой ради зелёной таблицы.
