from __future__ import annotations

from datetime import date
from typing import Any

from .config import Settings
from .models import Classification, StudyItem, TaskItem
from .notion_client import NotionClient
from .openai_client import OpenAIClient, _extract_due_date, _normalize_area
from .pending import PendingStore
from .recent import RecentStore
from .telegram import TelegramClient
from .todoist_client import TodoistClient
from .task_sync import TaskSyncService


class ConductorService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.openai = OpenAIClient(
            settings.openai_api_key,
            settings.openai_model,
            settings.openai_transcribe_model,
            settings.openai_transcribe_fallback_model,
        )
        self.notion = NotionClient(
            settings.notion_token,
            settings.notion_tasks_database_id,
            settings.notion_study_database_id,
            settings.notion_projects_database_id,
        )
        self.telegram = TelegramClient(settings.telegram_bot_token)
        # A configured token is enough to enable the client. The dedicated
        # TODOIST_SYNC_PAUSED flag is the single operational kill switch.
        self.todoist = TodoistClient(settings.todoist_api_token, settings.todoist_enabled or bool(settings.todoist_api_token))
        self.task_sync = TaskSyncService(
            settings.notion_token,
            settings.notion_tasks_database_id,
            settings.notion_projects_database_id,
            self.todoist,
            settings.todoist_sync_state_path,
            settings.todoist_completed_since,
            settings.notion_streams_database_id,
            paused=settings.todoist_sync_paused,
            mode=settings.todoist_sync_mode,
            allow_project_create=settings.todoist_allow_project_create,
            allow_task_create=settings.todoist_allow_task_create,
            allow_task_move=settings.todoist_allow_task_move,
            allow_label_write=settings.todoist_allow_label_write,
            allow_status_write=settings.todoist_allow_status_write,
            allow_missing_cancel=settings.todoist_allow_missing_cancel,
            max_task_moves=settings.todoist_max_task_moves,
            snapshot_path=settings.todoist_snapshot_path,
        )
        self.pending = PendingStore(settings.pending_store_path)
        self.recent = RecentStore(settings.recent_store_path)

    def process_text(self, text: str, *, chat_id: int | None = None, source: str = "Telegram") -> dict[str, Any]:
        pending_item: dict[str, Any] | None = None
        if chat_id is not None:
            pending = self.pending.pop_oldest_for_chat(chat_id)
            if pending:
                _, pending_item = pending
        if chat_id is not None and not pending_item and _looks_like_edit_request(text):
            return self._handle_edit_request(text, chat_id=chat_id)
        try:
            projects = self.notion.list_projects()
        except Exception as exc:  # noqa: BLE001 - missing project context should not break capture.
            projects = []
            print(f"Could not load Notion projects: {exc}", flush=True)
        if pending_item:
            resolved = _resolve_pending_without_ai(pending_item, text, today=date.today().isoformat(), projects=projects)
            if resolved:
                return self._handle_classification(
                    resolved,
                    chat_id=chat_id,
                    source=source,
                    from_clarification=True,
                )
            text = _merge_pending_text(pending_item, text)
        try:
            classification = self.openai.classify(text, projects=projects, today=date.today().isoformat())
        except Exception as exc:  # noqa: BLE001 - notify the user instead of returning a webhook 502.
            if chat_id is not None:
                self.telegram.send_message(chat_id, f"Не смог разобрать сообщение через AI: {exc}")
            return {"tasks_created": [], "studies_created": [], "pending": 0, "errors": [str(exc)], "notes": []}
        if pending_item:
            classification = _apply_clarification_fallbacks(classification)
        return self._handle_classification(
            classification,
            chat_id=chat_id,
            source=source,
            from_clarification=bool(pending_item),
        )

    def process_audio(
        self,
        filename: str,
        data: bytes,
        *,
        content_type: str,
        chat_id: int | None = None,
        source: str = "Telegram voice",
    ) -> dict[str, Any]:
        try:
            text = self.openai.transcribe(filename, data, content_type)
        except Exception as exc:  # noqa: BLE001 - voice failures should be visible to the user.
            if chat_id is not None:
                self.telegram.send_message(
                    chat_id,
                    f"Не смогла расшифровать голосовое: {exc}. Пока можно прислать текстом, а я продолжу разбор.",
                )
            return {"tasks_created": [], "studies_created": [], "pending": 0, "errors": [str(exc)], "notes": []}
        result = self.process_text(text, chat_id=chat_id, source=source)
        result["transcript"] = text
        return result

    def _handle_edit_request(self, text: str, *, chat_id: int) -> dict[str, Any]:
        recent = self.recent.get(chat_id)
        if not recent:
            self.telegram.send_message(chat_id, _edit_guidance_message())
            return {"tasks_created": [], "studies_created": [], "pending": 0, "errors": [], "notes": ["edit guidance sent"]}
        try:
            projects = self.notion.list_projects()
        except Exception as exc:  # noqa: BLE001
            projects = []
            print(f"Could not load Notion projects: {exc}", flush=True)

        updated = _apply_edit_to_recent(recent, text, today=date.today().isoformat(), projects=projects)
        if not updated:
            self.telegram.send_message(chat_id, _edit_guidance_message())
            return {"tasks_created": [], "studies_created": [], "pending": 0, "errors": [], "notes": ["edit guidance sent"]}

        try:
            if updated["type"] == "task":
                item = TaskItem(**updated["item"])
                self.notion.update_task(updated["page_id"], item)
                classification = Classification(tasks=[item], studies=[], notes=["edited recent task"])
            else:
                item = StudyItem(**updated["item"])
                self.notion.update_study(updated["page_id"], item)
                classification = Classification(tasks=[], studies=[item], notes=["edited recent study"])
        except Exception as exc:  # noqa: BLE001
            self.telegram.send_message(chat_id, f"Не смогла обновить запись: {exc}")
            return {"tasks_created": [], "studies_created": [], "pending": 0, "errors": [str(exc)], "notes": ["edit failed"]}
        self.recent.save(chat_id, updated)
        self.telegram.send_message(chat_id, _format_updated_summary(classification))
        return {"tasks_created": [], "studies_created": [], "pending": 0, "errors": [], "notes": classification.notes}

    def _handle_classification(
        self,
        classification: Classification,
        *,
        chat_id: int | None,
        source: str,
        from_clarification: bool = False,
    ) -> dict[str, Any]:
        created_tasks: list[str] = []
        created_studies: list[str] = []
        pending_count = 0
        errors: list[str] = []

        for item in classification.tasks:
            questions = self._task_questions(item)
            if questions and chat_id is not None:
                self.pending.add(chat_id, {"type": "task", "item": item.__dict__}, questions)
                pending_count += 1
                self.telegram.send_message(chat_id, _format_questions(item.title, questions))
                continue
            try:
                url = self.notion.create_task(item, source=source)
                created_tasks.append(url)
                if chat_id is not None:
                    self.recent.save(chat_id, _recent_payload("task", url, item.__dict__))
            except Exception as exc:  # noqa: BLE001 - notify user rather than hide automation failures.
                errors.append(f"Не удалось создать задачу '{item.title}': {exc}")

        for item in classification.studies:
            questions = self._study_questions(item)
            if questions and chat_id is not None:
                self.pending.add(chat_id, {"type": "study", "item": item.__dict__}, questions)
                pending_count += 1
                self.telegram.send_message(chat_id, _format_questions(item.question, questions))
                continue
            try:
                url = self.notion.create_study(item)
                created_studies.append(url)
                if chat_id is not None:
                    self.recent.save(chat_id, _recent_payload("study", url, item.__dict__))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Не удалось создать вопрос на изучение '{item.question}': {exc}")

        if chat_id is not None and errors:
            self.telegram.send_message(chat_id, "\n".join(errors))
        if chat_id is not None and (created_tasks or created_studies):
            self.telegram.send_message(chat_id, _format_created_summary(classification, from_clarification=from_clarification))
        return {
            "tasks_created": created_tasks,
            "studies_created": created_studies,
            "pending": pending_count,
            "errors": errors,
            "notes": classification.notes,
        }

    def _task_questions(self, item: TaskItem) -> list[str]:
        questions: list[str] = []
        if item.confidence < self.settings.confidence_threshold:
            questions.append(f"Уверенность {item.confidence:.0%}. Подтверди, что это задача.")
        if "project" in item.missing or not item.project:
            questions.append("К какому проекту отнести?")
        if "due_date" in item.missing or not item.due_date:
            questions.append("Какой срок исполнения?")
        if "area" in item.missing or not item.area:
            questions.append("Какое направление: Работа, Бизнес, Личное развитие, Семья или Прочее?")
        return questions

    def _study_questions(self, item: StudyItem) -> list[str]:
        questions: list[str] = []
        if item.confidence < self.settings.confidence_threshold:
            questions.append(f"Уверенность {item.confidence:.0%}. Подтверди, что это вопрос на изучение.")
        if "project" in item.missing or not item.project:
            questions.append("К какому проекту отнести?")
        if "due_date" in item.missing or not item.due_date:
            questions.append("Какой срок/горизонт изучения?")
        if "area" in item.missing or not item.area:
            questions.append("Какое направление: Работа, Бизнес, Личное развитие, Семья или Прочее?")
        if _needs_study_questions(item):
            questions.append("Какие именно вопросы должны войти в исследование?")
        return questions


