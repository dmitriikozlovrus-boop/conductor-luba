# Agent Registry

## Назначение

Этот документ фиксирует текущие и кандидатные роли агентов и сервисов в AI OS.

Документ нужен, чтобы пользователь, ИИ-ассистенты, Codex и будущие агенты одинаково понимали:

- какие роли уже существуют;
- какие роли являются только кандидатами;
- за что каждая роль отвечает;
- за что каждая роль не отвечает;
- какие сервисы и документы с ней связаны.

`Agent_Registry.md` не является roadmap.

Наличие роли со статусом `Candidate` не означает, что ее нужно реализовать сейчас.

## Главное правило

Агент — это не одна функция.

```text
Function = одно техническое действие
Logic = правила обработки сущности или сценария
Integration = техническое подключение к внешнему сервису
Agent = роль с ответственностью, правилами, инструментами и границами
Conductor = центральный оркестратор
```

Формула агента:

```text
Agent = Role + Instructions + Tools + Responsibility
```

На текущем этапе первые use cases могут быть реализованы без отдельных специализированных агентов.

Для первых сценариев важнее:

- `Conductor`;
- `Core Logic`;
- `Data Layer`;
- `Integration Layer`;
- `Execution Layer`;
- `Validation`;
- `Error Log`.

## Текущий практический фокус

На ближайшем этапе основное внимание уделяется двум сценариям:

1. правильное раскладывание сущностей по БД с нужным набором данных;
2. двусторонняя интеграция `Tasks DB` с `Todoist`.

Эти сценарии не требуют обязательной реализации `Task Agent` или других специализированных агентов.

Для них достаточно проектировать:

- Entity Classification Logic;
- Field Extraction Logic;
- Routing Rules;
- Task Logic;
- Todoist Integration;
- Sync Rules;
- Conflict Rules;
- Validation Logic;
- Error Log.

## Статусы

```text
Active — роль уже используется или является стабильной частью системы
Candidate — роль потенциально нужна, но пока не обязательна для MVP
Future — возможная будущая роль, не описывается в этом документе
Deprecated — роль больше не используется
```

---

## Lyuba

Статус:  
Active

Тип:  
User Interface Agent

Назначение:  
`Lyuba` — Telegram-бот и один из интерфейсов взаимодействия пользователя с AI OS.

Отвечает за:

- прием пользовательских сообщений;
- прием быстрых задач, идей, событий и уточнений;
- передачу входящего сигнала в `Conductor`;
- возврат результата пользователю;
- отправку уведомлений и подтверждений;
- ручную коррекцию или уточнение спорных случаев.

Не отвечает за:

- центральную маршрутизацию системы;
- самостоятельный выбор всей бизнес-логики;
- хранение источников истины;
- прямое управление всеми БД;
- замену `Conductor`;
- выполнение функций всех будущих агентов.

Связанные сервисы:

- Telegram;
- Telegram Bot API;
- Conductor;
- Notion;
- Todoist;
- Google Calendar;
- Gmail;
- OpenAI / GPT.

Связанные документы:

- `docs/architecture/System_Map.md`;
- `docs/architecture/System_Component_Registry.md`;
- `docs/services/conductor/Conductor_Service_Description.md`;
- `docs/product/use_cases/`.

---

## Conductor

Статус:  
Active / Design

Тип:  
Orchestration Service

Назначение:  
`Conductor` — центральный логический координатор AI OS.

Отвечает за:

- прием входящего сигнала от интерфейсов и источников;
- определение типа входа;
- классификацию информации;
- определение целевой сущности;
- выбор сценария обработки;
- вызов нужного блока `Core Logic`;
- при необходимости вызов `Intelligence Layer`;
- выбор нужной интеграции;
- инициирование записи, обновления, проверки или действия;
- возврат результата в подходящий канал;
- фиксацию ошибок или спорных случаев.

Не отвечает за:

- роль пользовательского интерфейса;
- хранение всех данных;
- выполнение функций внешних API;
- замену специализированных блоков `Core Logic`;
- замену всех будущих агентов;
- самостоятельное изменение архитектуры без правил.

Связанные сервисы:

- Lyuba;
- ChatGPT;
- Notion;
- Todoist;
- Google Calendar;
- Gmail;
- Google Drive;
- OpenAI / GPT;
- будущий backend.

Связанные документы:

- `docs/architecture/System_Map.md`;
- `docs/architecture/System_Component_Registry.md`;
- `docs/services/conductor/Conductor_Service_Description.md`;
- `docs/data/Data_Ownership_Map.md`;
- `docs/product/use_cases/`;
- `docs/architecture/Document_Placement_Rules.md`.

---

## Research Agent

Статус:  
Candidate

Тип:  
Intelligence Agent

Назначение:  
`Research Agent` — кандидатная роль для исследовательских задач, мониторинга, анализа источников и подготовки research outputs.

Отвечает за:

- поиск и анализ информации;
- фильтрацию источников;
- подготовку кратких и развернутых исследовательских выводов;
- подготовку digest / monitoring outputs;
- структурирование результатов исследования;
- выявление практической значимости информации;
- оформление результата в заданном формате.

Не отвечает за:

- центральную маршрутизацию всех входящих сигналов;
- создание задач без решения `Conductor`;
- изменение БД без правил записи;
- автоматическое изменение архитектуры;
- функции календаря;
- функции Todoist sync.

Связанные сервисы:

- OpenAI / GPT;
- web / external sources;
- Google Drive;
- Notion;
- Knowledge;
- Conductor.

