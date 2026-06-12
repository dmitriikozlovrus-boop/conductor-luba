from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .http import request_json
from .todoist_client import TodoistClient, todoist_priority

NOTION_VERSION = "2022-06-28"
OTHER_SECTION_NAME = "ПРОЧЕЕ"


@dataclass
class SyncResult:
    notion_to_todoist: int = 0
    todoist_to_notion: int = 0
    completed: int = 0
    labels_created: int = 0
    sections_created: int = 0
    errors: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "notion_to_todoist": self.notion_to_todoist,
            "todoist_to_notion": self.todoist_to_notion,
            "completed": self.completed,
            "labels_created": self.labels_created,
            "sections_created": self.sections_created,
            "errors": self.errors or [],
        }


class TaskSyncService:
    def __init__(
        self,
        notion_token: str,
        tasks_database_id: str,
        projects_database_id: str,
        todoist: TodoistClient,
        state_path: str,
        completed_since: str = "2007-01-01",
        streams_database_id: str = "",
    ):
        self.notion_token = notion_token
        self.tasks_database_id = tasks_database_id
        self.projects_database_id = projects_database_id
        self.todoist = todoist
        self.state_path = Path(state_path)
        self.completed_since = completed_since
        self.streams_database_id = streams_database_id
        self._lock = threading.Lock()

    @property
    def notion_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.notion_token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    @property
    def enabled(self) -> bool:
        return bool(
            self.notion_token
            and self.tasks_database_id
            and self.projects_database_id
            and self.todoist.enabled
            and self.todoist.api_token
        )

    def sync(self) -> dict[str, Any]:
        result = SyncResult(errors=[])
        if not self.enabled:
            result.errors.append("Todoist sync is not configured")
            return result.as_dict()
        if not self._lock.acquire(blocking=False):
            result.errors.append("Todoist sync is already running")
            return result.as_dict()
        try:
            state = self._load_state()
            notion_tasks = self._list_notion_tasks()
            streams = self._list_notion_streams()
            projects = self._list_notion_projects()
            result.labels_created = self._ensure_todoist_project_labels(projects)
            inbox_project_id, sections, result.sections_created = self._ensure_todoist_stream_sections(streams)
            active_tasks = self.todoist.list_tasks()
            completed_since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            try:
                completed_tasks = self.todoist.list_completed_tasks(completed_since)
            except Exception as exc:  # noqa: BLE001
                completed_tasks = []
                result.errors.append(f"Could not load completed Todoist tasks: {exc}")
            todoist_tasks = {str(task["id"]): task for task in [*active_tasks, *completed_tasks]}
            linked_ids = {task["todoist_id"] for task in notion_tasks if task["todoist_id"]}
            self._link_existing_matches(notion_tasks, todoist_tasks, linked_ids)
            self._enrich_missing_notion_routing(notion_tasks, todoist_tasks, projects, streams)
            self._attach_notion_routing(notion_tasks, projects, streams, inbox_project_id, sections)

            for notion_task in notion_tasks:
                try:
                    self._sync_notion_task(notion_task, todoist_tasks, state, result, projects, streams, sections)
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"{notion_task['title']}: {exc}")
                    self._mark_notion_sync(notion_task["page_id"], "Error")

            for todoist_id, todoist_task in todoist_tasks.items():
                if todoist_id in linked_ids:
                    continue
                try:
                    self._create_notion_from_todoist(todoist_task, projects, streams, sections)
                    result.todoist_to_notion += 1
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"Todoist {todoist_id}: {exc}")
            state["__meta__"] = {
                "last_successful_sync": datetime.now(timezone.utc).isoformat(),
            }
            self._save_state(state)
            return result.as_dict()
        finally:
            self._lock.release()

    def _link_existing_matches(
        self,
        notion_tasks: list[dict[str, Any]],
        todoist_tasks: dict[str, dict[str, Any]],
        linked_ids: set[str],
    ) -> None:
        available: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for todoist_id, task in todoist_tasks.items():
            if todoist_id in linked_ids:
                continue
            available.setdefault(_match_key_todoist(task), []).append(task)
        for notion_task in notion_tasks:
            if notion_task["todoist_id"]:
                continue
            matches = available.get(_match_key_notion(notion_task), [])
            if len(matches) != 1:
                continue
            match = matches.pop()
            todoist_id = str(match["id"])
            notion_task["todoist_id"] = todoist_id
            linked_ids.add(todoist_id)
            self._set_notion_todoist_id(notion_task["page_id"], todoist_id, "Synced")

    def handle_todoist_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {"ignored": True, "reason": "sync disabled"}
        event_name = str(event.get("event_name") or "")
        data = event.get("event_data") or {}
        task_id = str(data.get("id") or "")
        if not task_id:
            return {"ignored": True, "reason": "missing task id"}
        notion_task = self._find_notion_by_todoist_id(task_id)
        if event_name == "item:deleted":
            if notion_task:
                self._update_notion_status(notion_task["page_id"], "Cancelled")
            return {"ok": True, "action": "cancelled_in_notion"}
        if event_name == "item:completed":
            if notion_task:
                self._update_notion_status(notion_task["page_id"], "Done")
            return {"ok": True, "action": "completed_in_notion"}
        if event_name == "item:uncompleted":
            if notion_task:
                self._update_notion_status(notion_task["page_id"], "Backlog")
            return {"ok": True, "action": "reopened_in_notion"}
        if event_name in {"item:added", "item:updated"}:
            data = self.todoist.get_task(task_id)
            projects = self._list_notion_projects()
            streams = self._list_notion_streams()
            _, sections, _ = self._ensure_todoist_stream_sections(streams)
            if notion_task:
                self._update_notion_from_todoist(notion_task["page_id"], data, projects, streams, sections)
            else:
                self._create_notion_from_todoist(data, projects, streams, sections)
            return {"ok": True, "action": "upserted_in_notion"}
        return {"ignored": True, "reason": f"unsupported event {event_name}"}

    def _sync_notion_task(
        self,
        notion_task: dict[str, Any],
        todoist_tasks: dict[str, dict[str, Any]],
        state: dict[str, Any],
        result: SyncResult,
        projects: dict[str, dict[str, str]] | None = None,
        streams: dict[str, dict[str, str]] | None = None,
        sections: dict[str, str] | None = None,
    ) -> None:
        projects = projects or {}
        streams = streams or {}
        sections = sections or {}
        todoist_id = notion_task["todoist_id"]
        if not todoist_id:
            created_id = self.todoist.create_task(notion_task)
            self._set_notion_todoist_id(notion_task["page_id"], str(created_id), "Synced")
            if notion_task["status"] in {"Done", "Cancelled"}:
                self.todoist.close_task(str(created_id))
                result.completed += 1
            state[notion_task["page_id"]] = {
                "notion": _fingerprint(notion_task),
                "todoist": "",
                "todoist_id": str(created_id),
            }
            result.notion_to_todoist += 1
            return

        todoist_task = todoist_tasks.get(todoist_id)
        if notion_task["status"] == "Done":
            if todoist_task and not todoist_task.get("is_completed"):
                self.todoist.close_task(todoist_id)
            self._mark_notion_sync(notion_task["page_id"], "Synced")
            state[notion_task["page_id"]] = {
                "notion": _fingerprint(notion_task),
                "todoist": _fingerprint(todoist_task or {}),
                "todoist_id": todoist_id,
            }
            result.completed += 1
            return
        if notion_task["status"] == "Cancelled":
            if todoist_task and not todoist_task.get("is_completed"):
                self.todoist.close_task(todoist_id)
            self._mark_notion_sync(notion_task["page_id"], "Synced")
            state[notion_task["page_id"]] = {
                "notion": _fingerprint(notion_task),
                "todoist": _fingerprint(todoist_task or {}),
                "todoist_id": todoist_id,
            }
            result.completed += 1
            return
        if todoist_task and todoist_task.get("is_completed"):
            prior = state.get(notion_task["page_id"], {})
            notion_changed = bool(prior) and _fingerprint(notion_task) != prior.get("notion")
            todoist_changed = not prior or _fingerprint(todoist_task) != prior.get("todoist")
            if notion_changed and not todoist_changed:
                self.todoist.reopen_task(todoist_id)
                self.todoist.update_task(todoist_id, notion_task)
                self._mark_notion_sync(notion_task["page_id"], "Synced")
                result.notion_to_todoist += 1
            else:
                self._update_notion_from_todoist(notion_task["page_id"], todoist_task, projects, streams, sections)
                result.todoist_to_notion += 1
            state[notion_task["page_id"]] = {
                "notion": _fingerprint(notion_task),
                "todoist": _fingerprint(todoist_task),
                "todoist_id": todoist_id,
            }
            return
        if not todoist_task:
            # Active Notion task linked to a missing Todoist task is recreated.
            created_id = self.todoist.create_task(notion_task)
            self._set_notion_todoist_id(notion_task["page_id"], str(created_id), "Synced")
            state[notion_task["page_id"]] = {
                "notion": _fingerprint(notion_task),
                "todoist": "",
                "todoist_id": str(created_id),
            }
            result.notion_to_todoist += 1
            return

        prior = state.get(notion_task["page_id"], {})
        notion_fingerprint = _fingerprint(notion_task)
        todoist_fingerprint = _fingerprint(todoist_task)
        notion_changed = notion_fingerprint != prior.get("notion")
        todoist_changed = todoist_fingerprint != prior.get("todoist")
        if not prior:
            # Notion is the information core. After a fresh deployment, rebuild
            # Todoist from Notion instead of treating missing sync history as a
            # user edit made in Todoist.
            notion_changed = True
            todoist_changed = False

        if todoist_changed and not notion_changed:
            self._update_notion_from_todoist(notion_task["page_id"], todoist_task, projects, streams, sections)
            result.todoist_to_notion += 1
        elif notion_changed and not todoist_changed:
            self.todoist.update_task(todoist_id, notion_task)
            self._enforce_todoist_routing(notion_task, todoist_task)
            self._mark_notion_sync(notion_task["page_id"], "Synced")
            result.notion_to_todoist += 1
        elif notion_changed and todoist_changed:
            if _parse_time(todoist_task.get("updated_at") or todoist_task.get("added_at")) > _parse_time(
                notion_task["last_edited_time"]
            ):
                self._update_notion_from_todoist(notion_task["page_id"], todoist_task, projects, streams, sections)
                result.todoist_to_notion += 1
            else:
                self.todoist.update_task(todoist_id, notion_task)
                self._enforce_todoist_routing(notion_task, todoist_task)
                self._mark_notion_sync(notion_task["page_id"], "Synced")
                result.notion_to_todoist += 1
        else:
            self._enforce_todoist_routing(notion_task, todoist_task)
        state[notion_task["page_id"]] = {
            "notion": notion_fingerprint,
            "todoist": _fingerprint(todoist_task),
            "todoist_id": todoist_id,
        }

    def _enforce_todoist_routing(self, notion_task: dict[str, Any], todoist_task: dict[str, Any]) -> None:
        if todoist_task.get("is_completed"):
            return
        expected_labels = [notion_task["project_name"]] if notion_task.get("project_name") else []
        if todoist_task.get("labels", []) != expected_labels:
            self.todoist.update_task_labels(notion_task["todoist_id"], expected_labels)
            todoist_task["labels"] = expected_labels
        expected_section = notion_task.get("section_id")
        if notion_task.get("inbox_project_id") and (
            str(todoist_task.get("project_id")) != notion_task["inbox_project_id"]
            or str(todoist_task.get("section_id") or "") != str(expected_section or "")
        ):
            self.todoist.update_task_location(
                notion_task["todoist_id"],
                notion_task["inbox_project_id"],
                expected_section,
            )
            todoist_task["project_id"] = notion_task["inbox_project_id"]
            todoist_task["section_id"] = expected_section

    def _list_notion_tasks(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = request_json(
                "POST",
                f"https://api.notion.com/v1/databases/{self.tasks_database_id}/query",
                headers=self.notion_headers,
                payload=payload,
            )
            rows.extend(self._notion_task(row) for row in data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return rows

    def _list_notion_projects(self) -> dict[str, dict[str, str]]:
        projects: dict[str, dict[str, str]] = {}
        cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = request_json(
                "POST",
                f"https://api.notion.com/v1/databases/{self.projects_database_id}/query",
                headers=self.notion_headers,
                payload=payload,
            )
            for row in data.get("results", []):
                name = _plain_title(row.get("properties", {}).get("Project"))
                if name:
                    projects[_normalize_title(name)] = {
                        "id": row.get("id", ""),
                        "name": name,
                        "stream_id": _plain_relation_id(row.get("properties", {}).get("Stream")) or "",
                    }
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return projects

    def _list_notion_streams(self) -> dict[str, dict[str, str]]:
        if not self.streams_database_id:
            return {}
        streams: dict[str, dict[str, str]] = {}
        cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = request_json(
                "POST",
                f"https://api.notion.com/v1/databases/{self.streams_database_id}/query",
                headers=self.notion_headers,
                payload=payload,
            )
            for row in data.get("results", []):
                name = _plain_title(row.get("properties", {}).get("Направление"))
                if name:
                    streams[_normalize_title(name)] = {"id": row.get("id", ""), "name": name}
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return streams

    def _ensure_todoist_project_labels(self, projects: dict[str, dict[str, str]]) -> int:
        existing = {_normalize_title(label.get("name")) for label in self.todoist.list_labels()}
        created = 0
        for key, project in projects.items():
            if key in existing:
                continue
            self.todoist.create_label(project["name"])
            existing.add(key)
            created += 1
        return created

    def _ensure_todoist_stream_sections(
        self,
        streams: dict[str, dict[str, str]],
    ) -> tuple[str, dict[str, str], int]:
        inbox = next((project for project in self.todoist.list_projects() if project.get("inbox_project")), None)
        if not inbox:
            raise RuntimeError("Todoist Inbox project was not found")
        inbox_id = str(inbox["id"])
        sections = {
            _normalize_title(section.get("name")): str(section["id"])
            for section in self.todoist.list_sections()
            if str(section.get("project_id")) == inbox_id
        }
        created = 0
        for key, stream in streams.items():
            if key in sections:
                continue
            section_id = self.todoist.create_section(stream["name"], inbox_id)
            sections[key] = str(section_id)
            created += 1
        other_key = _normalize_title(OTHER_SECTION_NAME)
        if other_key not in sections:
            section_id = self.todoist.create_section(OTHER_SECTION_NAME, inbox_id)
            sections[other_key] = str(section_id)
            created += 1
        return inbox_id, sections, created

    def _enrich_missing_notion_routing(
        self,
        notion_tasks: list[dict[str, Any]],
        todoist_tasks: dict[str, dict[str, Any]],
        projects: dict[str, dict[str, str]],
        streams: dict[str, dict[str, str]],
    ) -> None:
        for task in notion_tasks:
            todoist_task = todoist_tasks.get(task.get("todoist_id", ""))
            if not todoist_task:
                continue
            labels = [_normalize_title(label) for label in todoist_task.get("labels") or []]
            properties: dict[str, Any] = {}
            if not task.get("project_id"):
                project = next((projects[label] for label in labels if label in projects), None)
                if project:
                    task["project_id"] = project["id"]
                    properties["Проект"] = _relation(project["id"])
            if not task.get("stream_id"):
                stream = next((streams[label] for label in labels if label in streams), None)
                if stream:
                    task["stream_id"] = stream["id"]
                    properties["Stream"] = _relation(stream["id"])
            if properties:
                request_json(
                    "PATCH",
                    f"https://api.notion.com/v1/pages/{task['page_id']}",
                    headers=self.notion_headers,
                    payload={"properties": properties},
                )

    @staticmethod
    def _attach_notion_routing(
        notion_tasks: list[dict[str, Any]],
        projects: dict[str, dict[str, str]],
        streams: dict[str, dict[str, str]],
        inbox_project_id: str,
        sections: dict[str, str],
    ) -> None:
        projects_by_id = {project["id"]: project for project in projects.values()}
        stream_names_by_id = {stream["id"]: stream["name"] for stream in streams.values()}
        for task in notion_tasks:
            project = projects_by_id.get(task.get("project_id"), {})
            task["project_name"] = project.get("name", "")
            stream_id = task.get("stream_id")
            task["stream_name"] = stream_names_by_id.get(stream_id, "")
            task["inbox_project_id"] = inbox_project_id
            section_name = task["stream_name"] or OTHER_SECTION_NAME
            task["section_id"] = sections.get(_normalize_title(section_name))

    def _find_notion_by_todoist_id(self, todoist_id: str) -> dict[str, Any] | None:
        data = request_json(
            "POST",
            f"https://api.notion.com/v1/databases/{self.tasks_database_id}/query",
            headers=self.notion_headers,
            payload={"filter": {"property": "Todoist ID", "rich_text": {"equals": todoist_id}}, "page_size": 1},
        )
        rows = data.get("results", [])
        return self._notion_task(rows[0]) if rows else None

    def _create_notion_from_todoist(
        self,
        task: dict[str, Any],
        projects: dict[str, dict[str, str]] | None = None,
        streams: dict[str, dict[str, str]] | None = None,
        sections: dict[str, str] | None = None,
    ) -> None:
        properties = _notion_properties_from_todoist(task)
        properties.update(_notion_routing_from_todoist(task, projects or {}, streams or {}, sections or {}))
        properties["Todoist ID"] = _rich_text(str(task["id"]))
        properties["Sync status"] = _select("Synced")
        properties["Source"] = _select("Todoist")
        request_json(
            "POST",
            "https://api.notion.com/v1/pages",
            headers=self.notion_headers,
            payload={"parent": {"database_id": self.tasks_database_id}, "properties": properties},
        )

    def _update_notion_from_todoist(
        self,
        page_id: str,
        task: dict[str, Any],
        projects: dict[str, dict[str, str]] | None = None,
        streams: dict[str, dict[str, str]] | None = None,
        sections: dict[str, str] | None = None,
    ) -> None:
        properties = _notion_properties_from_todoist(task)
        properties.update(_notion_routing_from_todoist(task, projects or {}, streams or {}, sections or {}))
        properties["Sync status"] = _select("Synced")
        request_json(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=self.notion_headers,
            payload={"properties": properties},
        )

    def _set_notion_todoist_id(self, page_id: str, todoist_id: str, status: str) -> None:
        request_json(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=self.notion_headers,
            payload={"properties": {"Todoist ID": _rich_text(todoist_id), "Sync status": _select(status)}},
        )

    def _mark_notion_sync(self, page_id: str, status: str) -> None:
        request_json(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=self.notion_headers,
            payload={"properties": {"Sync status": _select(status)}},
        )

    def _update_notion_status(self, page_id: str, status: str) -> None:
        request_json(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=self.notion_headers,
            payload={"properties": {"Статус": _status(status), "Sync status": _select("Synced")}},
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _notion_task(row: dict[str, Any]) -> dict[str, Any]:
        props = row.get("properties", {})
        return {
            "page_id": row.get("id", ""),
            "title": _plain_title(props.get("Task")),
            "description": "",
            "status": _plain_select(props.get("Статус")) or "Backlog",
            "priority": _strategic_to_priority(_plain_select(props.get("Strategic Impact"))),
            "due_date": _plain_date(props.get("Срок выполнения")),
            "deadline": _plain_date(props.get("Deadline")),
            "todoist_id": _plain_rich_text(props.get("Todoist ID")),
            "project_id": _plain_relation_id(props.get("Проект")),
            "stream_id": _plain_relation_id(props.get("Stream")),
            "last_edited_time": row.get("last_edited_time", ""),
        }


class TaskSyncLoop:
    def __init__(self, service: TaskSyncService, interval_seconds: int):
        self.service = service
        self.interval_seconds = max(interval_seconds, 30)
        self._thread: threading.Thread | None = None

    def start(self, *, sync_on_start: bool = True) -> None:
        if not self.service.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._run, args=(sync_on_start,), daemon=True, name="todoist-sync")
        self._thread.start()

    def _run(self, sync_on_start: bool) -> None:
        if not sync_on_start:
            time.sleep(self.interval_seconds)
        while True:
            try:
                print(f"Todoist sync: {self.service.sync()}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"Todoist sync failed: {exc}", flush=True)
            time.sleep(self.interval_seconds)


def _notion_properties_from_todoist(
    task: dict[str, Any],
) -> dict[str, Any]:
    due = task.get("due") or {}
    deadline = task.get("deadline") or {}
    properties = {
        "Task": _title(str(task.get("content") or "Без названия")),
        "Статус": _status("Done" if task.get("is_completed") else "Backlog"),
        "Strategic Impact": _select(_priority_to_strategic(todoist_priority(task.get("priority")))),
        "Срок выполнения": _date(due.get("date")),
        "Deadline": _date(deadline.get("date")),
    }
    return properties


def _notion_routing_from_todoist(
    task: dict[str, Any],
    projects: dict[str, dict[str, str]],
    streams: dict[str, dict[str, str]],
    sections: dict[str, str],
) -> dict[str, Any]:
    labels = [_normalize_title(label) for label in task.get("labels") or []]
    project = next((projects[label] for label in labels if label in projects), None)
    section_id = str(task.get("section_id") or "")
    section_name = next((name for name, item_id in sections.items() if str(item_id) == section_id), "")
    stream = streams.get(section_name)
    return {
        "Проект": _relation(project["id"]) if project else {"relation": []},
        "Stream": _relation(stream["id"]) if stream else {"relation": []},
    }


def _title(value: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": value[:2000]}}]}


def _rich_text(value: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": value[:2000]}}]}


def _select(value: str | None) -> dict[str, Any]:
    return {"select": {"name": value}} if value else {"select": None}


def _status(value: str) -> dict[str, Any]:
    return {"status": {"name": value}}


def _date(value: str | None) -> dict[str, Any]:
    return {"date": {"start": value}} if value else {"date": None}


def _relation(page_id: str) -> dict[str, Any]:
    return {"relation": [{"id": page_id}]}


def _plain_title(prop: dict[str, Any] | None) -> str:
    return "".join(item.get("plain_text", "") for item in (prop or {}).get("title", []))


def _plain_rich_text(prop: dict[str, Any] | None) -> str:
    return "".join(item.get("plain_text", "") for item in (prop or {}).get("rich_text", []))


def _plain_select(prop: dict[str, Any] | None) -> str:
    value = (prop or {}).get("select") or (prop or {}).get("status")
    return value.get("name", "") if value else ""


def _plain_date(prop: dict[str, Any] | None) -> str | None:
    value = (prop or {}).get("date")
    return value.get("start") if value else None


def _plain_relation_id(prop: dict[str, Any] | None) -> str | None:
    relation = (prop or {}).get("relation") or []
    return relation[0].get("id") if relation else None


def _strategic_to_priority(value: str) -> str:
    try:
        score = int(value)
    except (TypeError, ValueError):
        return "P4"
    if score >= 9:
        return "P1"
    if score >= 7:
        return "P2"
    if score >= 4:
        return "P3"
    return "P4"


def _priority_to_strategic(value: str) -> str:
    return {"P1": "10", "P2": "8", "P3": "5", "P4": "2"}.get(value, "2")


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _fingerprint(value: dict[str, Any]) -> str:
    if "page_id" in value:
        relevant = {
            "title": value.get("title"),
            "status": value.get("status"),
            "priority": value.get("priority"),
            "due_date": value.get("due_date"),
            "deadline": value.get("deadline"),
            "todoist_id": value.get("todoist_id"),
            "project_name": value.get("project_name"),
            "stream_name": value.get("stream_name"),
        }
    else:
        relevant = {
            "content": value.get("content"),
            "description": value.get("description"),
            "priority": value.get("priority"),
            "due": value.get("due"),
            "deadline": value.get("deadline"),
            "is_completed": value.get("is_completed"),
            "labels": value.get("labels"),
            "project_id": value.get("project_id"),
            "section_id": value.get("section_id"),
        }
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _match_key_notion(task: dict[str, Any]) -> tuple[str, str]:
    return (_normalize_title(task.get("title")), str(task.get("due_date") or ""))


def _match_key_todoist(task: dict[str, Any]) -> tuple[str, str]:
    due = task.get("due") or {}
    return (_normalize_title(task.get("content")), str(due.get("date") or ""))


def _normalize_title(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())