def _format_questions(title: str, questions: list[str]) -> str:
    joined = "\n".join(f"- {q}" for q in questions)
    return f"Нужно уточнение по записи:\n{title}\n\n{joined}\n\nОтветь одним сообщением, я сохраню это как уточнение для следующего шага."


def _edit_guidance_message() -> str:
    return (
        "Не поняла, что именно нужно поправить.\n\n"
        "Можно написать так:\n"
        "- Исправь срок на пятницу\n"
        "- Исправь проект на СЫРЬЕВОЙ ТРЕЙДИНГ\n"
        "- Исправь направление на Бизнес\n"
        "- Исправь длительность на 15 минут\n"
        "- Исправь название на Поздравить с днем рождения Марии"
    )


def _format_created_summary(classification: Classification, *, from_clarification: bool = False) -> str:
    lines: list[str] = []
    if from_clarification:
        lines.append("Зафиксировала после уточнения:")
    for item in classification.tasks:
        lines.extend(
            [
                f"Добавила задачу: {item.title}",
                f"Направление: {item.area or 'Не указано'}",
                f"Проект: {item.project or 'Не указано'}",
                f"Дата исполнения: {item.due_date or 'Не указана'}",
                f"Длительность работы: {item.effort_minutes} минут" if item.effort_minutes else "Длительность работы: Не указана",
            ]
        )
    for item in classification.studies:
        lines.extend(
            [
                f"Добавила на изучение: {item.question}",
                f"Направление: {item.area or 'Не указано'}",
                f"Проект: {item.project or 'Не указано'}",
                f"Дата исполнения: {item.due_date or 'Не указана'}",
                f"Тип исследования: {item.research_type}",
                f"Формат результата: {item.result_format}",
            ]
        )
    lines.append("Если что-то не так, напиши одним сообщением, что изменить.")
    return "\n".join(lines)