Связанные документы:

- `docs/architecture/System_Map.md`;
- `docs/architecture/System_Component_Registry.md`;
- `docs/product/use_cases/`;
- `docs/data/Data_Ownership_Map.md`.

Комментарий:  
На текущем этапе `Research Agent` не является обязательным для первых двух use cases. Его роль может быть уточнена после стабилизации сценариев research и digest.

---

## Task Agent

Статус:  
Candidate

Тип:  
Intelligence Agent / Execution Agent

Назначение:  
`Task Agent` — кандидатная роль для интеллектуальной работы с задачами.

Отвечает за потенциальные функции:

- анализ входящих задач;
- уточнение недостающих полей;
- помощь в определении приоритета;
- оценку длительности;
- предложение следующего действия;
- выявление зависших задач;
- подготовку follow-up;
- помощь в планировании задач.

Не отвечает за:

- базовую синхронизацию `Tasks DB ↔ Todoist`;
- техническое подключение к Todoist API;
- хранение задач как источник истины;
- замену `Task Logic`;
- замену `Conductor`;
- самостоятельное изменение статусов без правил синхронизации.

Связанные сервисы:

- Conductor;
- Task Logic;
- Notion Tasks DB;
- Todoist;
- Todoist API;
- OpenAI / GPT;
- Error Log.

Связанные документы:

- `docs/architecture/System_Map.md`;
- `docs/architecture/System_Component_Registry.md`;
- `docs/data/Data_Ownership_Map.md`;
- `docs/product/use_cases/`;
- будущий `docs/product/use_cases/Tasks_Todoist_Sync.md`.

Комментарий:  
Для use case `Tasks DB ↔ Todoist Sync` основной фокус должен быть на `Task Logic`, `Todoist Integration`, `Sync Rules`, `Conflict Rules` и `Data Ownership`, а не на реализации `Task Agent`.

---

## Calendar Agent

Статус:  
Candidate

Тип:  
Intelligence Agent / Execution Agent

Назначение:  
`Calendar Agent` — кандидатная роль для интеллектуальной работы с календарем, временем, событиями и расписанием.

Отвечает за потенциальные функции:

- анализ календарных событий;
- помощь в выборе времени встречи;
- выявление конфликтов;
- оценку занятости;
- предложение переносов;
- помощь в планировании временных блоков;
- объяснение календарных конфликтов;
- создание follow-up после событий.

Не отвечает за:

- простое техническое создание события через Google Calendar API;
- замену `Event Logic`;
- замену `Google Calendar Integration`;
- общую приоритизацию всех задач;
- центральную маршрутизацию входящих сигналов;
- самостоятельное изменение календаря без правил.

Связанные сервисы:

- Conductor;
- Event Logic;
- Google Calendar;
- Google Calendar API;
- Notion Events DB;
- OpenAI / GPT;
- Error Log.

Связанные документы:

- `docs/architecture/System_Map.md`;
- `docs/architecture/System_Component_Registry.md`;
- `docs/data/Data_Ownership_Map.md`;
- `docs/product/use_cases/`.

Комментарий:  
Calendar integration, calendar logic и Calendar Agent — разные вещи.

```text
Google Calendar API = Integration / Execution
Event Logic = Core Logic
Calendar Agent = Intelligence Agent
```

На текущем этапе `Calendar Agent` не является обязательным для MVP.

---

## Что не включается на текущем этапе

В этот документ пока не включаются:

- Planning Agent;
- Error Agent;
- Idea Agent;
- Problem Agent;
- Digest Agent;
- Priority Agent;
- Memory Agent;
- Document Agent;
- Contact Agent.

Причина: эти роли пока не являются стабильными и не нужны для первых use cases.

Если такие роли понадобятся, они должны добавляться после появления устойчивого сценария и отдельного описания в `docs/product/use_cases/` или `docs/roadmap/`.

## Правила ведения Agent Registry

1. `Agent_Registry.md` не является roadmap.
2. Статус `Candidate` не означает немедленную реализацию.
3. Новая роль добавляется только если у нее есть устойчивая зона ответственности.
4. Функция не должна оформляться как агент.
5. Интеграция не должна оформляться как агент.
6. Блок `Core Logic` не должен автоматически считаться агентом.
7. `Conductor` не должен превращаться в универсального исполнителя всех функций.
8. `Lyuba` не должна подменять `Conductor`.
9. Подробные планы будущих агентов должны храниться в `docs/roadmap/`.
10. Конкретные сценарии работы агентов должны описываться в `docs/product/use_cases/`.

## Связанные документы

| Документ | Назначение |
|---|---|
| `docs/architecture/System_Map.md` | верхнеуровневая логическая карта системы |
| `docs/architecture/System_Component_Registry.md` | перечень компонентов по слоям |
| `docs/architecture/Document_Placement_Rules.md` | правила размещения документов |
| `docs/services/conductor/Conductor_Service_Description.md` | описание Conductor |
| `docs/data/Data_Ownership_Map.md` | источники истины и владение данными |
| `docs/product/use_cases/` | конкретные сценарии обработки |
| `docs/roadmap/` | будущие роли и планы развития |

## Текущий статус

Статус документа: базовая версия.

На текущем этапе документ нужен не для проектирования всех будущих агентов, а для фиксации границ между интерфейсами, оркестратором, интеллектуальными агентами, core logic, интеграциями и execution-сервисами.

Документ может уточняться после реализации первых use cases.
