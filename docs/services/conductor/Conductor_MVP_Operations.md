# Conductor MVP Operations

## Назначение документа

Этот документ содержит операционное описание текущего `Conductor / Дирижёр MVP`.

Документ фиксирует практическую информацию по запуску, webhook, переменным окружения, Notion, Todoist, OpenAI transcription, командам и эксплуатации MVP.

Сервисное описание роли и границ `Conductor` хранится отдельно:

```text
docs/services/conductor/Conductor_Service_Description.md
```

Краткое описание папки сервиса хранится отдельно:

```text
docs/services/conductor/README.md
```

## Текущий статус MVP

`Conductor / Дирижёр MVP` — сервис для Telegram-бота `Lyuba` и двусторонней синхронизации задач `Notion ↔ Todoist`.

Текущая рабочая логика:

- `Todoist` используется как основной рабочий интерфейс задач;
- `Notion` используется как общая база задач для `Lyuba` и будущих агентов;
- `Conductor` принимает входящие сообщения, классифицирует их, создает записи и синхронизирует задачи.

На текущем этапе код сервиса находится в корневой папке:

```text
conductor/
```

Целевой перенос в `apps/conductor/` не выполняется без отдельной миграционной задачи.

## Что поддержано

Текущий MVP поддерживает:

- Telegram webhook: `POST /telegram/webhook`;
- проверку здоровья: `GET /healthz`;
- текстовые сообщения;
- голосовые и аудиосообщения через Telegram file API и OpenAI transcription;
- AI-классификацию на задачи и вопросы на изучение;
- уточнения в Telegram, если не хватает проекта, срока или уверенность ниже порога;
- создание задач в Notion `Tasks`;
- создание записей в Notion `Study / На изучение`;
- локальное хранение ожидающих уточнений в `data/pending.json`;
- полную двустороннюю синхронизацию базы `TASKS` и Todoist.

## Быстрый старт

### 1. Создать `.env`

```bash
cp .env.example .env
```

### 2. Заполнить обязательные токены

Минимальный набор:

```text
TELEGRAM_BOT_TOKEN
OPENAI_API_KEY
NOTION_TOKEN
```

Для синхронизации с Todoist также нужны:

```text
TODOIST_API_TOKEN
TODOIST_WEBHOOK_SECRET
TASK_SYNC_SECRET
```

### 3. Запустить локально

```bash
python3 -m conductor.app
```

### 4. Локальный тест без Telegram

```bash
python3 -m conductor.cli "Завтра напомни написать Марко по алюминию. И изучить доступные логистические пути в Веракрус"
```

## Notion базы

Текущие ID баз указаны в `.env.example`.

Текущие базы:

| База | ID |
|---|---|
| `Tasks` | `be9d26fe652b474696cd5de0118b1210` |
| `Study / На изучение` | `4e27e10ca2bf44a08b4c8f86c7a125bd` |
| `Projects / Приоритеты` | `bbb501a6933941b4837afff250479f0e` |

## Важная логика MVP

Текущие правила MVP:

- если срок не указан, `Conductor` спрашивает срок;
- если проект не найден или уверенность ниже `CONFIDENCE_THRESHOLD`, `Conductor` спрашивает уточнение;
- если в сообщении есть и задачи, и вопросы на изучение, создаются обе сущности;
- исходный `RAW` отдельно не сохраняется;
- Todoist включается при наличии `TODOIST_API_TOKEN`;
- аварийная пауза Todoist sync управляется только переменной `TODOIST_SYNC_PAUSED`.

## TASKS ↔ Todoist

### Общая модель

Текущая модель маршрутизации:

- `STREAMS` соответствует родительскому проекту Todoist;
- `PROJECTS` соответствует дочернему проекту Todoist;
- расположение задачи в Todoist определяет `TASKS.Проект`;
- `TASKS.Stream` определяется через связь `Project → Stream`;
- раздел Todoist записывается прямо в `TASKS.Раздел` и `TASKS.Todoist Section ID`;
- разделы создаются и редактируются в Todoist;
- отдельной базы Sections нет;
- проектные метки больше не используются для маршрутизации.

### Обязательные поля Notion `TASKS`

Обязательные поля базы Notion:

```text
Task
Описание
Статус
Deadline
Срок выполнения
Strategic Impact
Source
Проект
Stream
Раздел
Todoist Section ID
Метки Todoist
Todoist ID
Sync status
Sync error
Sync Notion hash
Sync Todoist hash
```

### Сопоставление полей

