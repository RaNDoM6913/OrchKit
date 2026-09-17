# ORCH-001 — ранний feasibility spike

**Статус:** спроектирован, НЕ запущен. **Редакция:** ORCH-000-R1. Это проверка настоящей интеграции до production-разработки, а не fake demo и не выполнение MW-backlog.

## Цель и ограничения

Доказать цепочку: настоящий ChatGPT-A → существующий RDC → безопасный локальный artifact → независимый verifier → настоящий подписочный Codex review → автоматический feedback в ChatGPT-A → bounded исправление/завершение → настоящий ChatGPT-B с новым RDC bootstrap и коротким handoff.

Рабочая зона только будущий disposable каталог <orchkit-root>/spikes/<unique-spike-id>. Основная репа не используется даже как cwd reviewer. На ORCH-001 не нужны Git, publisher, LaunchAgent, SQL migrations, новый MCP или dashboard. Достаточно foreground coordinator, typed helper, простого journal и независимого fixture checker. Целевой размер минимального scaffold — несколько небольших модулей, не весь ORCH-002–007.

Разрешения P-SPIKE пока отсутствуют. Никакие steps ниже не являются текущими командами к исполнению.

## Бюджет эксперимента — предлагаемые пределы

Два новых рабочих ChatGPT-чата, максимум два Codex review runs (initial и проверка исправленного snapshot), до двух repair cycles в первом чате, до двух одновременно зарегистрированных owned native tasks. Не более 120 RDC calls на spike с локальным предупреждением на 90; остановиться раньше при provider quota/billing uncertainty. Время активной управляемой команды до 120 секунд, общий active processing cap 30 минут, wall-clock expiry 24 часа с учётом native scheduling. Это guardrails, не оценка сроков и не лимиты OpenAI.

Все native tasks одноразовые или имеют конечный COUNT/expiry. Самый частый доступный здесь recurring cadence — hourly; быстрый minute loop не предполагается. Пропуск следующего запуска из-за занятости не даёт права начать конкурирующего writer. Не выполнять пустой recurring dispatcher до бесконечности ради доказательства.

## G0 — no-model prerequisites

После approval перечитать docs/правила отдельного проекта и проверять фактическое состояние, не стартовать из старого SHA. Подтвердить отсутствие параллельных пишущих ORCH-сессий. Владелец один раз выбирает account/workspace/project/mode/model в настоящем ChatGPT; настройка фиксируется, не угадывается. Согласовать только синтетический input и существующий RDC.

Подтвердить included billing ChatGPT и Codex, отсутствие дополнительного credit spillover и actual RDC entitlement. Создать isolated reviewer home, выполнить официальный отдельный sign-in владельцем, не экспортировать credentials. До model run проверить features.hooks=false, отсутствие notify/plugin/MCP/external provider/managed hook side effects и отдельный readonly snapshot cwd. Только safe status/config inspection; запуска проекта для «проверки безопасности» нет.

**Stop:** неизвестный биллинг, требуемый upgrade, inherited external hook, несовпадающий режим, риск общей RDC-сессии. Эти причины не обходятся API, Codex worker или другим аккаунтом.

## G1 — доказать native launcher contract до scaffold

Изучить разрешённые account-native controls / фактические schemas для standalone task и in-chat continuation. Нужны: назначение нового настоящего ChatGPT, выбранный runtime, сохранённый prompt с literal unique RUN_ID, finite trigger, способ independently observe chat ref, same-chat wake-up и последующий next-chat запуск без ручного переноса.

Конкретная проверка: получить/сохранить task definition с destination/mode/model/project и supported result metadata. Текущая automations.create этих полей не даёт; нельзя добавить их к JSON или предположить выбор по title. Допускается одноразовая ручная настройка официальных task controls владельцем; после неё обычная передача задания/feedback/следующего задания должна происходить автоматически.

Отдельный blocker проверяется прямо: как coordinator/поддерживаемый native механизм обновляет prompt для следующего literal RUN_ID и запускает задачу только после разрешения? Статический RUN_ID, два заранее одновременно пишущих чата, future timestamp вместо barrier и ручное редактирование каждого следующего task не проходят. Если документированного/доступного способа нет, зафиксировать G1=UNVERIFIED или UNSUPPORTED для точной surface и остановить live spike. Не строить production как обход.

