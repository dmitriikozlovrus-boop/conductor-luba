from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from .http import HttpError, request_json, request_multipart
from .models import Classification, classification_from_dict


CLASSIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "desired_result": {"type": "string"},
                    "project": {"type": ["string", "null"]},
                    "area": {"type": ["string", "null"], "enum": ["Работа", "Бизнес", "Личное развитие", "Семья", "Прочее", None]},
                    "due_date": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD if present"},
                    "effort_minutes": {"type": ["integer", "null"], "minimum": 5},
                    "priority": {"type": "string", "enum": ["P1", "P2", "P3"]},
                    "next_step": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "missing": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "title",
                    "description",
                    "desired_result",
                    "project",
                    "area",
                    "due_date",
                    "effort_minutes",
                    "priority",
                    "next_step",
                    "confidence",
                    "missing",
                ],
            },
        },
        "studies": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question": {"type": "string"},
                    "description": {"type": "string"},
                    "industry": {"type": "string"},
                    "research_type": {"type": "string", "enum": ["Простое", "Глубокое"]},
                    "project": {"type": ["string", "null"]},
                    "area": {"type": ["string", "null"], "enum": ["Работа", "Бизнес", "Личное развитие", "Семья", "Прочее", None]},
                    "priority": {"type": "string", "enum": ["P1", "P2", "P3"]},
                    "result_format": {
                        "type": "string",
                        "enum": ["Краткая справка", "Подробная справка", "Memo", "Таблица", "Telegram-дайджест"],
                    },
                    "due_date": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD if present"},
                    "source": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "missing": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "question",
                    "description",
                    "industry",
                    "research_type",
                    "project",
                    "area",
                    "priority",
                    "result_format",
                    "due_date",
                    "source",
                    "confidence",
                    "missing",
                ],
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tasks", "studies", "notes"],
}


class OpenAIClient:
    def __init__(self, api_key: str, model: str, transcribe_model: str):
        self.api_key = api_key
        self.model = model
        self.transcribe_model = transcribe_model

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def classify(self, text: str, *, projects: list[dict[str, str]], today: str) -> Classification:
        if not self.api_key:
            return self._fallback(text)

        project_lines = "\n".join(
            f"- {p.get('name')} | направление: {p.get('area') or 'не указано'} | статус: {p.get('status') or 'не указано'}"
            for p in projects
        )
        system = (
            "Ты классификатор сервиса 'Дирижер'. Работай строго по ТЗ:\n"
            "- Задача = любое действие кроме простого чтения/изучения.\n"
            "- Вопрос на изучение = чтение, просмотр, анализ информации или справка.\n"
            "- Не мельчи: объединяй близкие действия в одну сущность, если это один смысловой результат.\n"
            "- Если проект неясен, поставь project=null и добавь 'project' в missing.\n"
            "- Если срок не указан, поставь due_date=null и добавь 'due_date' в missing.\n"
            "- Если уверенность по проекту/типу/сроку ниже 0.70, добавь соответствующее поле в missing.\n"
            "- В title задачи не включай проект, направление, срок, оценку времени и желаемый результат; title = только короткое действие.\n"
            "- В question вопроса на изучение не включай проект, направление, срок и формат результата; question = только суть изучения.\n"
            "- Расширяй описание так, чтобы через месяц было понятно, что сделать и зачем.\n"
            "- Даты возвращай ISO YYYY-MM-DD. Сегодня: " + today + ".\n"
            "Направления: Работа, Бизнес, Личное развитие, Семья, Прочее.\n"
            "Существующие проекты:\n" + (project_lines or "- пока нет проектов") + "\n"
        )
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "conductor_classification",
                    "schema": CLASSIFIER_SCHEMA,
                    "strict": True,
                }
            },
        }
        try:
            data = request_json(
                "POST",
                "https://api.openai.com/v1/responses",
                headers={**self.headers, "Content-Type": "application/json"},
                payload=payload,
                timeout=90,
            )
        except HttpError as exc:
            if exc.status in {429, 500, 502, 503, 504}:
                return self._fallback(text, today=today, note=f"fallback classifier after OpenAI HTTP {exc.status}")
            raise
        raw = _extract_response_text(data)
        return classification_from_dict(json.loads(raw))

    def transcribe(self, filename: str, data: bytes, content_type: str = "audio/ogg") -> str:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for voice transcription")
        response = request_multipart(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=self.headers,
            fields={"model": self.transcribe_model, "response_format": "json", "language": "ru"},
            files={"file": (filename, data, content_type)},
            timeout=120,
        )
        return str(response.get("text") or "").strip()

    def _fallback(self, text: str, *, today: str | None = None, note: str = "fallback classifier") -> Classification:
        task_words = ("позвон", "напиш", "напис", "найти", "посчит", "подготов", "договор", "сдел", "отправ")
        study_words = ("изуч", "разобраться в", "исслед", "собрать справ")
        lower = text.lower()
        data: dict[str, Any] = {"tasks": [], "studies": [], "notes": [note]}
        task_text, study_text = _split_task_and_study(text)
        if any(word in lower for word in task_words):
            source = task_text or text
            project = _extract_after(source, r"по проекту\s+([^,.]+)")
            area = _extract_after(source, r"направлени[ея]\s+([^,.]+)")
            due_date = _extract_due_date(source, today)
            effort_minutes = _extract_minutes(source) or 30
            desired_result = _extract_after(source, r"Желаемый результат:\s*([^.\n]+)") or (
                "Понятный выполненный результат по исходному сообщению."
            )
            data["tasks"].append(
                {
                    "title": _clean_title(source, prefixes=("юба, задача:", "люба, задача:", "задача:"), kind="task"),
                    "description": source,
                    "desired_result": desired_result,
                    "project": project,
                    "area": _normalize_area(area),
                    "due_date": due_date,
                    "effort_minutes": effort_minutes,
                    "priority": "P2",
                    "next_step": _first_sentence(source),
                    "confidence": 0.75 if project and due_date else 0.45,
                    "missing": _missing(project=project, area=_normalize_area(area), due_date=due_date),
                }
            )
        if study_text or any(word in lower for word in study_words):
            source = study_text or text
            project = _extract_after(source, r"по проекту\s+([^,.]+)")
            area = _extract_after(source, r"направлени[ея]\s+([^,.]+)")
            due_date = _extract_due_date(source, today)
            data["studies"].append(
                {
                    "question": _clean_title(source, prefixes=("и на изучение:", "на изучение:"), kind="study"),
                    "description": source,
                    "industry": _guess_industry(source),
                    "research_type": "Глубокое" if "подроб" in source.lower() or "глубок" in source.lower() else "Простое",
                    "project": project,
                    "area": _normalize_area(area),
                    "priority": "P2",
                    "result_format": "Подробная справка" if "подроб" in source.lower() else "Краткая справка",
                    "due_date": due_date,
                    "source": "Telegram",
                    "confidence": 0.75 if project and due_date else 0.45,
                    "missing": _missing(project=project, area=_normalize_area(area), due_date=due_date),
                }
            )
        return classification_from_dict(data)


