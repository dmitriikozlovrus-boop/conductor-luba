from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .http import request_json
from .models import TaskItem

API_BASE = "https://api.todoist.com/api/v1"


class TodoistClient:
    def __init__(self, api_token: str, enabled: bool):
        self.api_token = api_token
        self.enabled = enabled

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}

    def list_tasks(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        tasks: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = request_json(
                "GET",
                f"{API_BASE}/tasks",
                headers=self.headers,
                query={"cursor": cursor, "limit": 200},
            )
            if isinstance(response, list):
                tasks.extend(response)
                break
            tasks.extend(response.get("results", []))
            cursor = response.get("next_cursor")
            if not cursor:
                break
        return tasks

    def list_labels(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        labels: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = request_json(
                "GET",
                f"{API_BASE}/labels",
                headers=self.headers,
                query={"cursor": cursor, "limit": 200},
            )
            if isinstance(response, list):
                labels.extend(response)
                break
            labels.extend(response.get("results", []))
            cursor = response.get("next_cursor")
            if not cursor:
                break
        return labels

    def create_label(self, name: str) -> str | None:
        if not self.enabled:
            return None
        response = request_json(
            "POST",
            f"{API_BASE}/labels",
            headers=self.headers,
            payload={"name": name},
        )
        return response.get("id")

    def list_projects(self) -> list[dict[str, Any]]:
        return self._list_resource("projects")

    def create_project(self, name: str, parent_id: str | None = None) -> str | None:
        payload: dict[str, Any] = {"name": name}
        if parent_id:
            payload["parent_id"] = parent_id
        response = request_json("POST", f"{API_BASE}/projects", headers=self.headers, payload=payload)
        return response.get("id")

    def update_project(self, project_id: str, *, name: str | None = None, parent_id: str | None = None) -> None:
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if parent_id is not None:
            payload["parent_id"] = parent_id
        if payload:
            request_json("POST", f"{API_BASE}/projects/{project_id}", headers=self.headers, payload=payload)

    def list_sections(self) -> list[dict[str, Any]]:
        return self._list_resource("sections")

    def create_section(self, name: str, project_id: str) -> str | None:
        response = request_json(
            "POST",
            f"{API_BASE}/sections",
            headers=self.headers,
            payload={"name": name, "project_id": project_id},
        )
        return response.get("id")

    def update_task_location(self, task_id: str, project_id: str, section_id: str | None) -> None:
        args: dict[str, Any] = {"id": task_id}
        if section_id:
            args["section_id"] = section_id
        else:
            args["project_id"] = project_id
        command_uuid = str(uuid4())
        response = request_json(
            "POST",
            f"{API_BASE}/sync",
            headers=self.headers,
            payload={"commands": [{"type": "item_move", "uuid": command_uuid, "args": args}]},
        )
        status = (response.get("sync_status") or {}).get(command_uuid)
        if status != "ok":
            raise RuntimeError(f"Todoist could not move task {task_id}: {status}")

    def update_task_routing_batch(self, changes: list[dict[str, Any]]) -> None:
        commands: list[dict[str, Any]] = []
        for change in changes:
            task_id = str(change["id"])
            labels_uuid = str(uuid4())
            commands.append(
                {
                    "type": "item_update",
                    "uuid": labels_uuid,
                    "args": {
                        "id": task_id,
                        "labels": change.get("labels") or [],
                        "content": change.get("content") or "Без названия",
                        "description": change.get("description") or "",
                        "priority": _priority(str(change.get("priority") or "")),
                    },
                }
            )
            move_uuid = str(uuid4())
            move_args: dict[str, Any] = {"id": task_id}
            if change.get("section_id"):
                move_args["section_id"] = change["section_id"]
            else:
                move_args["project_id"] = change["project_id"]
            commands.append({"type": "item_move", "uuid": move_uuid, "args": move_args})
        for start in range(0, len(commands), 100):
            batch = commands[start : start + 100]
            response = request_json(
                "POST",
                f"{API_BASE}/sync",
                headers=self.headers,
                payload={"commands": batch},
            )
            statuses = response.get("sync_status") or {}
            failures = {command["uuid"]: statuses.get(command["uuid"]) for command in batch if statuses.get(command["uuid"]) != "ok"}
            if failures:
                raise RuntimeError(f"Todoist could not update task routing: {failures}")

    def list_completed_tasks(self, since: str) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        start = _as_datetime(since)
        now = datetime.now(timezone.utc)
        tasks: list[dict[str, Any]] = []
        while start < now:
            end = min(start + timedelta(days=89), now + timedelta(seconds=1))
            cursor: str | None = None
            while True:
                response = request_json(
                    "GET",
                    f"{API_BASE}/tasks/completed/by_completion_date",
                    headers=self.headers,
                    query={
                        "since": start.isoformat().replace("+00:00", "Z"),
                        "until": end.isoformat().replace("+00:00", "Z"),
                        "cursor": cursor,
                        "limit": 200,
                    },
                )
                page = response.get("items", [])
                for task in page:
                    task["is_completed"] = True
                    task["updated_at"] = task.get("completed_at") or task.get("added_at")
                tasks.extend(page)
                cursor = response.get("next_cursor")
                if not cursor:
                    break
            start = end
        return tasks

    def get_task(self, task_id: str) -> dict[str, Any]:
        return request_json("GET", f"{API_BASE}/tasks/{task_id}", headers=self.headers)

    def create_task(self, item: TaskItem | dict[str, Any]) -> str | None:
        if not self.enabled:
            return None
        payload = _task_payload(item)
        response = request_json(
            "POST",
            f"{API_BASE}/tasks",
            headers=self.headers,
            payload=payload,
        )
        return response.get("id")

    def update_task(self, task_id: str, item: dict[str, Any]) -> None:
        request_json(
            "POST",
            f"{API_BASE}/tasks/{task_id}",
            headers=self.headers,
            payload=_task_payload(item),
        )

    def update_task_labels(self, task_id: str, labels: list[str]) -> None:
        request_json(
            "POST",
            f"{API_BASE}/tasks/{task_id}",
            headers=self.headers,
            payload={"labels": labels},
        )

    def close_task(self, task_id: str) -> None:
        request_json("POST", f"{API_BASE}/tasks/{task_id}/close", headers=self.headers)

    def reopen_task(self, task_id: str) -> None:
        request_json("POST", f"{API_BASE}/tasks/{task_id}/reopen", headers=self.headers)

    def delete_task(self, task_id: str) -> None:
        request_json("DELETE", f"{API_BASE}/tasks/{task_id}", headers=self.headers)

    def _list_resource(self, resource: str) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = request_json(
                "GET",
                f"{API_BASE}/{resource}",
                headers=self.headers,
                query={"cursor": cursor, "limit": 200},
            )
            if isinstance(response, list):
                items.extend(response)
                break
            items.extend(response.get("results", []))
            cursor = response.get("next_cursor")
            if not cursor:
                break
        return items


def _task_payload(item: TaskItem | dict[str, Any]) -> dict[str, Any]:
    if isinstance(item, TaskItem):
        title = item.title
        description = item.description
        priority = item.priority
        due_date = item.due_date
        deadline = None
    else:
        title = str(item.get("title") or "Без названия")
        description = str(item.get("description") or "")
        priority = str(item.get("priority") or "")
        due_date = item.get("due_date")
        deadline = item.get("deadline")
    payload: dict[str, Any] = {
        "content": title,
        "description": description,
        "priority": _priority(priority),
    }
    if due_date:
        payload["due_date"] = due_date
    if deadline:
        payload["deadline_date"] = deadline
    labels = item.get("labels") if isinstance(item, dict) else None
    if isinstance(item, dict) and "managed_labels" in item:
        labels = item.get("managed_labels") or []
    if labels is not None:
        payload["labels"] = labels
    if isinstance(item, dict) and item.get("todoist_project_id"):
        payload["project_id"] = item["todoist_project_id"]
        if item.get("todoist_section_id"):
            payload["section_id"] = item["todoist_section_id"]
    return payload


def _priority(value: str) -> int:
    return {"P1": 4, "P2": 3, "P3": 2, "P4": 1}.get(value, 1)


def todoist_priority(value: int | str | None) -> str:
    try:
        number = int(value or 1)
    except (TypeError, ValueError):
        number = 1
    return {4: "P1", 3: "P2", 2: "P3"}.get(number, "P4")


def _as_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime(2007, 1, 1, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
