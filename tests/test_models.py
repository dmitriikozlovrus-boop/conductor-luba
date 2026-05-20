import unittest

from conductor.models import classification_from_dict, normalize_effort
from conductor.openai_client import OpenAIClient


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
        self.assertEqual(result.tasks[0].title, "написать Марко по алюминию")
        self.assertEqual(result.tasks[0].project, "Сырьевой трейдинг")
        self.assertEqual(result.tasks[0].area, "Бизнес")
        self.assertEqual(result.tasks[0].due_date, "2026-05-21")

    def test_fallback_study_question_omits_metadata(self):
        client = OpenAIClient("", "unused", "unused")
        result = client._fallback(
            "Люба, на изучение: до пятницы изучить доступные логистические пути через Веракрус "
            "по проекту Базовые масла, направление Бизнес. Нужна подробная справка.",
            today="2026-05-20",
        )
        self.assertEqual(result.studies[0].question, "изучить доступные логистические пути через Веракрус")
        self.assertEqual(result.studies[0].project, "Базовые масла")


if __name__ == "__main__":
    unittest.main()