def _split_task_and_study(text: str) -> tuple[str, str]:
    marker = re.search(r"\b(?:и\s+)?на изучение\s*:", text, flags=re.IGNORECASE)
    if not marker:
        return text, ""
    return text[: marker.start()].strip(), text[marker.start() :].strip()


def _extract_after(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def _extract_minutes(text: str) -> int | None:
    match = re.search(r"(\d+)\s*(?:минут|мин|м\b)", text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _extract_due_date(text: str, today: str | None) -> str | None:
    if not today:
        return None
    base = date.fromisoformat(today)
    lower = text.lower()
    if "послезавтра" in lower:
        return (base + timedelta(days=2)).isoformat()
    if "завтра" in lower:
        return (base + timedelta(days=1)).isoformat()
    weekdays = {
        "понедельник": 0,
        "вторник": 1,
        "сред": 2,
        "четверг": 3,
        "пятниц": 4,
        "суббот": 5,
        "воскрес": 6,
    }
    for word, weekday in weekdays.items():
        if word in lower:
            delta = (weekday - base.weekday()) % 7
            return (base + timedelta(days=delta or 7)).isoformat()
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    return match.group(1) if match else None


def _normalize_area(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().capitalize()
    aliases = {"Личное": "Личное развитие"}
    return aliases.get(cleaned, cleaned)


def _missing(*, project: str | None, area: str | None, due_date: str | None) -> list[str]:
    missing = []
    if not project:
        missing.append("project")
    if not area:
        missing.append("area")
    if not due_date:
        missing.append("due_date")
    return missing


def _clean_title(text: str, *, prefixes: tuple[str, ...], kind: str = "generic") -> str:
    value = text.strip()
    lower = value.lower()
    for prefix in prefixes:
        if lower.startswith(prefix):
            value = value[len(prefix) :].strip()
            break
    value = _strip_metadata_from_title(value, kind=kind)
    return _first_sentence(value)[:120]


def _strip_metadata_from_title(text: str, *, kind: str) -> str:
    value = text.strip()
    metadata_patterns = [
        r"\s+по проекту\s+[^,.]+",
        r"\s+направлени[ея]\s+[^,.]+",
        r"\s+оценка\s+\d+\s*(?:минут|мин|м\b|час[а-я]*)",
        r"\s+желаемый результат\s*:\s*.+$",
        r"\s+нужна\s+.+(?:справка|таблица|memo|дайджест).*$",
    ]
    for pattern in metadata_patterns:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)
    if kind == "task":
        value = re.sub(r"^(?:до\s+\S+\s+)", "", value, flags=re.IGNORECASE)
    if kind == "study":
        value = re.sub(r"^(?:до\s+\S+\s+)", "", value, flags=re.IGNORECASE)
    return value.strip(" .,\n\t")


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0][:200]


def _guess_industry(text: str) -> str:
    lower = text.lower()
    if "логист" in lower or "веракрус" in lower:
        return "Логистика"
    if "алюмин" in lower or "сыр" in lower:
        return "Сырьевые товары"
    if "ai" in lower or "ии" in lower:
        return "AI"
    return "Не определено"


def _extract_response_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts: list[str] = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") in {"output_text", "text"} and "text" in content:
                parts.append(content["text"])
    if parts:
        return "".join(parts)
    raise RuntimeError(f"Could not extract text from OpenAI response: {data}")