def _format_updated_summary(classification: Classification) -> str:
    lines = ["Обновила запись:"]
    for item in classification.tasks:
        lines.extend(
            [
                f"Задача: {item.title}",
                f"Направление: {item.area or 'Не указано'}",
                f"Проект: {item.project or 'Не указано'}",
                f"Дата исполнения: {item.due_date or 'Не указана'}",
                f"Длительность работы: {item.effort_minutes} минут" if item.effort_minutes else "Длительность работы: Не указана",
            ]
        )
    for item in classification.studies:
        lines.extend(
            [
                f"На изучение: {item.question}",
                f"Направление: {item.area or 'Не указано'}",
                f"Проект: {item.project or 'Не указано'}",
                f"Дата исполнения: {item.due_date or 'Не указана'}",
                f"Тип исследования: {item.research_type}",
                f"Формат результата: {item.result_format}",
            ]
        )
    return "\n".join(lines)


def _apply_clarification_fallbacks(classification: Classification) -> Classification:
    for item in classification.tasks:
        if not item.project:
            item.project = "Общее"
        if "project" in item.missing:
            item.missing = [value for value in item.missing if value != "project"]
        if not item.area:
            item.area = "Прочее"
        if "area" in item.missing:
            item.missing = [value for value in item.missing if value != "area"]
    for item in classification.studies:
        if not item.project:
            item.project = "Общее"
        if "project" in item.missing:
            item.missing = [value for value in item.missing if value != "project"]
        if not item.area:
            item.area = "Прочее"
        if "area" in item.missing:
            item.missing = [value for value in item.missing if value != "area"]
    return classification


