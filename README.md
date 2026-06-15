# Conductor / Дирижер MVP

Минимальный сервис для Telegram-бота "Люба": принимает текст или голос, классифицирует поток на задачи и вопросы на изучение, записывает результат в Notion, а Todoist держит как следующий подключаемый слой.

## Что уже поддержано

- Telegram webhook: `POST /telegram/webhook`
- Проверка здоровья: `GET /healthz`
- Текстовые сообщения
- Голосовые/аудио сообщения через Telegram file API + OpenAI transcription
- AI-классификация на задачи и вопросы на изучение
- Уточнения в Telegram, если не хватает проекта, срока или уверенность ниже порога
- Создание задач в Notion `Tasks`
- Создание записей в Notion `Study / На изучение`
- Локальное хранение ожидающих уточнений в `data/pending.json`
- Полная двусторонняя синхронизация базы `TASKS` и Todoist

## Быстрый старт

1. Создай `.env` из примера:

```bash
cp .env.example .env
```

2. Заполни токены:

- `TELEGRAM_BOT_TOKEN`
- `OPENAI_API_KEY`
- `NOTION_TOKEN`

3. Запусти локально:

```bash
python3 -m conductor.app
```

4. Для локального теста без Telegram:

```bash
python3 -m conductor.cli "Завтра напомни написать Марко по алюминию. И изучить доступные логистические пути в Веракрус"
```

## Notion базы

Текущие ID уже проставлены в `.env.example`:

- `Tasks`: `be9d26fe652b474696cd5de0118b1210`
- `Study / На изучение`: `4e27e10ca2bf44a08b4c8f86c7a125bd`
- `Projects / Приоритеты`: `bbb501a6933941b4837afff250479f0e`

## Важная логика MVP

- Если срок не указан, Дирижер спрашивает срок.
- Если проект не найден или уверенность ниже `CONFIDENCE_THRESHOLD`, Дирижер спрашивает уточнение.
- Если в сообщении есть и задачи, и вопросы на изучение, создаются обе сущности.
- Исходный RAW отдельно не сохраняется.
- Todoist включается при наличии `TODOIST_API_TOKEN`; аварийная пауза управляется
  только переменной `TODOIST_SYNC_PAUSED`.

## TASKS ↔ Todoist

Новая модель маршрутизации:

- `STREAMS` соответствует родительскому проекту Todoist;
- `PROJECTS` соответствует дочернему проекту Todoist;
- расположение задачи в Todoist определяет `TASKS.Проект`;
- `TASKS.Stream` определяется через связь Project → Stream;
- раздел Todoist записывается прямо в `TASKS.Раздел` и `TASKS.Todoist Section ID`;
- разделы создаются и редактируются в Todoist, отдельной базы Sections нет;
- проектные метки больше не используются для маршрутизации.

Обязательные поля базы Notion: `Task`, `Описание`, `Статус`, `Deadline`, `Срок выполнения`, `Strategic Impact`,
`Source`, `Проект`, `Stream`, `Раздел`, `Todoist Section ID`, `Метки Todoist`, `Todoist ID`,
`Sync status`, `Sync error`, `Sync Notion hash`, `Sync Todoist hash`.

Сопоставление полей:

| Notion `TASKS` | Todoist |
| --- | --- |
| `Task` | название задачи |
| `Описание` | описание задачи |
| `Статус: Done` | задача завершена |
| `Статус: Cancelled` | задача завершена с сохранением истории |
| `Срок выполнения` | due date |
| `Deadline` | deadline |
| `Strategic Impact` | priority |
| `Проект` | проект, в котором находится задача |
| `Stream` | группа проекта; определяется через Project → Stream |
| `Раздел` | название раздела внутри проекта |
| `Todoist Section ID` | стабильная связь с разделом |
| `Метки Todoist` | только разрешённые операционные метки |
| `Todoist ID` | стабильная связь записей |
| `Sync status` | техническое состояние синхронизации |
| `Sync error` | последняя причина ошибки; очищается после успешной сверки |
| `Sync Notion hash` | технический отпечаток последней синхронизированной версии Notion |
| `Sync Todoist hash` | технический отпечаток последней синхронизированной версии Todoist |

Разрешённые управляемые метки: `встреча`, `звонок`, `письмо`, `сообщение`, `документ`,
`анализ`, `исследование`, `планирование`, `низкая_энергия`, `средняя_энергия`,
`высокая_энергия`, `пятиминутное_дело`. Все остальные метки сохраняются без изменений.

По умолчанию синхронизация работает в режиме `observe`: читает обе системы, сохраняет
снимок инвентаря и формирует план миграции, но ничего не записывает. Для записи нужен
режим `write` и отдельное разрешение на каждый рискованный тип операции.

Для запуска:

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

Ручной reconciliation:

```bash
curl -X POST \
  -H "X-Conductor-Sync-Secret: $TASK_SYNC_SECRET" \
  https://YOUR_DOMAIN/tasks/sync
```

Для мгновенного Todoist → Notion обновления зарегистрируй webhook приложения Todoist:

```text
https://YOUR_DOMAIN/todoist/webhook
```

В `TODOIST_WEBHOOK_SECRET` указывается Client Secret приложения Todoist. Без webhook периодический
reconciliation продолжает синхронизировать активные задачи и завершения. Удаления определяются
периодической сверкой после успешной загрузки истории завершённых задач. С webhook эти изменения
попадают в Notion почти сразу.

## Telegram webhook

Для публичного запуска нужен HTTPS URL. После деплоя выстави webhook:

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=https://YOUR_DOMAIN/telegram/webhook"
```

Или через скрипт:

```bash
TELEGRAM_BOT_TOKEN=... PUBLIC_BASE_URL=https://YOUR_DOMAIN sh deploy/set_webhook.sh
```

## Онлайн-запуск

Проект подготовлен для Render через `Dockerfile` и `render.yaml`.

Нужные переменные окружения на хостинге:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET` — любая длинная случайная строка, опционально, но желательно
- `OPENAI_API_KEY`
- `NOTION_TOKEN`
- `NOTION_TASKS_DATABASE_ID`
- `NOTION_STUDY_DATABASE_ID`
- `NOTION_PROJECTS_DATABASE_ID`
- `TODOIST_API_TOKEN` настроен
- `TODOIST_API_TOKEN`
- `TODOIST_WEBHOOK_SECRET`
- `TASK_SYNC_SECRET`

После деплоя надо вызвать Telegram `setWebhook` на публичный URL сервиса.

## Следующие доработки

- Кнопки "Изменить" и пошаговое редактирование параметров.
- OCR для фото и документов.
- Поддержка испанского и английского.
