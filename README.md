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
- Todoist включается переменной `TODOIST_ENABLED=true`.

## TASKS ↔ Todoist

После включения сервис каждые `TODOIST_SYNC_INTERVAL_SECONDS` выполняет reconciliation:

- новая активная задача Notion создаётся в Todoist;
- новая активная задача Todoist создаётся в Notion;
- импортируются активные задачи и завершённые задачи Todoist за последние 7 дней;
- завершённые и отменённые задачи Notion создаются в Todoist и сразу переносятся в завершённые;
- изменения названия, срока, дедлайна и приоритета передаются в обе стороны;
- `Done` в Notion завершает задачу Todoist;
- `Cancelled` в Notion завершает задачу Todoist, сохраняя историю;
- завершение, восстановление и удаление из Todoist передаются в Notion через webhook;
- конфликты разрешаются по времени последнего изменения;
- локальные отпечатки не позволяют изменениям циклически ходить между системами.

Обязательные поля базы Notion: `Task`, `Статус`, `Deadline`, `Срок выполнения`, `Strategic Impact`,
`Source`, `Todoist ID`, `Sync status`.

Сопоставление полей:

| Notion `TASKS` | Todoist |
| --- | --- |
| `Task` | название задачи |
| `Статус: Done` | задача завершена |
| `Статус: Cancelled` | задача завершена с сохранением истории |
| `Срок выполнения` | due date |
| `Deadline` | deadline |
| `Strategic Impact` | priority |
| `Проект` | единственная управляемая метка задачи |
| `Todoist ID` | стабильная связь записей |
| `Sync status` | техническое состояние синхронизации |

Notion является единственным источником истины для проектов:

- каждая запись базы `PROJECTS` автоматически получает одноимённую метку в Todoist;
- поле `TASKS.Проект` определяет управляемую метку задачи Todoist;
- изменение или добавление меток в Todoist не создаёт проекты и не меняет `TASKS.Проект`;
- при синхронизации метки задачи Todoist приводятся к проекту, указанному в Notion.

Лишние метки не удаляются из общего каталога Todoist автоматически, чтобы не уничтожать пользовательские
данные, но они не влияют на Notion и снимаются с синхронизируемых активных задач.

Поля Notion без прямого аналога Todoist (`Stream`, `Context`, `Энергия`, `Importance`, `Source`) остаются
источником истины в Notion и не удаляются при обратной синхронизации.

Для запуска:

```bash
TODOIST_ENABLED=true
TODOIST_API_TOKEN=...
TODOIST_WEBHOOK_SECRET=...
TASK_SYNC_SECRET=...
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
reconciliation продолжает синхронизировать активные задачи, но завершения и удаления Todoist попадут
в Notion только после подключения webhook.

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
- `TODOIST_ENABLED=true`
- `TODOIST_API_TOKEN`
- `TODOIST_WEBHOOK_SECRET`
- `TASK_SYNC_SECRET`

После деплоя надо вызвать Telegram `setWebhook` на публичный URL сервиса.

## Следующие доработки

- Кнопки "Изменить" и пошаговое редактирование параметров.
- OCR для фото и документов.
- Поддержка испанского и английского.