def _needs_study_questions(item: StudyItem) -> bool:
    description = item.description.lower()
    scope_markers = ("какие", "что", "сравн", "риск", "стоим", "марж", "услов", "этап", "срок", "вопрос")
    return not any(marker in description for marker in scope_markers)


def _merge_pending_text(pending_item: dict[str, Any], answer: str) -> str:
    payload = pending_item.get("payload", {})
    item = payload.get("item", {})
    questions = "\n".join(pending_item.get("questions", []))
    return (
        "Есть черновик записи Дирижера, который раньше не был сохранен из-за недостающих данных.\n"
        f"Тип черновика: {payload.get('type')}\n"
        f"Черновик: {item}\n"
        f"Какие уточнения были запрошены: {questions}\n"
        f"Ответ пользователя: {answer}\n"
        "Собери финальную запись. Если теперь данных хватает, confidence должен быть >= 0.70 и missing пустой."
    )


def _resolve_pending_without_ai(
    pending_item: dict[str, Any],
    answer: str,
    *,
    today: str,
    projects: list[dict[str, str]],
) -> Classification | None:
    payload = pending_item.get("payload", {})
    item_type = payload.get("type")
    raw_item = payload.get("item", {})
    if item_type not in {"task", "study"} or not raw_item:
        return None

    item = dict(raw_item)
    project_name = _extract_project_from_answer(answer, projects)
    if project_name:
        item["project"] = project_name

    area = _extract_area_from_answer(answer)
    if area:
        item["area"] = area

    due_date = _extract_due_date(answer, today)
    if due_date:
        item["due_date"] = due_date

    if item_type == "study" and "Какие именно вопросы должны войти в исследование?" in "\n".join(
        pending_item.get("questions", [])
    ):
        item["description"] = f"{item.get('description', '').strip()}\n\nУточнение пользователя: {answer.strip()}".strip()

    missing = list(item.get("missing", []))
    if item.get("project"):
        missing = [value for value in missing if value != "project"]
    if item.get("area"):
        missing = [value for value in missing if value != "area"]
    if item.get("due_date"):
        missing = [value for value in missing if value != "due_date"]
    item["missing"] = missing

    if missing:
        return None

    item["confidence"] = max(float(item.get("confidence") or 0.0), 0.85)
    if item_type == "task":
        return Classification(tasks=[TaskItem(**item)], studies=[], notes=["resolved pending clarification"])
    return Classification(tasks=[], studies=[StudyItem(**item)], notes=["resolved pending clarification"])


def _extract_project_from_answer(answer: str, projects: list[dict[str, str]]) -> str | None:
    answer_lower = answer.casefold()
    for project in projects:
        name = str(project.get("name") or "").strip()
        if name and name.casefold() in answer_lower:
            return name
    return None


