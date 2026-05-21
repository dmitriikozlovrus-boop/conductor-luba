import unittest
from unittest.mock import Mock, patch

from conductor.models import classification_from_dict, normalize_effort
from conductor.openai_client import OpenAIClient
from conductor.service import ConductorService, _apply_clarification_fallbacks, _resolve_pending_without_ai


class ModelsTest(unittest.TestCase):
    def test_normalize_effort(self):
        self.assertEqual(normalize_effort(5), "5m")
        self.assertEqual(normalize_effort(14), "15m")
        self.assertEqual(normalize_effort(30), "30m")
        self.assertEqual(normalize_effort(60), "1h")
        self.assertEqual(normalize_effort(90), "2h+")

    def test_classification_from_dict(self):
        data = {
            "tasks": [
                {
                    "title": "Позвонить Марко",
                    "description": "Уточнить алюминий",
                    "desired_result": "Есть следующий шаг",
                    "project": "Сырьевой трейдинг",
                    "area": "Бизнес",
                    "due_date": "2026-05-21",
                    "effort_minutes": 15,
                    "priority": "P2",
                    "next_step": "Позвонить",
                    "confidence": 0.9,
                    "missing": [],
                }
            ],
            "studies": [],
            "notes": [],
        }
        result = classification_from_dict(data)
        self.assertEqual(result.tasks[0].title, "Позвонить Марко")
        self.assertEqual(result.tasks[0].effort_minutes, 15)

    def test_fallback_task_title_omits_metadata(self):
        client = OpenAIClient("", "unused", "unused")
        result = client._fallback(
            "Люба, задача: до завтра написать Марко по алюминию по проекту Сырьевой трейдинг, "
            "направление Бизнес. Оценка 15 минут. Желаемый результат: понять следующий шаг.",
            today="2026-05-20",
        )
        self.assertEqual(result.tasks[0].title, "Написать Марко по алюминию")
        self.assertEqual(result.tasks[0].project, "Сырьевой трейдинг")
        self.assertEqual(result.tasks[0].area, "Бизнес")
        self.assertEqual(result.tasks[0].due_date, "2026-05-21")
        self.assertEqual(result.tasks[0].desired_result, "понять следующий шаг")

    def test_fallback_study_question_omits_metadata(self):
        client = OpenAIClient("", "unused", "unused")
        result = client._fallback(
            "Люба, на изучение: до пятницы изучить доступные логистические пути через Веракрус "
            "по проекту Базовые масла, направление Бизнес. Нужна подробная справка.",
            today="2026-05-20",
        )
        self.assertEqual(result.studies[0].question, "Доступные логистические пути через Веракрус")
        self.assertEqual(result.studies[0].project, "Базовые масла")
        self.assertEqual(result.studies[0].research_type, "Глубокое")
        self.assertEqual(result.studies[0].result_format, "Подробная справка")

    def test_fallback_study_defaults_to_simple(self):
        client = OpenAIClient("", "unused", "unused")
        result = client._fallback(
            "Люба, на изучение: до пятницы исследовать рынок пластификаторов по проекту Сырьевой трейдинг, направление Бизнес.",
            today="2026-05-20",
        )
        self.assertEqual(result.studies[0].research_type, "Простое")
        self.assertEqual(result.studies[0].result_format, "Краткая справка")

    def test_postprocess_normalizes_project_name_and_area_from_catalog(self):
        client = OpenAIClient("", "unused", "unused")
        result = client._fallback(
            "Люба, задача: завтра написать Марко по проекту сырьевой трейдинг.",
            today="2026-05-20",
            projects=[{"name": "СЫРЬЕВОЙ ТРЕЙДИНГ", "area": "Бизнес", "status": "Active"}],
        )
        self.assertEqual(result.tasks[0].project, "СЫРЬЕВОЙ ТРЕЙДИНГ")
        self.assertEqual(result.tasks[0].area, "Бизнес")
        self.assertNotIn("project", result.tasks[0].missing)
        self.assertNotIn("area", result.tasks[0].missing)

    def test_postprocess_clears_unknown_project_when_catalog_exists(self):
        client = OpenAIClient("", "unused", "unused")
        result = client._fallback(
            "Люба, задача: завтра написать Марко по проекту неизвестный проект, направление Бизнес.",
            today="2026-05-20",
            projects=[{"name": "СЫРЬЕВОЙ ТРЕЙДИНГ", "area": "Бизнес", "status": "Active"}],
        )
        self.assertIsNone(result.tasks[0].project)
        self.assertIn("project", result.tasks[0].missing)

    def test_clarification_fallback_uses_obshchee_project(self):
        classification = classification_from_dict(
            {
                "tasks": [
                    {
                        "title": "Позвонить",
                        "description": "Позвонить клиенту",
                        "desired_result": "Совершенный звонок",
                        "project": None,
                        "area": None,
                        "due_date": "2026-05-21",
                        "effort_minutes": 15,
                        "priority": "P2",
                        "next_step": "Позвонить клиенту",
                        "confidence": 0.6,
                        "missing": ["project", "area"],
                    }
                ],
                "studies": [],
                "notes": [],
            }
        )
        result = _apply_clarification_fallbacks(classification)
        self.assertEqual(result.tasks[0].project, "Общее")
        self.assertEqual(result.tasks[0].area, "Прочее")
        self.assertEqual(result.tasks[0].missing, [])

    def test_process_audio_reports_transcription_failure(self):
        service = object.__new__(ConductorService)
        service.openai = Mock()
        service.openai.transcribe.side_effect = RuntimeError("insufficient_quota")
        service.telegram = Mock()

        result = service.process_audio("voice.ogg", b"123", content_type="audio/ogg", chat_id=42)

        self.assertEqual(result["tasks_created"], [])
        self.assertEqual(result["studies_created"], [])
        self.assertIn("insufficient_quota", result["errors"][0])
        self.assertEqual(service.telegram.send_message.call_count, 2)
        self.assertIn("Не смогла расшифровать голосовое", service.telegram.send_message.call_args.args[1])

    def test_transcribe_uses_fallback_model(self):
        client = OpenAIClient("key", "unused", "gpt-4o-mini-transcribe", "whisper-1")
        with patch("conductor.openai_client.request_multipart") as request_multipart:
            request_multipart.side_effect = [RuntimeError("primary failed"), {"text": "транскрипт"}]
            result = client.transcribe("voice.ogg", b"123", "audio/ogg")
        self.assertEqual(result, "транскрипт")
        self.assertEqual(request_multipart.call_count, 2)

    def test_pending_task_can_resolve_without_ai(self):
        pending_item = {
            "payload": {
                "type": "task",
                "item": {
                    "title": "Написать Марко",
                    "description": "Написать Марко по алюминию",
                    "desired_result": "Отправленное письмо",
                    "project": "СЫРЬЕВОЙ ТРЕЙДИНГ",
                    "area": "Бизнес",
                    "due_date": None,
                    "effort_minutes": 15,
                    "priority": "P2",
                    "next_step": "Написать Марко",
                    "confidence": 0.6,
                    "missing": ["due_date"],
                },
            },
            "questions": ["Какой срок исполнения?"],
        }
        result = _resolve_pending_without_ai(
            pending_item,
            "Завтра",
            today="2026-05-20",
            projects=[{"name": "СЫРЬЕВОЙ ТРЕЙДИНГ", "area": "Бизнес"}],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.tasks[0].due_date, "2026-05-21")
        self.assertEqual(result.tasks[0].missing, [])
        self.assertGreaterEqual(result.tasks[0].confidence, 0.85)


if __name__ == "__main__":
    unittest.main()
