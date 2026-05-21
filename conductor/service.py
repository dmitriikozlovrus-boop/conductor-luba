from __future__ import annotations

from datetime import date
from typing import Any

from .config import Settings
from .models import Classification, StudyItem, TaskItem
from .notion_client import NotionClient
from .openai_client import OpenAIClient, _extract_due_date, _normalize_area
from .pending import PendingStore
from .telegram import TelegramClient
from .todoist_client import TodoistClient


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
        self.todoist = TodoistClient(settings.todoist_api_token, settings.todoist_enabled)
        self.pending = PendingStore(settings.pending_store_path)

    def process_text(self, text: str, *, chat_id: int | None = None, source: str = "Telegram") -> dict[str, Any]:
        pending_item: dict[str, Any] | None = None
        if chat_id is not None:
            pending = self.pending.pop_oldest_for_chat(chat_id)
            if pending:
                _, pending_item = pending
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
        if chat_id is not None:
            self.telegram.send_message(chat_id, "Приняла голосовое. Расшифровываю и раскладываю по базам.")
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
                self.todoist.create_task(item)
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
                created_studies.append(self.notion.create_study(item))
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


def _format_created_summary(classification: Classification, *, from_clarification: bool = False) -> str:
    lines: list[str] = []
    if from_clarification:
        lines.append("Зафиксировала после уточнения:")
    for item in classification.tasks:
        due = item.due_date or "без срока"
        effort = f"{item.effort_minutes} мин" if item.effort_minutes else "без оценки"
        lines.append(f"Добавила задачу: {item.title} ({due}, {effort})")
    for item in classification.studies:
        lines.append(
            f"Добавила на изучение: {item.question} ({item.research_type.lower()}, {item.result_format.lower()})"
        )
    lines.append("Если что-то не так, напиши одним сообщением, что изменить.")
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