def _extract_area_from_answer(answer: str) -> str | None:
    answer_lower = answer.casefold()
    for area in ("Работа", "Бизнес", "Личное развитие", "Семья", "Прочее"):
        if area.casefold() in answer_lower:
            return area
    if answer_lower.strip() == "личное":
        return "Личное развитие"
    normalized = _normalize_area(answer.strip())
    return normalized if normalized in {"Работа", "Бизнес", "Личное развитие", "Семья", "Прочее"} else None


def _looks_like_edit_request(text: str) -> bool:
    lower = text.strip().casefold()
    return lower.startswith(("поправь", "исправь", "измени", "не так", "неправильно"))


def _recent_payload(item_type: str, url: str, item: dict[str, Any]) -> dict[str, Any]:
    return {"type": item_type, "url": url, "page_id": _extract_notion_page_id(url), "item": item}


def _extract_notion_page_id(url: str) -> str:
    raw = url.rstrip("/").split("-")[-1].split("?")[0]
    if len(raw) == 32:
        return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
    return raw


def _apply_edit_to_recent(
    recent: dict[str, Any],
    text: str,
    *,
    today: str,
    projects: list[dict[str, str]],
) -> dict[str, Any] | None:
    item_type = recent.get("type")
    if item_type not in {"task", "study"}:
        return None
    updated = {
        "type": recent["type"],
        "url": recent["url"],
        "page_id": recent["page_id"],
        "item": dict(recent["item"]),
    }
    item = updated["item"]
    changed = False

    title = _extract_replacement_value(text, ("название", "задачу", "задача", "вопрос", "на изучение"))
    if title:
        key = "title" if item_type == "task" else "question"
        item[key] = title
        changed = True

    project_name = _extract_project_from_answer(text, projects)
    if project_name:
        item["project"] = project_name
        changed = True

    area = _extract_area_from_answer(text)
    if area:
        item["area"] = area
        changed = True

    due_date = _extract_due_date(text, today)
    if due_date:
        item["due_date"] = due_date
        changed = True

    effort = _extract_effort_from_answer(text)
    if effort is not None and item_type == "task":
        item["effort_minutes"] = effort
        changed = True

    if item_type == "study":
        research_type = _extract_research_type(text)
        if research_type:
            item["research_type"] = research_type
            item["result_format"] = "Подробная справка" if research_type == "Глубокое" else "Краткая справка"
            changed = True

    if item_type == "task" and _looks_like_birthday_correction(text):
        current_title = str(item.get("title") or "")
        current_title = current_title.replace("Напомнить", "Поздравить").replace("напомнить", "Поздравить")
        current_title = current_title.replace("Напомни", "Поздравить").replace("напомни", "Поздравить")
        current_title = current_title.replace("о дне рождения", "с днем рождения")
        item["title"] = current_title
        item["desired_result"] = "Совершенное поздравление"
        changed = True

    if not changed:
        return None
    return updated


def _extract_replacement_value(text: str, markers: tuple[str, ...]) -> str | None:
    lower = text.casefold()
    for marker in markers:
        for pattern in (f"{marker} на ", f"{marker}:"):
            index = lower.find(pattern)
            if index != -1:
                return text[index + len(pattern) :].strip(" .:\n\t")
    return None


def _extract_effort_from_answer(text: str) -> int | None:
    import re

    lower = text.casefold()
    match = re.search(r"(\d+)\s*(час|часа|часов)", lower)
    if match:
        return int(match.group(1)) * 60
    match = re.search(r"(\d+)\s*(минут|мин|м\b)", lower)
    if match:
        return int(match.group(1))
    return None


def _extract_research_type(text: str) -> str | None:
    lower = text.casefold()
    if "глубок" in lower or "подроб" in lower:
        return "Глубокое"
    if "прост" in lower or "кратк" in lower:
        return "Простое"
    return None


def _looks_like_birthday_correction(text: str) -> bool:
    lower = text.casefold()
    return "поздрав" in lower and "рожд" in lower
