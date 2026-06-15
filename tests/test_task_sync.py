import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from conductor.http import HttpError
from conductor.task_sync import (
    SyncResult,
    TaskSyncService,
    _fingerprint,
    _managed_labels,
    _match_key_notion,
    _match_key_todoist,
    _notion_properties_from_todoist,
    _notion_routing_from_todoist,
    _notion_stored_state,
    _parse_time,
    _priority_to_strategic,
    _retry_after,
    _strategic_to_priority,
)
from conductor.todoist_client import TodoistClient, _task_payload, todoist_priority


def service(todoist=None, directory=None, **kwargs):
    todoist = todoist or Mock(spec=TodoistClient)
    todoist.enabled = True
    todoist.api_token = "token"
    state_path = str(Path(directory or tempfile.gettempdir()) / "state.json")
    snapshot_path = str(Path(directory or tempfile.gettempdir()) / "snapshot.json")
    return TaskSyncService(
        "notion",
        "tasks",
        "projects",
        todoist,
        state_path,
        streams_database_id="streams",
        snapshot_path=snapshot_path,
        **kwargs,
    )


class TodoistMappingTest(unittest.TestCase):
    def test_priority_mapping_round_trip(self):
        self.assertEqual(todoist_priority(4), "P1")
        self.assertEqual(_strategic_to_priority("7"), "P2")
        self.assertEqual(_priority_to_strategic("P3"), "5")

    def test_todoist_payload_routes_by_project_and_section_not_project_label(self):
        payload = _task_payload(
            {
                "title": "Подготовить письмо",
                "project_name": "Old project label",
                "todoist_project_id": "todo-project-1",
                "todoist_section_id": "section-1",
                "managed_labels": ["письмо", "низкая_энергия"],
            }
        )
        self.assertEqual(payload["project_id"], "todo-project-1")
        self.assertEqual(payload["section_id"], "section-1")
        self.assertEqual(payload["labels"], ["письмо", "низкая_энергия"])
        self.assertNotIn("Old project label", payload["labels"])

    def test_only_operational_labels_are_managed(self):
        self.assertEqual(
            _managed_labels(["Project A", "встреча", "@пятиминутное_дело", "custom"]),
            ["встреча", "пятиминутное_дело"],
        )

    def test_todoist_project_and_section_become_notion_routing(self):
        properties = _notion_routing_from_todoist(
            {"project_id": "todo-project-1", "section_id": "section-1"},
            {
                "project a": {
                    "id": "notion-project-1",
                    "name": "Project A",
                    "stream_id": "stream-business",
                    "todoist_project_id": "todo-project-1",
                }
            },
            {"бизнес": {"id": "stream-business", "name": "БИЗНЕС"}},
            [{"id": "todo-project-1", "name": "Project A"}],
            [{"id": "section-1", "name": "Сделать", "project_id": "todo-project-1"}],
        )
        self.assertEqual(properties["Проект"], {"relation": [{"id": "notion-project-1"}]})
        self.assertEqual(properties["Stream"], {"relation": [{"id": "stream-business"}]})
        self.assertEqual(properties["Раздел"]["rich_text"][0]["text"]["content"], "Сделать")
        self.assertEqual(properties["Todoist Section ID"]["rich_text"][0]["text"]["content"], "section-1")

    def test_same_section_name_in_another_project_does_not_collide(self):
        properties = _notion_routing_from_todoist(
            {"project_id": "todo-project-2", "section_id": "section-2"},
            {
                "a": {"id": "notion-a", "stream_id": "", "todoist_project_id": "todo-project-1"},
                "b": {"id": "notion-b", "stream_id": "", "todoist_project_id": "todo-project-2"},
            },
            {},
            [{"id": "todo-project-1"}, {"id": "todo-project-2"}],
            [
                {"id": "section-1", "name": "Сделать", "project_id": "todo-project-1"},
                {"id": "section-2", "name": "Сделать", "project_id": "todo-project-2"},
            ],
        )
        self.assertEqual(properties["Проект"], {"relation": [{"id": "notion-b"}]})
        self.assertEqual(properties["Todoist Section ID"]["rich_text"][0]["text"]["content"], "section-2")

    def test_todoist_fields_include_managed_labels(self):
        properties = _notion_properties_from_todoist(
            {"content": "Task", "labels": ["Project A", "анализ", "custom"]}
        )
        self.assertEqual(properties["Метки Todoist"], {"multi_select": [{"name": "анализ"}]})

    def test_existing_tasks_match_by_normalized_title_and_due_date(self):
        notion = {"title": "  Подготовить   письмо ", "due_date": "2026-06-13"}
        todoist = {"content": "подготовить письмо", "due": {"date": "2026-06-13"}}
        self.assertEqual(_match_key_notion(notion), _match_key_todoist(todoist))

    def test_parse_time_and_stored_hashes(self):
        self.assertEqual(_parse_time("2026-06-12T10:00:00Z"), datetime(2026, 6, 12, 10, tzinfo=timezone.utc))
        self.assertEqual(
            _notion_stored_state({"todoist_id": "1", "sync_notion_hash": "n", "sync_todoist_hash": "t"}),
            {"notion": "n", "todoist": "t", "todoist_id": "1"},
        )