| Notion `TASKS` | Todoist / назначение |
|---|---|
| `Task` | название задачи |
| `Описание` | описание задачи |
| `Статус: Done` | задача завершена |
| `Статус: Cancelled` | задача завершена с сохранением истории |
| `Срок выполнения` | due date |
| `Deadline` | deadline |
| `Strategic Impact` | priority |
| `Проект` | проект, в котором находится задача |
| `Stream` | группа проекта; определяется через `Project → Stream` |
| `Раздел` | название раздела внутри проекта |
| `Todoist Section ID` | стабильная связь с разделом |
| `Метки Todoist` | только разрешенные операционные метки |
| `Todoist ID` | стабильная связь записей |
| `Sync status` | техническое состояние синхронизации |
| `Sync error` | последняя причина ошибки; очищается после успешной сверки |
| `Sync Notion hash` | технический отпечаток последней синхронизированной версии Notion |
| `Sync Todoist hash` | технический отпечаток последней синхронизированной версии Todoist |

### Разрешенные управляемые метки

Разрешенные управляемые метки:

```text
встреча
звонок
письмо
сообщение
документ
анализ
исследование
планирование
низкая_энергия
средняя_энергия
высокая_энергия
пятиминутное_дело
```

Служебная метка:

```text
проверить_завершение
```

Назначение служебной метки: показать задачи, которые `Lyuba`, агент или пользователь завершили в Notion и которые нужно подтвердить закрытием в Todoist.

Все остальные метки сохраняются без изменений.

## Режимы Todoist sync

### `todoist-primary`

Рабочий режим `todoist-primary` использует Todoist как первоначальный источник.

Правила:

- первая сверка новой версии переносит текущие версии связанных задач Todoist в Notion;
- webhook переносит изменения Todoist почти сразу, включая задачи во входящих;
- периодическая сверка каждые 5 минут восстанавливает пропущенные события;
- новые активные задачи из Notion создаются в Todoist: в назначенном проекте или во входящих;
- последующие конфликты решаются по времени изменения;
- удаление Todoist переводит запись Notion в `Cancelled`;
- `Done` или `Cancelled` из Notion не закрывает Todoist без второго ключа;
- при `Done` или `Cancelled` из Notion задача остается активной с меткой `проверить_завершение`.

### `observe`

Режим `observe` остается доступен для инвентаризации без удаленных записей.

## Переменные окружения для Todoist sync

Базовые переменные:

```bash
TODOIST_SYNC_PAUSED=false
TODOIST_SYNC_MODE=observe
TODOIST_API_TOKEN=...
TODOIST_WEBHOOK_SECRET=...
TASK_SYNC_SECRET=...
```

Защитные разрешения для поэтапного canary-запуска:

```bash
TODOIST_ALLOW_PROJECT_CREATE=false
TODOIST_ALLOW_TASK_CREATE=false
TODOIST_ALLOW_TASK_MOVE=false
TODOIST_ALLOW_LABEL_WRITE=false
TODOIST_ALLOW_STATUS_WRITE=false
TODOIST_ALLOW_MISSING_CANCEL=false
TODOIST_MAX_TASK_MOVES=10
```

Аварийная пауза без удаления токенов:

```bash
TODOIST_SYNC_PAUSED=true
```

## Ручной reconciliation

Ручной запуск сверки:

```bash
curl -X POST \
  -H "X-Conductor-Sync-Secret: $TASK_SYNC_SECRET" \
  https://YOUR_DOMAIN/tasks/sync
```

## Todoist webhook

Для мгновенного обновления `Todoist → Notion` нужно зарегистрировать webhook приложения Todoist:

```text
https://YOUR_DOMAIN/todoist/webhook
```

В `TODOIST_WEBHOOK_SECRET` указывается `Client Secret` приложения Todoist.

Если webhook не настроен, периодический reconciliation продолжает синхронизировать активные задачи и завершения.

Удаления определяются периодической сверкой после успешной загрузки истории завершенных задач.

С webhook изменения попадают в Notion почти сразу.

## Telegram webhook

Для публичного запуска нужен HTTPS URL.

После деплоя выставить webhook:

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=https://YOUR_DOMAIN/telegram/webhook"
```

Или через скрипт:

```bash
TELEGRAM_BOT_TOKEN=... PUBLIC_BASE_URL=https://YOUR_DOMAIN sh deploy/set_webhook.sh
```

## Онлайн-запуск

Проект подготовлен для Render через:

```text
Dockerfile
render.yaml
```

Нужные переменные окружения на хостинге:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET
OPENAI_API_KEY
NOTION_TOKEN
NOTION_TASKS_DATABASE_ID
NOTION_STUDY_DATABASE_ID
NOTION_PROJECTS_DATABASE_ID
TODOIST_API_TOKEN
TODOIST_WEBHOOK_SECRET
TASK_SYNC_SECRET
```

