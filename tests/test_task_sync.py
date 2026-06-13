import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from conductor.http import HttpError
from conductor.task_sync import (
    SyncResult,
    TaskSyncService,
    _fingerprint,
    _match_key_notion,
    _match_key_todoist,
    _notion_properties_from_todoist,
    _notion_routing_from_todoist,
    _parse_time,
    _project_stream_section,
    _priority_to_strategic,
    _retry_after,
    _strategic_to_priority,
    _todoist_routing_differs,
)
from conductor.todoist_client import TodoistClient, _task_payload, todoist_priority


class TodoistMappingTest(unittest.TestCase):
    def test_priority_mapping_round_trip(self):
        self.assertEqual(todoist_priority(1), "P1")
        self.assertEqual(todoist_priority(4), "P4")
        self.assertEqual(_strategic_to_priority("9"), "P1")
        self.assertEqual(_strategic_to_priority("7"), "P2")
        self.assertEqual(_priority_to_strategic("P3"), "5")

    def test_todoist_payload_contains_dates(self):
        payload = _task_payload(
            {
                "title": "Подготовить письмо",
                "description": "Черновик",
                "priority": "P1",
                "due_date": "2026-06-13",
                "deadline": "2026-06-15",
            }
        )
        self.assertEqual(payload["content"], "Подготовить письмо")
        self.assertEqual(payload["priority"], 1)
        self.assertEqual(payload["due_date"], "2026-06-13")
        self.assertEqual(payload["deadline_date"], "2026-06-15")

    def test_todoist_payload_uses_fallback_for_empty_title(self):
        self.assertEqual(_task_payload({"title": ""})["content"], "Без названия")

    def test_parse_time_handles_iso_and_empty_values(self):
        self.assertEqual(_parse_time("2026-06-12T10:00:00Z"), datetime(2026, 6, 12, 10, tzinfo=timezone.utc))
        self.assertEqual(_parse_time(None), datetime.min.replace(tzinfo=timezone.utc))

    def test_move_to_section_does_not_also_send_project(self):
        client = TodoistClient("token", True)
        with unittest.mock.patch("conductor.todoist_client.request_json") as request:
            request.return_value = {"sync_status": {"command-1": "ok"}}
            with unittest.mock.patch("conductor.todoist_client.uuid4", return_value="command-1"):
                client.update_task_location("task-1", "inbox-1", "section-1")
        args = request.call_args.kwargs["payload"]["commands"][0]["args"]
        self.assertEqual(args, {"id": "task-1", "section_id": "section-1"})

    def test_notion_project_becomes_todoist_label(self):
        payload = _task_payload(
            {
                "title": "Подготовить письмо",
                "description": "",
                "priority": "P2",
                "project_name": "AI DESIGN SYSTEM",
            }
        )
        self.assertEqual(payload["labels"], ["AI DESIGN SYSTEM"])

    def test_todoist_task_does_not_set_notion_project(self):
        properties = _notion_properties_from_todoist(
            {
                "content": "Подготовить письмо",
                "priority": 3,
                "labels": ["AI DESIGN SYSTEM"],
                "due": {"date": "2026-06-13"},
                "deadline": {"date": "2026-06-15"},
                "is_completed": False,
            }
        )
        self.assertIn("Task", properties)
        self.assertIn("Статус", properties)
        self.assertIn("Срок выполнения", properties)
        self.assertIn("Deadline", properties)
        self.assertNotIn("Проект", properties)

    def test_todoist_description_is_written_to_notion(self):
        properties = _notion_properties_from_todoist({"content": "Task", "description": "Details"})
        self.assertEqual(properties["Описание"]["rich_text"][0]["text"]["content"], "Details")

    def test_active_notion_status_is_preserved_by_active_todoist_task(self):
        properties = _notion_properties_from_todoist(
            {"content": "Task", "is_completed": False},
            current_status="In Progress",
        )
        self.assertEqual(properties["Статус"], {"status": {"name": "In Progress"}})

    def test_todoist_section_and_label_become_notion_routing(self):
        properties = _notion_routing_from_todoist(
            {"labels": ["Project A"], "section_id": "section-work"},
            {"project a": {"id": "project-1", "name": "Project A"}},
            {"работа": {"id": "stream-work", "name": "РАБОТА"}},
            {"работа": "section-work", "прочее": "section-other"},
        )
        self.assertEqual(properties["Проект"], {"relation": [{"id": "project-1"}]})
        self.assertEqual(properties["Stream"], {"relation": [{"id": "stream-work"}]})

    def test_todoist_other_section_and_no_label_clear_notion_routing(self):
        properties = _notion_routing_from_todoist(
            {"labels": [], "section_id": "section-other"},
            {"project a": {"id": "project-1", "name": "Project A"}},
            {"работа": {"id": "stream-work", "name": "РАБОТА"}},
            {"работа": "section-work", "прочее": "section-other"},
        )
        self.assertEqual(properties["Проект"], {"relation": []})
        self.assertEqual(properties["Stream"], {"relation": []})

    def test_project_label_supplies_stream_when_task_is_in_other(self):
        properties = _notion_routing_from_todoist(
            {"labels": ["Project A"], "section_id": "section-other"},
            {"project a": {"id": "project-1", "name": "Project A", "stream_id": "stream-business"}},
            {"бизнес": {"id": "stream-business", "name": "БИЗНЕС"}},
            {"бизнес": "section-business", "прочее": "section-other"},
        )
        self.assertEqual(properties["Проект"], {"relation": [{"id": "project-1"}]})
        self.assertEqual(properties["Stream"], {"relation": [{"id": "stream-business"}]})

    def test_manual_stream_section_overrides_project_stream(self):
        properties = _notion_routing_from_todoist(
            {"labels": ["Project A"], "section_id": "section-personal"},
            {"project a": {"id": "project-1", "name": "Project A", "stream_id": "stream-business"}},
            {
                "бизнес": {"id": "stream-business", "name": "БИЗНЕС"},
                "личное": {"id": "stream-personal", "name": "ЛИЧНОЕ"},
            },
            {"бизнес": "section-business", "личное": "section-personal", "прочее": "section-other"},
        )
        self.assertEqual(properties["Stream"], {"relation": [{"id": "stream-personal"}]})

    def test_project_label_routes_other_task_to_project_stream_section(self):
        section = _project_stream_section(
            {"labels": ["Project A"], "section_id": "section-other"},
            {"project a": {"id": "project-1", "name": "Project A", "stream_id": "stream-business"}},
            {"бизнес": {"id": "stream-business", "name": "БИЗНЕС"}},
            {"бизнес": "section-business", "прочее": "section-other"},
        )
        self.assertEqual(section, "section-business")

    def test_project_label_does_not_override_manual_stream_section(self):
        section = _project_stream_section(
            {"labels": ["Project A"], "section_id": "section-personal"},
            {"project a": {"id": "project-1", "name": "Project A", "stream_id": "stream-business"}},
            {
                "бизнес": {"id": "stream-business", "name": "БИЗНЕС"},
                "личное": {"id": "stream-personal", "name": "ЛИЧНОЕ"},
            },
            {"бизнес": "section-business", "личное": "section-personal", "прочее": "section-other"},
        )
        self.assertIsNone(section)

    def test_todoist_routing_difference_is_detected_against_notion(self):
        self.assertTrue(
            _todoist_routing_differs(
                {"project_id": None, "stream_id": "stream-business"},
                {"labels": [], "section_id": "section-personal"},
                {},
                {
                    "бизнес": {"id": "stream-business", "name": "БИЗНЕС"},
                    "личное": {"id": "stream-personal", "name": "ЛИЧНОЕ"},
                },
                {"бизнес": "section-business", "личное": "section-personal"},
            )
        )

    def test_existing_tasks_match_by_normalized_title_and_due_date(self):
        notion = {"title": "  Подготовить   письмо ", "due_date": "2026-06-13"}
        todoist = {"content": "подготовить письмо", "due": {"date": "2026-06-13"}}
        self.assertEqual(_match_key_notion(notion), _match_key_todoist(todoist))