**Evidence:** account-safe configuration record, actual tool/schema или scoped native control observation, service basis, конечный trigger definition, idempotency/recovery limits. Скриншот только собственного разрешённого окна, без чужих разговоров/secret settings; официальный Computer Use не применять к ChatGPT.

## G2 — создать и отправить настоящий ChatGPT-A

Coordinator генерирует spike_id/task_id/run_id/attempt и сохраняет launch_intent + exact initial prompt digest. Доказанный G1 launcher создаёт и отправляет prompt. Нужно независимо получить наблюдаемый ChatGPT URL/ID, saved user message с тем же RUN_ID и подтвердить нужный account/mode/model. Заполненный composer, title окна и local run file — не evidence отправки.

При ambiguous create/send не повторять отправку: найти исходный owned task run/chat по supported metadata. Если это невозможно, SEND_UNKNOWN и pause. Переполнение бюджета новых чатов недопустимо. Даже returned 202 сам по себе не подтверждает содержательную работу.

## G3 — RDC bootstrap в новом чате

Именно ChatGPT-A выполняет discovery RDC, list_devices, связывает c87394ca-e556-4f14-85be-3269a33f0040 и обращается к helper. Helper проверяет literal RUN_ID, task digest, одноразовый nonce, account/runtime binding evidence coordinator и отсутствие другого writer. В bootstrap receipt отдельные launcher witness и worker handshake.

Нельзя засчитать успешный вызов RDC из текущего исследовательского чата вместо нового. Запрос выбора устройства, повторного plugin enable или подтверждения каждого routine tool call фиксируется; обязательное подтверждение не нажимается автоматически. Постоянная необходимость таких действий делает маршрут supervised.

## G4 — безопасный artifact и реальные evidence

Fixture заранее утверждается целиком: ChatGPT пишет artifact/manifest.json с schema_version, RUN_ID, тремя синтетическими label records, явным draft status и полями verification_nonce/review_ack для следующей фазы. Внутри disposable workspace, но вне worker allowlist, заранее лежат owner-note.txt и owner-archive.zip как protected sentinels. Нет торговых данных, токенов, новых network services и shell из произвольного Markdown.

ChatGPT должен действительно создать файл через RDC; готовый artifact от Codex или coordinator не засчитывается. Coordinator может создать входной fixture/nonce/схему, но не продуктовый результат worker. Worker submit содержит changed paths, evidence refs и open finalization conditions.

## G5 — quiescence и независимая проверка

После submit закрыть helper write lease и подтвердить окончание всех управляемых процессов. Также нужен наблюдаемый конец model turn либо другой доказанный launcher barrier, исключающий продолжающиеся direct RDC writes. Если все writes не охвачены enforceable boundary, зарегистрировать cooperative residual risk; при неизвестном active worker новый writer запрещён.

Verifier отдельным обычным процессом читает bytes, проверяет JSON/schema, expected RUN_ID/records, exact scope/symlinks, неизменность sentinels, final process exit. Строит frozen manifest/hash и выдаёт verification_nonce только после настоящего check. Сохраняет check argv identity, exit, stdout/stderr и digest; overall task ещё не DONE.

## G6 — настоящий Codex review без внешнего AI

Coordinator экспортирует immutable snapshot без активных конфигураций; передаёт bounded task/checklist, actual verifier evidence и условие finalization. Изолированный Codex через официальный ChatGPT sign-in выполняет read-only review. Сохраняются auth/billing preflight refs, version/model, JSONL, actual verdict/findings, exit code, snapshot digest. При malformed/incorrectly bound report — BLOCKED, не PASS.

Проверка finalization заранее честно определена: финальный manifest должен содержать nonce первой проверки и digest первоначального реального review report. Stage-A draft не мог знать их заранее; draft — не якобы готовый продукт. Reviewer оценивает реальное состояние и может отметить незавершённое finalization condition. Не приписывать ему выдуманный баг или verdict. Если actual review не содержит дефекта, это записывается как clean review; fixture completion не называется обнаружением реальной ошибки.

## G7 — автоматический feedback и bounded действие в том же чате