`TELEGRAM_WEBHOOK_SECRET` — любая длинная случайная строка. Переменная опциональна, но желательна.

После деплоя нужно вызвать Telegram `setWebhook` на публичный URL сервиса.

## OpenAI transcription

MVP поддерживает голосовые и аудиосообщения через связку:

```text
Telegram file API
OpenAI transcription
```

Для работы нужен:

```text
OPENAI_API_KEY
```

Транскрибированный текст дальше передается в общую логику классификации входящего сообщения.

## Команды и endpoints

| Назначение | Команда / endpoint |
|---|---|
| Локальный запуск | `python3 -m conductor.app` |
| CLI-тест без Telegram | `python3 -m conductor.cli "текст сообщения"` |
| Telegram webhook | `POST /telegram/webhook` |
| Health check | `GET /healthz` |
| Ручной sync | `POST /tasks/sync` |
| Todoist webhook | `POST /todoist/webhook` |

## Эксплуатационные правила

### Перед запуском

Проверить:

- заполнен `.env`;
- есть `TELEGRAM_BOT_TOKEN`;
- есть `OPENAI_API_KEY`;
- есть `NOTION_TOKEN`;
- указаны ID Notion-баз;
- при включении Todoist sync есть `TODOIST_API_TOKEN`;
- при включении ручного sync есть `TASK_SYNC_SECRET`;
- при webhook Todoist есть `TODOIST_WEBHOOK_SECRET`.

### При проблемах с Telegram

Проверить:

- публичный HTTPS URL;
- корректность Telegram webhook;
- переменную `TELEGRAM_BOT_TOKEN`;
- endpoint `POST /telegram/webhook`;
- доступность `GET /healthz`.

### При проблемах с Notion

Проверить:

- `NOTION_TOKEN`;
- ID баз Notion;
- доступ интеграции Notion к базам;
- обязательные поля базы `TASKS`;
- корректность названий полей;
- ошибки в логах Conductor.

### При проблемах с Todoist sync

Проверить:

- `TODOIST_API_TOKEN`;
- `TODOIST_SYNC_PAUSED`;
- `TODOIST_SYNC_MODE`;
- `TASK_SYNC_SECRET`;
- `TODOIST_WEBHOOK_SECRET`;
- защитные переменные canary-запуска;
- последние значения `Sync status`;
- поле `Sync error`;
- значения `Sync Notion hash` и `Sync Todoist hash`.

### При проблемах с OpenAI transcription

Проверить:

- `OPENAI_API_KEY`;
- доступность Telegram file API;
- тип входящего сообщения;
- ошибки загрузки аудиофайла;
- ошибки транскрибации.

## Что не должно храниться в этом документе

Этот документ не должен содержать:

- архитектурную карту всей AI OS;
- roadmap;
- планы будущих агентов;
- детальное описание Data Ownership;
- подробные use cases;
- новые архитектурные решения;
- секретные токены и реальные значения ключей.

Для этих целей используются отдельные документы.

## Связанные документы

| Документ | Назначение |
|---|---|
| `docs/services/conductor/README.md` | краткое описание папки сервиса |
| `docs/services/conductor/Conductor_Service_Description.md` | роль, ответственность и границы Conductor |
| `docs/services/conductor/Conductor_MVP_Operations.md` | текущий операционный документ |
| `docs/architecture/System_Map.md` | верхнеуровневая карта AI OS |
| `docs/architecture/System_Component_Registry.md` | компоненты по слоям |
| `docs/data/Data_Ownership_Map.md` | источники истины |
| `docs/product/use_cases/` | пользовательские сценарии |
| `.env.example` | пример переменных окружения |
| `README.md` | корневое описание репозитория |

## Следующие доработки, зафиксированные в текущем README

В текущем README указаны следующие будущие доработки:

- кнопки `Изменить` и пошаговое редактирование параметров;
- OCR для фото и документов;
- поддержка испанского и английского.

Эти пункты не являются задачами в рамках данного операционного документа.

Если они переводятся в работу, их нужно оформить отдельно в `docs/roadmap/` или в конкретных `docs/product/use_cases/`.

## Текущий статус

Статус документа: базовая версия.

Документ создан как перенос операционной информации Conductor MVP из корневого README в сервисную документацию.

При изменении запуска, webhook, переменных окружения, Notion, Todoist, OpenAI transcription или команд этот документ должен обновляться.