class TaskSyncTest(unittest.TestCase):
    def test_fresh_deployment_does_not_rebuild_todoist_from_notion(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            notion = {
                "page_id": "page-1",
                "title": "Task",
                "description": "",
                "status": "Backlog",
                "priority": "P2",
                "due_date": None,
                "deadline": None,
                "todoist_id": "todo-1",
                "project_name": "",
                "stream_name": "",
                "inbox_project_id": "inbox-1",
                "section_id": "section-other",
                "last_edited_time": "2026-06-01T10:00:00Z",
            }
            todo = {
                "id": "todo-1",
                "content": "Changed in Todoist",
                "priority": 2,
                "labels": [],
                "project_id": "inbox-1",
                "section_id": None,
                "is_completed": False,
                "updated_at": "2026-06-12T10:00:00Z",
            }
            result = SyncResult(errors=[])
            service._mark_notion_sync = Mock()
            service._update_notion_from_todoist = Mock()
            service._sync_notion_task(notion, {"todo-1": todo}, {}, result)
            todoist.update_task.assert_not_called()
            todoist.update_task_location.assert_not_called()
            service._update_notion_from_todoist.assert_called_once()

    def test_todoist_rate_limit_retry_after_is_honored(self):
        self.assertEqual(
            _retry_after(HttpError(429, '{"error_extra":{"retry_after":1280}}')),
            1280,
        )

    def test_linked_unchanged_task_does_not_sync_again(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            service._mark_notion_sync = Mock()
            notion = {
                "page_id": "page-1",
                "title": "Task",
                "description": "",
                "status": "Backlog",
                "priority": "P2",
                "due_date": "2026-06-13",
                "deadline": None,
                "todoist_id": "todo-1",
                "last_edited_time": "2026-06-11T10:00:00Z",
            }
            todo = {
                "id": "todo-1",
                "content": "Task",
                "description": "",
                "priority": 3,
                "due": {"date": "2026-06-13"},
                "deadline": None,
                "is_completed": False,
                "updated_at": "2026-06-11T10:00:00Z",
            }
            state = {
                "page-1": {
                    "notion": _fingerprint(notion),
                    "todoist": _fingerprint(todo),
                    "todoist_id": "todo-1",
                }
            }
            result = SyncResult(errors=[])
            service._sync_notion_task(notion, {"todo-1": todo}, state, result)
            todoist.update_task.assert_not_called()
            self.assertEqual(result.notion_to_todoist, 0)

    def test_webhook_completion_updates_notion(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            service._find_notion_by_todoist_id = Mock(return_value={"page_id": "page-1"})
            service._update_notion_status = Mock()
            result = service.handle_todoist_event({"event_name": "item:completed", "event_data": {"id": "todo-1"}})
            service._update_notion_status.assert_called_once_with("page-1", "Done")
            self.assertEqual(result["action"], "completed_in_notion")

    def test_webhook_move_updates_notion_stream_from_new_section(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            service._find_notion_by_todoist_id = Mock(return_value={"page_id": "page-1"})
            service._list_notion_projects = Mock(return_value={})
            service._list_notion_streams = Mock(return_value={"работа": {"id": "stream-work", "name": "РАБОТА"}})
            service._ensure_todoist_stream_sections = Mock(
                return_value=("inbox-1", {"работа": "section-work", "прочее": "section-other"}, 0)
            )
            service._update_notion_from_todoist = Mock()
            event_task = {"id": "todo-1", "content": "Task", "labels": [], "section_id": "section-work"}
            todoist.get_task.return_value = event_task
            result = service.handle_todoist_event({"event_name": "item:updated", "event_data": event_task})
            service._update_notion_from_todoist.assert_called_once_with(
                "page-1",
                event_task,
                {},
                {"работа": {"id": "stream-work", "name": "РАБОТА"}},
                {"работа": "section-work", "прочее": "section-other"},
                current_status=None,
            )
            todoist.get_task.assert_called_once_with("todo-1")
            self.assertEqual(result["action"], "upserted_in_notion")

    def test_partial_webhook_loads_full_task_before_updating_notion_routing(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            service._find_notion_by_todoist_id = Mock(return_value={"page_id": "page-1"})
            service._list_notion_projects = Mock(return_value={})
            service._list_notion_streams = Mock(return_value={"личное": {"id": "stream-personal", "name": "ЛИЧНОЕ"}})
            service._ensure_todoist_stream_sections = Mock(
                return_value=("inbox-1", {"личное": "section-personal", "прочее": "section-other"}, 0)
            )
            service._update_notion_from_todoist = Mock()
            full_task = {
                "id": "todo-1",
                "content": "Task",
                "labels": [],
                "section_id": "section-personal",
            }
            todoist.get_task.return_value = full_task
            service.handle_todoist_event(
                {"event_name": "item:updated", "event_data": {"id": "todo-1", "content": "Task"}}
            )
            service._update_notion_from_todoist.assert_called_once_with(
                "page-1",
                full_task,
                {},
                {"личное": {"id": "stream-personal", "name": "ЛИЧНОЕ"}},
                {"личное": "section-personal", "прочее": "section-other"},
                current_status=None,
            )

    def test_webhook_project_label_moves_other_task_to_project_stream(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            service._find_notion_by_todoist_id = Mock(return_value={"page_id": "page-1"})
            service._list_notion_projects = Mock(
                return_value={"project a": {"id": "project-1", "name": "Project A", "stream_id": "stream-business"}}
            )
            service._list_notion_streams = Mock(
                return_value={"бизнес": {"id": "stream-business", "name": "БИЗНЕС"}}
            )
            service._ensure_todoist_stream_sections = Mock(
                return_value=("inbox-1", {"бизнес": "section-business", "прочее": "section-other"}, 0)
            )
            service._update_notion_from_todoist = Mock()
            event_task = {
                "id": "todo-1",
                "content": "Task",
                "labels": ["Project A"],
                "section_id": "section-other",
            }
            todoist.get_task.return_value = event_task
            service.handle_todoist_event({"event_name": "item:updated", "event_data": event_task})
            todoist.update_task_location.assert_called_once_with("todo-1", "inbox-1", "section-business")

    def test_periodic_sync_completion_updates_notion(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            service._list_notion_projects = Mock(return_value={})
            service._update_notion_from_todoist = Mock()
            notion = {
                "page_id": "page-1",
                "title": "Task",
                "description": "",
                "status": "Backlog",
                "priority": "P2",
                "due_date": None,
                "deadline": None,
                "todoist_id": "todo-1",
                "last_edited_time": "2026-06-11T10:00:00Z",
            }
            todo = {"id": "todo-1", "content": "Task", "priority": 2, "is_completed": True}
            result = SyncResult(errors=[])
            service._sync_notion_task(notion, {"todo-1": todo}, {}, result)
            service._update_notion_from_todoist.assert_called_once_with(
                "page-1", todo, {}, {}, {}, current_status="Backlog"
            )
            todoist.reopen_task.assert_not_called()
            self.assertEqual(result.todoist_to_notion, 1)

    def test_explicit_notion_reopen_reopens_todoist(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            service._mark_notion_sync = Mock()
            notion = {
                "page_id": "page-1",
                "title": "Task",
                "description": "",
                "status": "Backlog",
                "priority": "P2",
                "due_date": None,
                "deadline": None,
                "todoist_id": "todo-1",
                "last_edited_time": "2026-06-11T11:00:00Z",
            }
            previous_notion = {**notion, "status": "Done"}
            todo = {"id": "todo-1", "content": "Task", "priority": 2, "is_completed": True}
            state = {
                "page-1": {
                    "notion": _fingerprint(previous_notion),
                    "todoist": _fingerprint({**todo, "section_id": "section-work"}),
                    "todoist_id": "todo-1",
                }
            }
            result = SyncResult(errors=[])
            service._sync_notion_task(notion, {"todo-1": todo}, state, result)
            todoist.reopen_task.assert_called_once_with("todo-1")
            todoist.update_task.assert_called_once_with("todo-1", notion)
            self.assertEqual(result.notion_to_todoist, 1)

    def test_all_notion_projects_become_todoist_labels(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        todoist.list_labels.return_value = [{"name": "Existing Project"}]
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            created = service._ensure_todoist_project_labels(
                {
                    "existing project": {"id": "project-1", "name": "Existing Project"},
                    "new project": {"id": "project-2", "name": "New Project"},
                }
            )
            todoist.create_label.assert_called_once_with("New Project")
            self.assertEqual(created, 1)

    def test_notion_streams_become_inbox_sections(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        todoist.list_projects.return_value = [{"id": "inbox-1", "inbox_project": True}]
        todoist.list_sections.return_value = [{"id": "section-1", "name": "РАБОТА", "project_id": "inbox-1"}]
        todoist.create_section.return_value = "section-2"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            inbox_id, sections, created = service._ensure_todoist_stream_sections(
                {
                    "работа": {"id": "stream-1", "name": "РАБОТА"},
                    "бизнес": {"id": "stream-2", "name": "БИЗНЕС"},
                }
            )
            self.assertEqual(inbox_id, "inbox-1")
            self.assertEqual(sections["работа"], "section-1")
            self.assertEqual(sections["бизнес"], "section-2")
            self.assertIn("прочее", sections)
            self.assertEqual(
                todoist.create_section.call_args_list,
                [unittest.mock.call("БИЗНЕС", "inbox-1"), unittest.mock.call("ПРОЧЕЕ", "inbox-1")],
            )
            self.assertEqual(created, 2)

    def test_task_stream_overrides_project_stream_for_inbox_section(self):
        tasks = [{"project_id": "project-1", "stream_id": "stream-family"}]
        TaskSyncService._attach_notion_routing(
            tasks,
            {"project": {"id": "project-1", "name": "Project", "stream_id": "stream-work"}},
            {
                "работа": {"id": "stream-work", "name": "РАБОТА"},
                "семья": {"id": "stream-family", "name": "СЕМЬЯ"},
            },
            "inbox-1",
            {"работа": "section-work", "семья": "section-family"},
        )
        self.assertEqual(tasks[0]["stream_name"], "СЕМЬЯ")
        self.assertEqual(tasks[0]["section_id"], "section-family")

    def test_empty_task_stream_routes_to_other_even_when_project_has_stream(self):
        tasks = [{"project_id": "project-1", "stream_id": None}]
        TaskSyncService._attach_notion_routing(
            tasks,
            {"project": {"id": "project-1", "name": "Project", "stream_id": "stream-work"}},
            {"работа": {"id": "stream-work", "name": "РАБОТА"}},
            "inbox-1",
            {"работа": "section-work", "прочее": "section-other"},
        )
        self.assertEqual(tasks[0]["stream_name"], "")
        self.assertEqual(tasks[0]["section_id"], "section-other")

    def test_empty_notion_routing_is_initialized_from_matching_todoist_labels(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            with unittest.mock.patch("conductor.task_sync.request_json") as request:
                tasks = [{"page_id": "page-1", "todoist_id": "todo-1", "project_id": None, "stream_id": None}]
                service._enrich_missing_notion_routing(
                    tasks,
                    {"todo-1": {"labels": ["Project A", "РАБОТА"]}},
                    {"project a": {"id": "project-1", "name": "Project A", "stream_id": "stream-work"}},
                    {"работа": {"id": "stream-work", "name": "РАБОТА"}},
                )
                self.assertEqual(tasks[0]["project_id"], "project-1")
                self.assertEqual(tasks[0]["stream_id"], "stream-work")
                request.assert_called_once()
                properties = request.call_args.kwargs["payload"]["properties"]
                self.assertEqual(properties["Проект"], {"relation": [{"id": "project-1"}]})
                self.assertEqual(properties["Stream"], {"relation": [{"id": "stream-work"}]})

    def test_fresh_deployment_imports_todoist_section_into_notion_stream(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            tasks = [{"page_id": "page-1", "todoist_id": "todo-1", "project_id": None, "stream_id": None}]
            with unittest.mock.patch("conductor.task_sync.request_json") as request:
                imported = service._bootstrap_notion_routing(
                    tasks,
                    {"todo-1": {"id": "todo-1", "labels": [], "section_id": "section-business", "is_completed": False}},
                    {},
                    {"бизнес": {"id": "stream-business", "name": "БИЗНЕС"}},
                    {"бизнес": "section-business", "прочее": "section-other"},
                )
            self.assertEqual(imported, 1)
            self.assertEqual(tasks[0]["stream_id"], "stream-business")
            properties = request.call_args.kwargs["payload"]["properties"]
            self.assertEqual(properties["Stream"], {"relation": [{"id": "stream-business"}]})
            todoist.update_task_routing_batch.assert_not_called()

    def test_fresh_deployment_imports_other_section_as_empty_notion_stream(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            tasks = [
                {
                    "page_id": "page-1",
                    "todoist_id": "todo-1",
                    "project_id": None,
                    "stream_id": "stream-business",
                }
            ]
            with unittest.mock.patch("conductor.task_sync.request_json") as request:
                imported = service._bootstrap_notion_routing(
                    tasks,
                    {"todo-1": {"id": "todo-1", "labels": [], "section_id": "section-other", "is_completed": False}},
                    {},
                    {"бизнес": {"id": "stream-business", "name": "БИЗНЕС"}},
                    {"бизнес": "section-business", "прочее": "section-other"},
                )
            self.assertEqual(imported, 1)
            self.assertIsNone(tasks[0]["stream_id"])
            properties = request.call_args.kwargs["payload"]["properties"]
            self.assertEqual(properties["Stream"], {"relation": []})

    def test_changed_todoist_label_updates_notion_project(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            notion = {
                "page_id": "page-1",
                "title": "Task",
                "description": "",
                "status": "Backlog",
                "priority": "P2",
                "due_date": None,
                "deadline": None,
                "todoist_id": "todo-1",
                "project_id": "project-1",
                "stream_id": "stream-work",
                "project_name": "Notion Project",
                "last_edited_time": "2026-06-11T10:00:00Z",
            }
            todo = {
                "id": "todo-1",
                "content": "Task",
                "description": "",
                "priority": 2,
                "labels": ["Todoist Project"],
                "is_completed": False,
                "updated_at": "2026-06-11T10:00:00Z",
            }
            state = {
                "page-1": {
                    "notion": _fingerprint(notion),
                    "todoist": _fingerprint({**todo, "labels": ["Notion Project"]}),
                    "todoist_id": "todo-1",
                }
            }
            result = SyncResult(errors=[])
            service._update_notion_from_todoist = Mock()
            service._sync_notion_task(notion, {"todo-1": todo}, state, result)
            service._update_notion_from_todoist.assert_called_once()
            todoist.update_task_labels.assert_not_called()

    def test_later_todoist_section_overrides_different_notion_stream(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            notion = {
                "page_id": "page-1",
                "title": "Task",
                "description": "",
                "status": "Backlog",
                "priority": "P2",
                "due_date": None,
                "deadline": None,
                "todoist_id": "todo-1",
                "project_name": "Notion Project",
                "stream_name": "РАБОТА",
                "inbox_project_id": "inbox-1",
                "section_id": "section-work",
                "last_edited_time": "2026-06-12T09:00:00Z",
            }
            todo = {
                "id": "todo-1",
                "content": "Task",
                "description": "",
                "priority": 2,
                "labels": ["Notion Project"],
                "project_id": "inbox-1",
                "section_id": "section-personal",
                "is_completed": False,
                "updated_at": "2026-06-12T10:00:00Z",
            }
            state = {
                "page-1": {
                    "notion": _fingerprint(notion),
                    "todoist": _fingerprint({**todo, "section_id": "section-work"}),
                    "todoist_id": "todo-1",
                }
            }
            result = SyncResult(errors=[])
            service._update_notion_from_todoist = Mock()
            service._sync_notion_task(
                notion,
                {"todo-1": todo},
                state,
                result,
                {"notion project": {"id": "project-1", "name": "Notion Project", "stream_id": "stream-work"}},
                {
                    "работа": {"id": "stream-work", "name": "РАБОТА"},
                    "личное": {"id": "stream-personal", "name": "ЛИЧНОЕ"},
                },
                {"работа": "section-work", "личное": "section-personal", "прочее": "section-other"},
            )
            service._update_notion_from_todoist.assert_called_once()
            todoist.update_task_location.assert_not_called()
            self.assertEqual(result.todoist_to_notion, 1)

    def test_later_notion_section_overrides_todoist_section(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            service._mark_notion_sync = Mock()
            notion = {
                "page_id": "page-1",
                "title": "Task",
                "description": "",
                "status": "Backlog",
                "priority": "P2",
                "due_date": None,
                "deadline": None,
                "todoist_id": "todo-1",
                "project_name": "",
                "stream_name": "РАБОТА",
                "inbox_project_id": "inbox-1",
                "section_id": "section-work",
                "last_edited_time": "2026-06-12T11:00:00Z",
            }
            previous_notion = {**notion, "stream_name": "ЛИЧНОЕ", "section_id": "section-personal"}
            todo = {
                "id": "todo-1",
                "content": "Task",
                "description": "",
                "priority": 2,
                "labels": [],
                "project_id": "inbox-1",
                "section_id": "section-personal",
                "is_completed": False,
                "updated_at": "2026-06-12T10:00:00Z",
            }
            state = {
                "page-1": {
                    "notion": _fingerprint(previous_notion),
                    "todoist": _fingerprint(todo),
                    "todoist_id": "todo-1",
                }
            }
            result = SyncResult(errors=[])
            service._sync_notion_task(notion, {"todo-1": todo}, state, result)
            todoist.update_task_location.assert_called_once_with("todo-1", "inbox-1", "section-work")
            self.assertEqual(result.notion_to_todoist, 1)

    def test_equal_conflict_timestamp_prefers_todoist(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            service._update_notion_from_todoist = Mock()
            notion = {
                "page_id": "page-1",
                "title": "Changed in Notion",
                "description": "",
                "status": "Backlog",
                "priority": "P2",
                "due_date": None,
                "deadline": None,
                "todoist_id": "todo-1",
                "last_edited_time": "2026-06-12T10:00:00Z",
            }
            todo = {
                "id": "todo-1",
                "content": "Changed in Todoist",
                "description": "",
                "priority": 2,
                "is_completed": False,
                "updated_at": "2026-06-12T10:00:00Z",
            }
            baseline_notion = {**notion, "title": "Baseline"}
            baseline_todo = {**todo, "content": "Baseline"}
            state = {
                "page-1": {
                    "notion": _fingerprint(baseline_notion),
                    "todoist": _fingerprint(baseline_todo),
                    "todoist_id": "todo-1",
                }
            }
            result = SyncResult(errors=[])
            service._sync_notion_task(notion, {"todo-1": todo}, state, result)
            service._update_notion_from_todoist.assert_called_once()
            todoist.update_task.assert_not_called()

    def test_completed_notion_task_is_created_and_closed_in_todoist(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        todoist.create_task.return_value = "todo-1"
        with tempfile.TemporaryDirectory() as directory:
            service = TaskSyncService("notion", "tasks", "projects", todoist, str(Path(directory) / "state.json"))
            service._set_notion_todoist_id = Mock()
            notion = {
                "page_id": "page-1",
                "title": "Завершённая задача",
                "description": "",
                "status": "Done",
                "priority": "P3",
                "due_date": None,
                "deadline": None,
                "todoist_id": "",
                "project_name": "AI DESIGN SYSTEM",
                "last_edited_time": "2026-06-11T10:00:00Z",
            }
            result = SyncResult(errors=[])
            service._sync_notion_task(notion, {}, {}, result)
            todoist.create_task.assert_called_once_with(notion)
            todoist.close_task.assert_called_once_with("todo-1")
            self.assertEqual(result.completed, 1)


if __name__ == "__main__":
    unittest.main()