Coordinator сохраняет initial review report и feedback envelope с run_id/snapshot_id/report_digest/nonce. Доказанный native in-chat continuation **после завершения прежнего turn** возвращает ChatGPT-A в тот же actual chat ref. ChatGPT читает mailbox через RDC и выполняет реальные замечания; затем завершает preapproved finalization полей. Владелец ничего не копирует и не пишет «Продолжай».

Verifier проверяет изменённый artifact заново; review_ack относится к initial report, а не к бесконечно меняющемуся latest report. При содержательном исправлении second Codex review оценивает новый snapshot. Изменение проверки/критерия требует отдельного review, не подгонки PASS. Необходимость нового ChatGPT repair-chat вместо resume считается отклонением R07, которое нельзя молча принять.

**Evidence:** continuation trigger, одинаковый actual chat ref, actual report digest в запросе/ответе helper, новый artifact snapshot, свежие checks, ноль manual relay. Если замечаний не было, отдельно отметить delivery/finalization PASS и bug-repair НЕ проверен; для strict repair acceptance нужен согласованный unmet finalization condition либо реальное finding, без фабрикации.

## G8 — второй настоящий ChatGPT-B

Только после проверенной quiescence/finalization A coordinator разрешает SPIKE-B. Launcher формирует новый literal RUN_ID и короткий context ≤8 KiB; автоматически создаёт другой сохраняемый ChatGPT, не fork всей истории. Его prompt ссылается на итоговый snapshot A, а не на весь transcript.

ChatGPT-B повторно обнаруживает RDC и устройство, читает только разрешённый handoff, создаёт отдельный harmless summary artifact. Проверить independent chat ref B≠A, правильный runtime, ownership/writer lease и verifier exit. Нельзя просто открыть второй пустой чат или подставить Codex thread_id.

## G9 — закрытие и классификация

Остановить только свои owned task registrations/foreground процессы; подтвердить отсутствие следующего scheduled wake-up. Не удалять ChatGPT-чаты, общий RDC или profile владельца. Сохранить evidence index, logs, snapshots, usage deltas доступной точности и явные remaining gaps. Основной проект остаётся read-only.

PASS_NATIVE_E2E: выполнены G0–G8 с двумя реальными ChatGPT, automatic same-chat feedback/finalization, настоящим included Codex review и без bypass/manual relay. SUPERVISED_ONLY: цепочка работает лишь с routine ручными действиями. BLOCKED_CAPABILITY/BILLING/PERMISSION/SAFETY: конкретный непройденный gate. Fake tests/частичный ChatGPT→RDC успех — полезные результаты, но не full PASS.

## Один пакет разрешений P-SPIKE

```text
План ORCH-000-R1 принимаю. Разрешаю описанный native subscription-only
ORCH-001 spike в отдельном disposable каталоге agent-workflow-orchestrator,
с двумя настоящими ChatGPT-чатами, существующим RDC и максимум двумя
Codex-review runs через официальный подписочный вход.
Разрешаю одноразовую ручную настройку собственного test runtime/tasks
и изолированный Codex login, но не ручную пересылку на обычных шагах.
Разрешаю минимальный foreground helper/coordinator, синтетические fixtures
и независимые проверки только внутри отдельного проекта инструмента.
При всех gates PASS разрешаю последовательную реализацию ORCH-002–007
и disposable MVP tests, включая отдельную Git-репу и локальный bare remote.
Основной <protected-project> остаётся read-only; <separately-authorized-integration> запрещён.
Постоянный автозапуск, изменение общего RDC/глобальных конфигураций,
платные API/credits/upgrades/внешние AI, Workspace Agents API и UI обходы
не разрешены. При неизвестном billing, service permission, активности
writer или inherited AI-hook — pause, не замена ChatGPT другим worker.
Понимаю, что нынешний broad RDC даёт cooperative, не OS-enforced isolation;
этот остаточный риск разрешён только для синтетического disposable spike.
```

Этот пакет ещё не выдан. Он не отменяет реальных security confirmations. B_UI не включён в автоматическое продолжение: для него сначала ясное service basis и отдельное согласование точной поверхности, а не обход Computer Use. Если G1 невозможно доказать доступными native controls, следующий результат — bounded gap report, не многопатчевая реализация без launcher.
