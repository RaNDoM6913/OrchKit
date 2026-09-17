# Требования и трассировка

**Редакция:** ORCH-000-R1, 17.09.2026. Основание — MASTER_PROMPT_V3_CHATGPT_RDC_RESEARCH_RU.txt целиком и текущие явные ограничения владельца. Исторические файлы основной репы — контекст, а не дополнительная очередь исполнения.

## Обязательный результат

Инструмент автоматизирует именно текущий содержательный процесс ChatGPT → RDC → Mac, добавляя обычный локальный диспетчер и периодическое Codex-ревью. После принятия ограниченной очереди обычные переходы не требуют ручного копирования или «Продолжай». Реальные owner/security/quota/billing gates сохраняются.

| ID | Требование | Где проектируется / проверяется |
|---|---|---|
| R01 | Сохраняемые видимые ChatGPT-чаты с ручным открытием позже | 02 C01–C03; 06 G1/G2/G8 |
| R02 | Правильные account/workspace/project/mode/model, не Codex-thread | 02 C04; 06 identity gate |
| R03 | Initial prompt содержит уникальный literal RUN_ID; сообщение отправлено | 05 dispatch; 09 N08/N09 |
| R04 | Новый чат обнаруживает RDC и именно разрешённый Mac | 03 device binding; 06 G3 |
| R05 | ChatGPT выполняет анализ, изменения и исправления | 05 flow; 09 real E2E |
| R06 | Codex выполняет только периодический независимый review | 04 isolation; 05 review_policy |
| R07 | Feedback автоматически возвращается в тот же связанный ChatGPT | 06 G7; новая repair-сессия не равна resume |
| R08 | Новый блок получает новый настоящий чат и bounded context | 05 context; 06 G8 |
| R09 | Ноль дополнительных модельных расходов и внешнего AI | 04; 09 billing/hook negative tests |
| R10 | Конечные budgets задач, повторов, времени, диска и RDC calls | 04/10; exhaustion всегда pause |
| R11 | Один writer; receipt не означает quiescence | 07; 09 N06/N10/N11 |
| R12 | Verifier сам проверяет реальные bytes и actual module | 05/07; 09 false-pass и test-basis |
| R13 | Проверки и review привязаны к immutable snapshot | 05; 09 stale snapshot |
| R14 | Publication отдельно от implementation/acceptance | 07 exact allowlist / remote verification |
| R15 | Чужие staged/dirty/untracked/handoff/ZIP сохраняются | 07; 09 three-chat E2E |
| R16 | Исходный MW backlog immutable; явная синхронизация PROJECT_PLAN | 05 source-of-truth protocol; <separately-authorized-integration> |
| R17 | Перезапуск не дублирует send/writer/commit/push | 07 reconciliation; ORCH-006 |
| R18 | Неизвестность не записывается как PASS | 02 evidence taxonomy; весь spike |
| R19 | Нет обхода авторизации, подтверждений и service constraints | 04; UI reserve disabled |
| R20 | Исследование не вмешивается в основную репу и общий RDC | 02 local evidence; 10 research boundary |
| R21 | Принятие очереди не равно visual/behavior ACCEPTED | 07 approval ledger; <separately-authorized-integration> contract |
| R22 | До большой разработки — реальный интеграционный spike | ORCH-001, затем ORCH-002–007 |

## Жёсткие non-goals

Не автоматизировать торговлю, реальные MT5/prop-аккаунты, ордера или финансовые решения. Не менять правила FundingPips. Не выполнять MW-backlog в этой сессии. Не строить собственный chat UI вместо истории ChatGPT. Не использовать новые чаты для обхода квот; не направлять OAuth в самодельный model proxy.

Не включать SaaS, paid tunnel, облачную очередь, платные computer-use/vision API, внешний AI, upgrades/credits. Бесплатный локальный код допустим только в утверждённых будущих этапах; текущая запись ограничена исследовательскими документами.

## Фактический контекст основной репы

Начальный read-only срез 03:17:37 UTC: main / a30a2592885915b6d289606b4738f178fac94d4d; local origin/main совпадает без проверки сети. PROJECT_PLAN — текущий execution source of truth. MW-009, MW-002, MW-003 BLOCKED; активной implementation-задачи нет. Исторические записи о тестах и SHA не переносятся в текущие результаты (L03/L04).

Исходный backlog содержит 38 задач; в нём implementation_status=not_assessed и proposal_status=proposed являются исходными утверждениями пакета, а не текущим состоянием разработки. Полный JSON был разобран для IDs/dependencies; вводная часть 04_ROADMAP_AND_BACKLOG.md прочитана для семантики. Исходные файлы не переписывались.

Существующее «один Продолжай — один MW-блок» относится к другой рабочей сессии. Будущий automation contract может изменить routine dispatch только по отдельному явному разрешению; owner acceptance и предметные gates не исчезают. Пока <separately-authorized-integration> запрещён к выполнению.

## Что считается успешной автоматизацией

Начальный одноразовый login, подтверждение безопасных прав и выбор режима владельцем допустимы. Ручная пересылка задания, reviewer feedback или «Продолжай» после каждого обычного шага — failure целевого unattended режима. Наличие периодического обязательного approval честно классифицирует маршрут как supervised.

История должна содержать реально выполненную работу ChatGPT. Не принимаются три локальных run_id, три Codex thread_id, три пустых UI-окна или результаты fake adapters. Завершение текста ответа, success tool или JSON валидность не являются содержательным завершением задачи.

## Согласованные параметры, которые нельзя угадать

Фактические ChatGPT account/workspace/project, режим и модель ещё не наблюдались через разрешённую поверхность. Название приложения, bundle com.openai.codex, модель в ~/.codex/config.toml и название текущей модели ассистента не заменяют этот выбор. До spike owner выбирает текущий желаемый режим один раз; launcher фиксирует наблюдение и повторно проверяет его, не переключая без согласования.

Не известны реальные остатки included ChatGPT/Work/Codex usage, наличие приобретённых credits и правила их автоматического расхода. У RDC известен процент, но не actual billing plan/reset. Неизвестные параметры блокируют соответствующий run, а не оправдывают покупку или «бесплатный trial».

## Результат текущего этапа

Этот пакет даёт выбранный условный маршрут, локальную инвентаризацию, evidence matrix, архитектуру, исполняемый план spike и последовательных патчей, тестовые и эксплуатационные контракты. Он не выдаёт разрешение самому себе. Все будущие CLI, JSON и команды в документах — проектные примеры.