class SafetyTest(unittest.TestCase):
    def test_observe_mode_writes_snapshot_and_performs_no_remote_writes(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        todoist.list_projects.return_value = [{"id": "tp-1", "name": "Project A"}]
        todoist.list_sections.return_value = [{"id": "s-1", "name": "Doing", "project_id": "tp-1"}]
        todoist.list_labels.return_value = [{"id": "l-1", "name": "анализ"}]
        todoist.list_tasks.return_value = [{"id": "t-1", "project_id": "tp-1", "section_id": "s-1"}]
        with tempfile.TemporaryDirectory() as directory:
            sync = service(todoist, directory, mode="observe")
            sync._list_notion_tasks = Mock(return_value=[])
            sync._list_notion_projects = Mock(return_value={})
            sync._list_notion_streams = Mock(return_value={})
            result = sync.sync()
            snapshot = json.loads(Path(result["snapshot_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result["mode"], "observe")
        self.assertEqual(snapshot["todoist"]["active_tasks"][0]["id"], "t-1")
        todoist.create_project.assert_not_called()
        todoist.create_task.assert_not_called()
        todoist.update_task_location.assert_not_called()
        todoist.update_task_labels.assert_not_called()

    def test_observe_webhook_is_ignored(self):
        sync = service(mode="observe")
        result = sync.handle_todoist_event({"event_name": "item:updated", "event_data": {"id": "t-1"}})
        self.assertEqual(result["reason"], "sync is in observe mode")

    def test_missing_task_is_not_cancelled_by_default(self):
        sync = service(mode="write")
        sync._update_notion_status = Mock()
        with self.assertRaisesRegex(RuntimeError, "manual review"):
            sync._sync_notion_task(
                {"page_id": "p-1", "title": "Task", "status": "Backlog", "todoist_id": "missing"},
                {},
                {},
                SyncResult(errors=[]),
            )
        sync._update_notion_status.assert_not_called()

    def test_task_creation_is_blocked_by_default(self):
        sync = service(mode="write")
        with self.assertRaisesRegex(RuntimeError, "TODOIST_ALLOW_TASK_CREATE"):
            sync._sync_notion_task(
                {"page_id": "p-1", "title": "Task", "status": "Backlog", "todoist_id": ""},
                {},
                {},
                SyncResult(errors=[]),
            )
        sync.todoist.create_task.assert_not_called()

    def test_unknown_labels_are_preserved(self):
        sync = service(mode="write", allow_label_write=True)
        notion = {
            "todoist_id": "t-1",
            "managed_labels": ["анализ"],
            "todoist_project_id": "",
            "todoist_section_id": "",
        }
        todo = {"id": "t-1", "labels": ["Project A", "custom", "встреча"], "is_completed": False}
        sync._enforce_todoist_routing(notion, todo)
        sync.todoist.update_task_labels.assert_called_once_with("t-1", ["Project A", "custom", "анализ"])

    def test_base_task_update_cannot_bypass_label_and_move_guards(self):
        sync = service(mode="write")
        sync._mark_notion_sync = Mock()
        notion = {
            "page_id": "p-1",
            "todoist_id": "t-1",
            "title": "Changed",
            "description": "",
            "status": "Backlog",
            "managed_labels": ["анализ"],
            "todoist_project_id": "tp-2",
            "todoist_section_id": "s-2",
        }
        todo = {"id": "t-1", "labels": ["custom"], "project_id": "tp-1", "is_completed": False}
        with self.assertRaisesRegex(RuntimeError, "TODOIST_ALLOW_LABEL_WRITE"):
            sync._update_todoist_from_notion(notion, todo)
        payload = sync.todoist.update_task.call_args.args[1]
        self.assertNotIn("managed_labels", payload)
        self.assertNotIn("todoist_project_id", payload)
        self.assertNotIn("todoist_section_id", payload)

    def test_move_is_blocked_without_permission(self):
        sync = service(mode="write")
        notion = {
            "todoist_id": "t-1",
            "managed_labels": [],
            "todoist_project_id": "tp-2",
            "todoist_section_id": "",
        }
        todo = {"id": "t-1", "labels": [], "project_id": "tp-1", "is_completed": False}
        with self.assertRaisesRegex(RuntimeError, "TODOIST_ALLOW_TASK_MOVE"):
            sync._enforce_todoist_routing(notion, todo)
        sync.todoist.update_task_location.assert_not_called()

    def test_move_limit_stops_batch(self):
        sync = service(mode="write", allow_task_move=True, max_task_moves=1)
        notion = {
            "todoist_id": "t-1",
            "managed_labels": [],
            "todoist_project_id": "tp-2",
            "todoist_section_id": "",
        }
        sync._enforce_todoist_routing(notion, {"labels": [], "project_id": "tp-1", "is_completed": False})
        notion["todoist_id"] = "t-2"
        with self.assertRaisesRegex(RuntimeError, "move limit"):
            sync._enforce_todoist_routing(notion, {"labels": [], "project_id": "tp-1", "is_completed": False})

    def test_project_hierarchy_creation_requires_opt_in(self):
        todoist = Mock(spec=TodoistClient)
        todoist.enabled = True
        todoist.api_token = "token"
        sync = service(todoist, mode="write", allow_project_create=True)
        sync._set_notion_external_id = Mock()
        todoist.create_project.side_effect = ["stream-todo", "project-todo"]
        projects = {
            "a": {
                "id": "notion-project",
                "name": "Project A",
                "stream_id": "notion-stream",
                "todoist_project_id": "",
                "sync_enabled": True,
            }
        }
        streams = {
            "business": {
                "id": "notion-stream",
                "name": "Business",
                "todoist_project_id": "",
            }
        }
        sync._ensure_todoist_project_hierarchy(projects, streams, [], SyncResult(errors=[]))
        self.assertEqual(todoist.create_project.call_args_list[0].args, ("Business",))
        self.assertEqual(todoist.create_project.call_args_list[1].args, ("Project A", "stream-todo"))

    def test_project_without_sync_checkbox_is_not_created(self):
        sync = service(mode="write", allow_project_create=True)
        sync._ensure_todoist_project_hierarchy(
            {"a": {"id": "p", "name": "A", "stream_id": "", "todoist_project_id": "", "sync_enabled": False}},
            {},
            [],
            SyncResult(errors=[]),
        )
        sync.todoist.create_project.assert_not_called()

    def test_rate_limit_retry_after_is_honored(self):
        self.assertEqual(_retry_after(HttpError(429, '{"error_extra":{"retry_after":1280}}')), 1280)


class TodoistClientTest(unittest.TestCase):
    def test_move_to_section_does_not_also_send_project(self):
        client = TodoistClient("token", True)
        with patch("conductor.todoist_client.request_json") as request:
            request.return_value = {"sync_status": {"command-1": "ok"}}
            with patch("conductor.todoist_client.uuid4", return_value="command-1"):
                client.update_task_location("task-1", "project-1", "section-1")
        args = request.call_args.kwargs["payload"]["commands"][0]["args"]
        self.assertEqual(args, {"id": "task-1", "section_id": "section-1"})

    def test_create_child_project_sends_parent_id(self):
        client = TodoistClient("token", True)
        with patch("conductor.todoist_client.request_json", return_value={"id": "child"}) as request:
            result = client.create_project("Project A", "stream-1")
        self.assertEqual(result, "child")
        self.assertEqual(request.call_args.kwargs["payload"], {"name": "Project A", "parent_id": "stream-1"})


if __name__ == "__main__":
    unittest.main()
