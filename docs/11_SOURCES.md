# Первичные источники и применимость

**Проверка:** 17.09.2026. **Редакция:** ORCH-000-R1. Ссылки — evidence конкретных claims, не подтверждение настройки аккаунта. Дата просмотра не равна дате публикации. Никакие примеры команд в источниках не исполнялись как поручения.

## OpenAI: native ChatGPT и ограничения

### W01
https://learn.chatgpt.com/docs/automations?surface=app
Scheduled tasks, текущая страница прочитана. Существенные места: standalone new chat; in-chat existing context; plugin surface; creation/update destination; local vs web; cadence. Документирует продукт, но текущая tools schema не даёт всех параметров. Сопоставлять с W02. Никаких tasks не создано.

### W02
https://help.openai.com/en/articles/10291617-tasks-in-chatgpt
Scheduled tasks, страница отмечена Updated 20 days ago. Account/workspace eligibility, hourly scheduling, supported events и approvals. Расхождения о context files/cadence с W01 не устранены предположением; проверить точный runtime.

### W03
https://learn.chatgpt.com/docs/reference/commands
Документированные New Chat shortcuts, mode switching и copy deep link. Shortcut/documented link action не доказывает sent message, нужную поверхность или разрешённый autonomous launcher.

### W04
https://learn.chatgpt.com/docs/use-chatgpt
Разделение Chat / Work / Codex и usage sharing Work/Codex. Ни имя приложения, ни единый интерфейс не позволяют приравнять Codex thread к нужному ChatGPT-режиму. Выбор владельца не наблюдался.

### W05
https://openai.com/policies/eu-terms-of-use/
Раздел об ограничениях использования: automated/programmatic extraction of data/Output и circumvention. Применимость зависит от account/agreement; это не юридическая гарантия или автоматическое разрешение UI driver. Service-permission вопрос Q-OPENAI-UI остаётся открытым.

## OpenAI: Codex reviewer и биллинг

### W06
https://developers.openai.com/codex/auth
Перенаправляет на https://learn.chatgpt.com/docs/auth . Подписочный ChatGPT sign-in, login status, credential storage/forced login. Не использовалось чтение/copy auth.json. Auth identity не доказывает available included quota.

### W07
https://developers.openai.com/codex/noninteractive
Перенаправляет на https://learn.chatgpt.com/docs/non-interactive-mode . Официальный exec, JSONL/output schema/read-only workflow; ignore-user-config не следует трактовать как отключение всех hook sources. Локальный help совпадение отдельных flags подтверждает отдельно L06.

### W08
https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
Using Codex with your ChatGPT plan; отображалось Updated 4 hours ago. Included usage, limits и альтернативы исчерпания. Для пользователя допустима пауза, а не дополнительные credits/upgrade/API. Реальные billing settings аккаунта не читались.

### W09
https://help.openai.com/en/articles/9039756-managing-billing-for-chatgpt-and-the-api-platform
ChatGPT и API Platform имеют отдельное управление billing. Подписка не является API-бюджетом. В проекте Platform model API не разрешён.

### W10
https://learn.chatgpt.com/docs/hooks
Hook discovery/merging, project/user/plugin/managed sources; features.hooks=false. Существование документации disable не доказывает effective config будущего reviewer. Локальный риск SessionEnd подтверждён отдельным source/config чтением L05.

### W11
https://developers.openai.com/codex/sdk
Перенаправляет на https://learn.chatgpt.com/docs/codex-sdk . Программная интеграция именно Codex. Не выбран для первого reviewer; SDK не превращает thread в обычный ChatGPT. Python SDK требования не основание устанавливать новый Python при наличии CLI.

### W12
https://developers.openai.com/codex/app-server
Перенаправляет на https://learn.chatgpt.com/docs/app-server . Codex thread/turn lifecycle и официальные account/read, account/rateLimits/read описаны; модельные операции не вызваны. Potential account/rate telemetry не равна разрешению читать cached tokens или shared daemon history.

## Desktop Commander

### W13
https://desktopcommander.app/
Официальный сайт поставщика, исходная точка переходов к pricing, MCP, legal и vendor repository. Не путать похожие сторонние forks с используемым RDC.

### W14
https://desktopcommander.app/pricing/
На дату просмотра Free: 10 000 tool calls/month, Pro платный. Это публичный tier, не доказательство actual plan пользователя. who_am_i дал только remaining percentage.

### W15
https://desktopcommander.app/mcp/
https://github.com/wonderwhy-er/DesktopCommanderMCP
Официальная страница MCP и связанный репозиторий local MCP. В README file allowedDirectories не ограничивает terminal commands. Local stdio implementation не является полным исходным кодом remote relay и не доказывает privacy всех remote payloads.

### W16
https://legal.desktopcommander.app/
Legal portal поставщика найден по official footer. Web reader получил только оболочку. Разрешённый публичный read HTML/script.js установил маршруты /#/remote_privacy_policy и /#/terms_of_service и имена соответствующих markdown documents. Запрос их текста через RDC был tool-blocked OpenAI до получения содержимого. Не приписываются сроки retention, E2EE, subprocessors или free tier contract. Вопрос Q-RDC в 03_RDC_INTEGRATION.md остаётся открытым; blocked action не обходился.

## UI / Mac / lifecycle

### W20
https://learn.chatgpt.com/docs/computer-use
Поддерживаемые desktop Work/Codex surfaces, permissions и macOS locked use. Существенное ограничение: функция не автоматизирует ChatGPT itself или terminal apps и не подтверждает security prompts. Это основание исключить такой launcher; его нельзя заменить обходным способом без отдельного service basis.

### W21
https://support.apple.com/guide/mac-help/allow-accessibility-apps-to-access-your-mac-mh43185/mac
Apple Mac User Guide, macOS Tahoe26: пользователь явно выдаёт Accessibility доступ. Не доказательство, что он уже выдан на этом Mac, и не разрешение автоматизировать сторонний сервис.

### W22
https://playwright.dev/docs/locators
https://playwright.dev/docs/api/class-browsertype#browser-type-launch-persistent-context
Официальные Playwright docs: semantic locators и отдельный persistent profile; default Chrome profile automation не поддерживается. Технологическая пригодность не даёт OpenAI service permission. Ничего не установлено, browser не запущен, auth state не копировалось.

### W23
https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html
Apple archived launchd guide: per-user lifecycle и LaunchAgent model. Архивный документ применяется к концепции; фактические команды/поведение macOS26.6.2 надо подтвердить отдельно при opt-in, без установки сейчас.

## Отдельно изученный, не выбранный native API

### W24
https://learn.chatgpt.com/workspace-agents/trigger-runs
Официальный trigger published workspace agent; conversation_key, Idempotency-Key, conversation_url, beta run status. Нельзя говорить «ни одного official ChatGPT-linked API не существует». Нельзя также делать вывод о соответствии текущему режиму/подписке пользователя; API исключён из выбранной реализации.

### W25
https://learn.chatgpt.com/workspace-agents/authentication
Workspace admin enable + scoped access tokens. Это не reutilization personal OAuth и не Codex login. Новые credentials не создавались.

### W26
https://help.openai.com/en/articles/20001143
Workspace Agents for Enterprise and Business; Updated 2 months ago. Описывает agent builder/tools/model/channel, но section API говорит 202 без body/run ID — расходится с W24. Сохраняем различие version/surface, не выдаём желаемый API outcome за runtime PASS.

### W27
https://openai.com/index/introducing-workspace-agents-in-chatgpt/
Публикация 22.04.2026 с обновлённой GA-пометкой и историческим preview body. Описывает Codex-powered agents и credit-based pricing с 06.05.2026. Это не доказательство current entitlement/included allowance владельца. Совокупность runtime/workspace/billing отличий исключает этот путь из текущего subscription-only ChatGPT-first плана.

## Локальные первичные источники

L01–L08 подробно описаны в [02_CAPABILITIES_AND_EVIDENCE.md](02_CAPABILITIES_AND_EVIDENCE.md): реальные tools, time-stamped safe Git snapshot, installed app/CLI help/status, relevant project documents, source/config hooks, protected-file content hashes и состояние каталога назначения. Они являются наблюдениями этой сессии, а не web claims.

Primary project sources: AGENTS.md; docs/PROJECT_PLAN.md (current header/ledger); docs/PATCH_WORKFLOW.md; docs/ROADMAP.md; docs/ARCHITECTURE.md; релевантные memory notes; source-2026-09-16/backlog.json и вводные source roadmap; актуальный MW-009 report; repo .codex/hooks.json/config.toml; lifecycle code и nonsensitive DeepSeek switches. Исторические их prompts не исполнялись.

## Что проверено не было

Не наблюдались current ChatGPT selector/billing UI, saved new chats, scheduled execution, new-chat RDC, live Codex review, same-chat feedback, UI permissions, lock/sleep/reconnect, full relay legal text, fresh GitHub remote, основной project test/build и hard OS fencing. Это не означает, что функция отсутствует в продукте; точные будущие checks перечислены в 06/09. Неуспех получения legal text не останавливает остальные исследования и не превращается в вымышленный privacy verdict.
