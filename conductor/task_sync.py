from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .http import HttpError, request_json
from .todoist_client import TodoistClient, todoist_priority

NOTION_VERSION = "2022-06-28"
MANAGED_TODOIST_LABELS = {
    "встреча",
    "звонок",
    "письмо",
    "сообщение",
    "документ",
    "анализ",
    "исследование",
    "планирование",
    "низкая_энергия",
    "средняя_энергия",
    "высокая_энергия",
    "пятиминутное_дело",
}


@dataclass
class SyncResult:
    notion_to_todoist: int = 0
    todoist_to_notion: int = 0
    completed: int = 0
    labels_created: int = 0
    sections_created: int = 0
    mode: str = "observe"
    inventory: dict[str, int] | None = None
    planned: dict[str, int] | None = None
    snapshot_path: str = ""
    errors: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "notion_to_todoist": self.notion_to_todoist,
            "todoist_to_notion": self.todoist_to_notion,
            "completed": self.completed,
            "labels_created": self.labels_created,
            "sections_created": self.sections_created,
            "mode": self.mode,
            "inventory": self.inventory or {},
            "planned": self.planned or {},
            "snapshot_path": self.snapshot_path,
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
        paused: bool = False,
        mode: str = "observe",
        allow_project_create: bool = False,
        allow_task_create: bool = False,
        allow_task_move: bool = False,
        allow_label_write: bool = False,
        allow_status_write: bool = False,
        allow_missing_cancel: bool = False,
        max_task_moves: int = 10,
        snapshot_path: str = "data/todoist_inventory_snapshot.json",
    ):
        self.notion_token = notion_token
        self.tasks_database_id = tasks_database_id
        self.projects_database_id = projects_database_id
        self.todoist = todoist
        self.state_path = Path(state_path)
        self.completed_since = completed_since
        self.streams_database_id = streams_database_id
        self.paused = paused
        self.mode = mode if mode in {"observe", "projects", "write"} else "observe"
        self.allow_project_create = allow_project_create
        self.allow_task_create = allow_task_create
        self.allow_task_move = allow_task_move
        self.allow_label_write = allow_label_write
        self.allow_status_write = allow_status_write
        self.allow_missing_cancel = allow_missing_cancel
        self.max_task_moves = max(max_task_moves, 0)
        self.snapshot_path = Path(snapshot_path)
        self.report_path = self.snapshot_path.with_name(f"{self.snapshot_path.stem}_report.json")
        self._task_moves = 0
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
            not self.paused
            and self.notion_token
            and self.tasks_database_id
            and self.projects_database_id
            and self.todoist.enabled
            and self.todoist.api_token
        )

    def sync(self) -> dict[str, Any]:
        result = SyncResult(errors=[], mode=self.mode)
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
            todoist_projects = self.todoist.list_projects()
            sections = self.todoist.list_sections()
            labels = self.todoist.list_labels()
            active_tasks = self.todoist.list_tasks()
            result.inventory = {
                "notion_tasks": len(notion_tasks),
                "todoist_active_tasks": len(active_tasks),
                "notion_projects": len(projects),
                "todoist_projects": len(todoist_projects),
                "notion_streams": len(streams),
                "todoist_sections": len(sections),
                "todoist_labels": len(labels),
            }
            result.planned = self._migration_plan(
                notion_tasks,
                projects,
                streams,
                todoist_projects,
                sections,
                active_tasks,
            )
            result.snapshot_path = str(
                self._save_inventory_snapshot(
                    notion_tasks,
                    projects,
                    streams,
                    todoist_projects,
                    sections,
                    labels,
                    active_tasks,
                    result.planned,
                )
            )
            self._save_inventory_report(
                result.inventory,
                result.planned,
                projects,
                streams,
                todoist_projects,
                sections,
                active_tasks,
            )
            if self.mode == "observe":
                return result.as_dict()

            self._ensure_todoist_project_hierarchy(projects, streams, todoist_projects, result)
            if self.mode == "projects":
                return result.as_dict()
            todoist_projects = self.todoist.list_projects()
            self._attach_notion_routing(notion_tasks, projects, streams, todoist_projects, sections)
            meta = state.get("__meta__", {})
            history_imported = bool(meta.get("completed_history_imported"))
            completed_since = (
                (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                if history_imported
                else self.completed_since
            )
            completed_history_loaded = False
            try:
                completed_tasks = self.todoist.list_completed_tasks(completed_since)
                completed_history_loaded = True
            except Exception as exc:  # noqa: BLE001
                completed_tasks = []
                result.errors.append(f"Could not load completed Todoist tasks: {exc}")
            print(
                "Todoist sync inventory: "
                f"notion={len(notion_tasks)}, active={len(active_tasks)}, completed={len(completed_tasks)}, "
                f"completed_since={completed_since}",
                flush=True,
            )
            todoist_tasks = {str(task["id"]): task for task in [*active_tasks, *completed_tasks]}
            linked_ids = {task["todoist_id"] for task in notion_tasks if task["todoist_id"]}
            self._link_existing_matches(notion_tasks, todoist_tasks, linked_ids)
            for notion_task in notion_tasks:
                try:
                    self._sync_notion_task(
                        notion_task,
                        todoist_tasks,
                        state,
                        result,
                        projects,
                        streams,
                        todoist_projects,
                        sections,
                        allow_missing_todoist_resolution=completed_history_loaded,
                    )
                    self._persist_sync_state_to_notion(notion_task, state.get(notion_task["page_id"], {}))
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"{notion_task['title']}: {exc}")
                    self._mark_notion_sync(notion_task["page_id"], "Error", str(exc))

            for todoist_id, todoist_task in todoist_tasks.items():
                if todoist_id in linked_ids:
                    continue
                try:
                    self._create_notion_from_todoist(todoist_task, projects, streams, todoist_projects, sections)
                    result.todoist_to_notion += 1
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"Todoist {todoist_id}: {exc}")
            state["__meta__"] = {
                **meta,
                "last_successful_sync": datetime.now(timezone.utc).isoformat(),
                "completed_history_imported": history_imported or completed_history_loaded,
            }
            self._save_state(state)
            return result.as_dict()
        finally:
            self._lock.release()

    def bootstrap_projects(self) -> dict[str, Any]:
        result = SyncResult(errors=[], mode="projects")
        if not self.enabled:
            result.errors.append("Todoist sync is not configured")
            return result.as_dict()
        if not self._lock.acquire(blocking=False):
            result.errors.append("Todoist sync is already running")
            return result.as_dict()
        try:
            streams = self._list_notion_streams()
            projects = self._list_notion_projects()
            todoist_projects = self.todoist.list_projects()
            previous = self.allow_project_create
            self.allow_project_create = True
            try:
                self._ensure_todoist_project_hierarchy(projects, streams, todoist_projects, result)
            finally:
                self.allow_project_create = previous
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
        if self.mode != "write":
            return {"ignored": True, "reason": f"sync is in {self.mode} mode"}
        # Webhooks and periodic reconciliation share the same state file.
        # Serialize them so a webhook cannot be overwritten by an older sync snapshot.
        with self._lock:
            return self._handle_todoist_event(event)

    def _handle_todoist_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event_name = str(event.get("event_name") or "")
        data = event.get("event_data") or {}
        task_id = str(data.get("id") or "")
        if not task_id:
            return {"ignored": True, "reason": "missing task id"}
        notion_task = self._find_notion_by_todoist_id(task_id)
        if event_name == "item:deleted":
            if notion_task and self.allow_missing_cancel:
                self._update_notion_status(notion_task["page_id"], "Cancelled")
                self._forget_state(notion_task["page_id"])
            return {"ok": True, "action": "cancelled_in_notion" if self.allow_missing_cancel else "deletion_observed"}
        if event_name == "item:completed":
            if notion_task and self.allow_status_write:
                self._update_notion_status(notion_task["page_id"], "Done")
                self._forget_state(notion_task["page_id"])
            return {"ok": True, "action": "completed_in_notion" if self.allow_status_write else "completion_observed"}
        if event_name == "item:uncompleted":
            if notion_task and self.allow_status_write:
                self._update_notion_status(notion_task["page_id"], "Backlog")
                self._forget_state(notion_task["page_id"])
            return {"ok": True, "action": "reopened_in_notion" if self.allow_status_write else "reopen_observed"}
        if event_name in {"item:added", "item:updated"}:
            projects = self._list_notion_projects()
            streams = self._list_notion_streams()
            todoist_projects = self.todoist.list_projects()
            sections = self.todoist.list_sections()
            # Todoist webhook payloads can omit routing fields such as section_id
            # and labels. Always load the complete task before updating Notion,
            # otherwise a partial event can clear routing and move the task to
            # ПРОЧЕЕ during the next reconciliation.
            task = self.todoist.get_task(task_id)
            if notion_task:
                self._update_notion_from_todoist(
                    notion_task["page_id"],
                    task,
                    projects,
                    streams,
                    todoist_projects,
                    sections,
                    current_status=notion_task.get("status"),
                )
            else:
                self._create_notion_from_todoist(task, projects, streams, todoist_projects, sections)
            if notion_task:
                _apply_todoist_snapshot_to_notion(notion_task, task, projects, streams, todoist_projects, sections)
                self._remember_state(notion_task, task)
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
        todoist_projects: list[dict[str, Any]] | None = None,
        sections: list[dict[str, Any]] | None = None,
        allow_missing_todoist_resolution: bool = True,
    ) -> None:
        projects = projects or {}
        streams = streams or {}
        todoist_projects = todoist_projects or []
        sections = sections or []
        todoist_id = notion_task["todoist_id"]
        if not todoist_id:
            if notion_task["status"] in {"Done", "Cancelled"}:
                self._mark_notion_sync(notion_task["page_id"], "Synced")
                state[notion_task["page_id"]] = {
                    "notion": _fingerprint(notion_task),
                    "todoist": "",
                    "todoist_id": "",
                }
                return
            if not self.allow_task_create:
                raise RuntimeError("Task creation is blocked by TODOIST_ALLOW_TASK_CREATE=false")
            create_payload = dict(notion_task)
            if not self.allow_label_write:
                create_payload.pop("managed_labels", None)
            if not self.allow_task_move:
                create_payload.pop("todoist_project_id", None)
                create_payload.pop("todoist_section_id", None)
            created_id = self.todoist.create_task(create_payload)
            self._set_notion_todoist_id(notion_task["page_id"], str(created_id), "Synced")
            notion_task["todoist_id"] = str(created_id)
            state[notion_task["page_id"]] = {
                "notion": _fingerprint(notion_task),
                "todoist": "",
                "todoist_id": str(created_id),
            }
            result.notion_to_todoist += 1
            return

        todoist_task = todoist_tasks.get(todoist_id)
        if not todoist_task:
            if not allow_missing_todoist_resolution:
                raise RuntimeError("Could not verify missing Todoist task because completed history was unavailable")
            if notion_task["status"] in {"Done", "Cancelled"}:
                self._mark_notion_sync(notion_task["page_id"], "Synced")
                state[notion_task["page_id"]] = {
                    "notion": _fingerprint(notion_task),
                    "todoist": "",
                    "todoist_id": todoist_id,
                }
                return
            # Todoist is the primary task interface. A linked task that no
            # longer exists there must not be resurrected from an old Notion
            # record. Historical completed tasks are loaded before this point;
            # anything still missing is treated as deleted.
            if not self.allow_missing_cancel:
                raise RuntimeError("Missing Todoist task requires manual review; automatic cancellation is disabled")
            self._update_notion_status(notion_task["page_id"], "Cancelled")
            notion_task["status"] = "Cancelled"
            state[notion_task["page_id"]] = {
                "notion": _fingerprint(notion_task),
                "todoist": "",
                "todoist_id": todoist_id,
            }
            result.todoist_to_notion += 1
            return

        prior = state.get(notion_task["page_id"]) or _notion_stored_state(notion_task)
        notion_fingerprint = _fingerprint(notion_task)
        todoist_fingerprint = _fingerprint(todoist_task)
        notion_changed = notion_fingerprint != prior.get("notion")
        todoist_changed = todoist_fingerprint != prior.get("todoist")
        if not prior:
            # A missing baseline is ambiguous. Todoist is the primary task UI,
            # so bootstrap every mapped field from Todoist without touching it.
            self._update_notion_from_todoist(
                notion_task["page_id"],
                todoist_task,
                projects,
                streams,
                todoist_projects,
                sections,
                current_status=notion_task.get("status"),
            )
            _apply_todoist_snapshot_to_notion(notion_task, todoist_task, projects, streams, todoist_projects, sections)
            result.todoist_to_notion += 1
            state[notion_task["page_id"]] = _state_entry(notion_task, todoist_task)
            return

        if todoist_changed and not notion_changed:
            self._update_notion_from_todoist(
                notion_task["page_id"],
                todoist_task,
                projects,
                streams,
                todoist_projects,
                sections,
                current_status=notion_task.get("status"),
            )
            _apply_todoist_snapshot_to_notion(notion_task, todoist_task, projects, streams, todoist_projects, sections)
            result.todoist_to_notion += 1
        elif notion_changed and not todoist_changed:
            self._update_todoist_from_notion(notion_task, todoist_task)
            _apply_notion_snapshot_to_todoist(todoist_task, notion_task)
            self._mark_notion_sync(notion_task["page_id"], "Synced")
            result.notion_to_todoist += 1
        elif notion_changed and todoist_changed:
            if _parse_time(todoist_task.get("updated_at") or todoist_task.get("added_at")) >= _parse_time(
                notion_task["last_edited_time"]
            ):
                self._update_notion_from_todoist(
                    notion_task["page_id"],
                    todoist_task,
                    projects,
                    streams,
                    todoist_projects,
                    sections,
                    current_status=notion_task.get("status"),
                )
                _apply_todoist_snapshot_to_notion(notion_task, todoist_task, projects, streams, todoist_projects, sections)
                result.todoist_to_notion += 1
            else:
                self._update_todoist_from_notion(notion_task, todoist_task)
                _apply_notion_snapshot_to_todoist(todoist_task, notion_task)
                self._mark_notion_sync(notion_task["page_id"], "Synced")
                result.notion_to_todoist += 1
        elif notion_task.get("sync_status") != "Synced" or notion_task.get("sync_error"):
            self._mark_notion_sync(notion_task["page_id"], "Synced")
        state[notion_task["page_id"]] = _state_entry(notion_task, todoist_task)

    def _update_todoist_from_notion(self, notion_task: dict[str, Any], todoist_task: dict[str, Any]) -> None:
        todoist_id = notion_task["todoist_id"]
        notion_completed = notion_task.get("status") in {"Done", "Cancelled"}
        todoist_completed = bool(todoist_task.get("is_completed"))
        if notion_completed:
            if not todoist_completed:
                if not self.allow_status_write:
                    raise RuntimeError("Closing Todoist tasks is blocked by TODOIST_ALLOW_STATUS_WRITE=false")
                self.todoist.close_task(todoist_id)
            return
        if todoist_completed:
            if not self.allow_status_write:
                raise RuntimeError("Reopening Todoist tasks is blocked by TODOIST_ALLOW_STATUS_WRITE=false")
            self.todoist.reopen_task(todoist_id)
        base_fields = dict(notion_task)
        base_fields.pop("managed_labels", None)
        base_fields.pop("todoist_project_id", None)
        base_fields.pop("todoist_section_id", None)
        self.todoist.update_task(todoist_id, base_fields)
        self._enforce_todoist_routing(notion_task, todoist_task)

    def _enforce_todoist_routing(self, notion_task: dict[str, Any], todoist_task: dict[str, Any]) -> None:
        if todoist_task.get("is_completed"):
            return
        existing_labels = list(todoist_task.get("labels") or [])
        unmanaged_labels = [label for label in existing_labels if not _is_managed_label(label)]
        expected_labels = sorted(set([*unmanaged_labels, *notion_task.get("managed_labels", [])]))
        if sorted(existing_labels) != expected_labels:
            if not self.allow_label_write:
                raise RuntimeError("Todoist label changes are blocked by TODOIST_ALLOW_LABEL_WRITE=false")
            self.todoist.update_task_labels(notion_task["todoist_id"], expected_labels)
            todoist_task["labels"] = expected_labels
        expected_project = notion_task.get("todoist_project_id")
        expected_section = notion_task.get("todoist_section_id")
        if expected_project and (
            str(todoist_task.get("project_id")) != expected_project
            or str(todoist_task.get("section_id") or "") != str(expected_section or "")
        ):
            if not self.allow_task_move:
                raise RuntimeError("Todoist task moves are blocked by TODOIST_ALLOW_TASK_MOVE=false")
            if self._task_moves >= self.max_task_moves:
                raise RuntimeError(f"Todoist task move limit reached ({self.max_task_moves})")
            self.todoist.update_task_location(
                notion_task["todoist_id"],
                expected_project,
                expected_section,
            )
            self._task_moves += 1
            todoist_task["project_id"] = expected_project
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
                        "todoist_project_id": _plain_rich_text(
                            row.get("properties", {}).get("Todoist Project ID")
                        ),
                        "sync_enabled": _plain_checkbox(
                            row.get("properties", {}).get("Синхронизировать с Todoist")
                        ),
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
                    streams[_normalize_title(name)] = {
                        "id": row.get("id", ""),
                        "name": name,
                        "todoist_project_id": _plain_rich_text(
                            row.get("properties", {}).get("Todoist Parent Project ID")
                        ),
                    }
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return streams

    def _migration_plan(
        self,
        notion_tasks: list[dict[str, Any]],
        projects: dict[str, dict[str, str]],
        streams: dict[str, dict[str, str]],
        todoist_projects: list[dict[str, Any]],
        sections: list[dict[str, Any]],
        active_tasks: list[dict[str, Any]],
    ) -> dict[str, int]:
        todoist_project_ids = {str(project.get("id")) for project in todoist_projects}
        mapped_projects = {
            str(project.get("todoist_project_id"))
            for project in projects.values()
            if str(project.get("todoist_project_id") or "") in todoist_project_ids
        }
        mapped_streams = {
            str(stream.get("todoist_project_id"))
            for stream in streams.values()
            if str(stream.get("todoist_project_id") or "") in todoist_project_ids
        }
        project_by_notion_id = {project["id"]: project for project in projects.values()}
        sections_by_id = {str(section.get("id")): section for section in sections}
        active_by_id = {str(task.get("id")): task for task in active_tasks}
        planned_moves = 0
        invalid_sections = 0
        for task in notion_tasks:
            project = project_by_notion_id.get(str(task.get("project_id") or ""))
            target_project_id = str((project or {}).get("todoist_project_id") or "")
            target_section_id = str(task.get("todoist_section_id") or "")
            section = sections_by_id.get(target_section_id)
            if target_section_id and (not section or str(section.get("project_id")) != target_project_id):
                invalid_sections += 1
            todoist_task = active_by_id.get(str(task.get("todoist_id") or ""))
            if todoist_task and target_project_id and (
                str(todoist_task.get("project_id") or "") != target_project_id
                or (
                    target_section_id
                    and str(todoist_task.get("section_id") or "") != target_section_id
                )
            ):
                planned_moves += 1
        return {
            "unmapped_notion_projects": sum(
                1 for project in projects.values() if not project.get("todoist_project_id")
            ),
            "projects_eligible_for_creation": sum(
                1
                for project in projects.values()
                if not project.get("todoist_project_id")
            ),
            "mapped_notion_projects": len(mapped_projects),
            "unmapped_streams": sum(1 for stream in streams.values() if not stream.get("todoist_project_id")),
            "mapped_streams": len(mapped_streams),
            "todoist_tasks_in_unmapped_projects": sum(
                1 for task in active_tasks if str(task.get("project_id") or "") not in mapped_projects
            ),
            "notion_tasks_without_todoist_id": sum(1 for task in notion_tasks if not task.get("todoist_id")),
            "notion_tasks_with_invalid_section": invalid_sections,
            "planned_task_moves": planned_moves,
        }

    def _save_inventory_snapshot(
        self,
        notion_tasks: list[dict[str, Any]],
        projects: dict[str, dict[str, str]],
        streams: dict[str, dict[str, str]],
        todoist_projects: list[dict[str, Any]],
        sections: list[dict[str, Any]],
        labels: list[dict[str, Any]],
        active_tasks: list[dict[str, Any]],
        planned: dict[str, int],
    ) -> Path:
        snapshot = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "planned": planned,
            "notion": {
                "tasks": notion_tasks,
                "projects": list(projects.values()),
                "streams": list(streams.values()),
            },
            "todoist": {
                "projects": todoist_projects,
                "sections": sections,
                "labels": labels,
                "active_tasks": active_tasks,
            },
        }
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.snapshot_path

    def _save_inventory_report(
        self,
        inventory: dict[str, int],
        planned: dict[str, int],
        projects: dict[str, dict[str, str]],
        streams: dict[str, dict[str, str]],
        todoist_projects: list[dict[str, Any]],
        sections: list[dict[str, Any]],
        active_tasks: list[dict[str, Any]],
    ) -> Path:
        task_counts: dict[str, int] = {}
        section_counts: dict[str, int] = {}
        for task in active_tasks:
            project_id = str(task.get("project_id") or "")
            task_counts[project_id] = task_counts.get(project_id, 0) + 1
        for section in sections:
            project_id = str(section.get("project_id") or "")
            section_counts[project_id] = section_counts.get(project_id, 0) + 1

        todoist_by_normalized_name: dict[str, list[dict[str, Any]]] = {}
        for todoist_project in todoist_projects:
            todoist_by_normalized_name.setdefault(
                _normalize_title(todoist_project.get("name")),
                [],
            ).append(todoist_project)

        def candidates(name: str, *, root_only: bool = False) -> list[dict[str, str]]:
            matches = todoist_by_normalized_name.get(_normalize_title(name), [])
            if root_only:
                matches = [match for match in matches if not match.get("parent_id")]
            return [
                {
                    "id": str(match.get("id") or ""),
                    "name": str(match.get("name") or ""),
                    "parent_id": str(match.get("parent_id") or ""),
                }
                for match in matches
            ]

        report = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "inventory": inventory,
            "planned": planned,
            "notion_projects": [
                {
                    "notion_id": project.get("id", ""),
                    "name": project.get("name", ""),
                    "stream_id": project.get("stream_id", ""),
                    "todoist_project_id": project.get("todoist_project_id", ""),
                    "sync_enabled": bool(project.get("sync_enabled")),
                    "exact_todoist_candidates": candidates(project.get("name", "")),
                }
                for project in projects.values()
            ],
            "notion_streams": [
                {
                    "notion_id": stream.get("id", ""),
                    "name": stream.get("name", ""),
                    "todoist_project_id": stream.get("todoist_project_id", ""),
                    "exact_todoist_candidates": candidates(stream.get("name", ""), root_only=True),
                }
                for stream in streams.values()
            ],
            "todoist_projects": [
                {
                    "id": str(project.get("id") or ""),
                    "name": str(project.get("name") or ""),
                    "parent_id": str(project.get("parent_id") or ""),
                    "active_task_count": task_counts.get(str(project.get("id") or ""), 0),
                    "section_count": section_counts.get(str(project.get("id") or ""), 0),
                }
                for project in todoist_projects
            ],
        }
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.report_path

    def read_inventory_report(self) -> dict[str, Any]:
        if not self.report_path.exists():
            return {"available": False}
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        return {"available": True, **report}

    def _ensure_todoist_project_hierarchy(
        self,
        projects: dict[str, dict[str, str]],
        streams: dict[str, dict[str, str]],
        todoist_projects: list[dict[str, Any]],
        result: SyncResult,
    ) -> None:
        if not self.allow_project_create:
            return
        todoist_by_id = {str(project.get("id")): project for project in todoist_projects}
        todoist_by_name_parent = {
            (_normalize_title(project.get("name")), str(project.get("parent_id") or "")): project
            for project in todoist_projects
        }
        stream_by_id = {stream["id"]: stream for stream in streams.values()}
        for project in projects.values():
            if project.get("todoist_project_id"):
                continue
            stream = stream_by_id.get(str(project.get("stream_id") or ""))
            parent_id = str((stream or {}).get("todoist_project_id") or "")
            if stream and not parent_id:
                existing_parent = todoist_by_name_parent.get((_normalize_title(stream["name"]), ""))
                parent_id = str(existing_parent.get("id")) if existing_parent else str(
                    self.todoist.create_project(stream["name"]) or ""
                )
                if parent_id:
                    stream["todoist_project_id"] = parent_id
                    self._set_notion_external_id(stream["id"], "Todoist Parent Project ID", parent_id)
                    if parent_id not in todoist_by_id:
                        result.sections_created += 1
            existing = todoist_by_name_parent.get((_normalize_title(project["name"]), parent_id))
            todoist_project_id = str(existing.get("id")) if existing else str(
                self.todoist.create_project(project["name"], parent_id or None) or ""
            )
            if todoist_project_id:
                project["todoist_project_id"] = todoist_project_id
                self._set_notion_external_id(project["id"], "Todoist Project ID", todoist_project_id)
                result.labels_created += 1

    def _set_notion_external_id(self, page_id: str, property_name: str, value: str) -> None:
        request_json(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=self.notion_headers,
            payload={"properties": {property_name: _rich_text(value)}},
        )

    @staticmethod
    def _attach_notion_routing(
        notion_tasks: list[dict[str, Any]],
        projects: dict[str, dict[str, str]],
        streams: dict[str, dict[str, str]],
        todoist_projects: list[dict[str, Any]],
        sections: list[dict[str, Any]],
    ) -> None:
        projects_by_id = {project["id"]: project for project in projects.values()}
        stream_names_by_id = {stream["id"]: stream["name"] for stream in streams.values()}
        todoist_project_ids = {str(project.get("id")) for project in todoist_projects}
        sections_by_id = {str(section.get("id")): section for section in sections}
        for task in notion_tasks:
            project = projects_by_id.get(task.get("project_id"), {})
            task["project_name"] = project.get("name", "")
            stream_id = project.get("stream_id") or ""
            task["stream_id"] = stream_id
            task["stream_name"] = stream_names_by_id.get(stream_id, "")
            todoist_project_id = str(project.get("todoist_project_id") or "")
            task["todoist_project_id"] = todoist_project_id if todoist_project_id in todoist_project_ids else ""
            section_id = str(task.get("todoist_section_id") or "")
            section = sections_by_id.get(section_id)
            task["todoist_section_id"] = (
                section_id if section and str(section.get("project_id")) == task["todoist_project_id"] else ""
            )

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
        todoist_projects: list[dict[str, Any]] | None = None,
        sections: list[dict[str, Any]] | None = None,
    ) -> None:
        properties = _notion_properties_from_todoist(task)
        if not self.allow_status_write:
            properties.pop("Статус", None)
        properties.update(
            _notion_routing_from_todoist(
                task,
                projects or {},
                streams or {},
                todoist_projects or [],
                sections or [],
            )
        )
        properties["Todoist ID"] = _rich_text(str(task["id"]))
        properties["Sync status"] = _select("Synced")
        properties["Sync error"] = _rich_text("")
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
        todoist_projects: list[dict[str, Any]] | None = None,
        sections: list[dict[str, Any]] | None = None,
        current_status: str | None = None,
    ) -> None:
        properties = _notion_properties_from_todoist(task, current_status=current_status)
        if not self.allow_status_write:
            properties.pop("Статус", None)
        properties.update(
            _notion_routing_from_todoist(
                task,
                projects or {},
                streams or {},
                todoist_projects or [],
                sections or [],
            )
        )
        properties["Sync status"] = _select("Synced")
        properties["Sync error"] = _rich_text("")
        request_json(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=self.notion_headers,
            payload={"properties": properties},
        )

    def _update_notion_routing_from_todoist(
        self,
        page_id: str,
        task: dict[str, Any],
        projects: dict[str, dict[str, str]],
        streams: dict[str, dict[str, str]],
        todoist_projects: list[dict[str, Any]],
        sections: list[dict[str, Any]],
    ) -> None:
        properties = _notion_routing_from_todoist(task, projects, streams, todoist_projects, sections)
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
            payload={
                "properties": {
                    "Todoist ID": _rich_text(todoist_id),
                    "Sync status": _select(status),
                    "Sync error": _rich_text(""),
                }
            },
        )

    def _mark_notion_sync(self, page_id: str, status: str, error: str = "") -> None:
        request_json(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=self.notion_headers,
            payload={"properties": {"Sync status": _select(status), "Sync error": _rich_text(error)}},
        )

    def _update_notion_status(self, page_id: str, status: str) -> None:
        request_json(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=self.notion_headers,
            payload={
                "properties": {
                    "Статус": _status(status),
                    "Sync status": _select("Synced"),
                    "Sync error": _rich_text(""),
                }
            },
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _remember_state(self, notion_task: dict[str, Any], todoist_task: dict[str, Any]) -> None:
        state = self._load_state()
        state[notion_task["page_id"]] = _state_entry(notion_task, todoist_task)
        self._save_state(state)

    def _forget_state(self, page_id: str) -> None:
        state = self._load_state()
        state.pop(page_id, None)
        self._save_state(state)

    def _persist_sync_state_to_notion(self, notion_task: dict[str, Any], entry: dict[str, str]) -> None:
        notion_hash = str(entry.get("notion") or "")
        todoist_hash = str(entry.get("todoist") or "")
        if (
            notion_hash == notion_task.get("sync_notion_hash", "")
            and todoist_hash == notion_task.get("sync_todoist_hash", "")
        ):
            return
        request_json(
            "PATCH",
            f"https://api.notion.com/v1/pages/{notion_task['page_id']}",
            headers=self.notion_headers,
            payload={
                "properties": {
                    "Sync Notion hash": _rich_text(notion_hash),
                    "Sync Todoist hash": _rich_text(todoist_hash),
                }
            },
        )
        notion_task["sync_notion_hash"] = notion_hash
        notion_task["sync_todoist_hash"] = todoist_hash

    @staticmethod
    def _notion_task(row: dict[str, Any]) -> dict[str, Any]:
        props = row.get("properties", {})
        return {
            "page_id": row.get("id", ""),
            "title": _plain_title(props.get("Task")),
            "description": _plain_rich_text(props.get("Описание")),
            "status": _plain_select(props.get("Статус")) or "Backlog",
            "priority": _strategic_to_priority(_plain_select(props.get("Strategic Impact"))),
            "due_date": _plain_date(props.get("Срок выполнения")),
            "deadline": _plain_date(props.get("Deadline")),
            "todoist_id": _plain_rich_text(props.get("Todoist ID")),
            "sync_status": _plain_select(props.get("Sync status")),
            "sync_error": _plain_rich_text(props.get("Sync error")),
            "sync_notion_hash": _plain_rich_text(props.get("Sync Notion hash")),
            "sync_todoist_hash": _plain_rich_text(props.get("Sync Todoist hash")),
            "project_id": _plain_relation_id(props.get("Проект")),
            "stream_id": _plain_relation_id(props.get("Stream")),
            "section_name": _plain_rich_text(props.get("Раздел")),
            "todoist_section_id": _plain_rich_text(props.get("Todoist Section ID")),
            "managed_labels": _plain_multi_select(props.get("Метки Todoist")),
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
            except HttpError as exc:
                retry_after = _retry_after(exc)
                print(f"Todoist sync failed: {exc}", flush=True)
                time.sleep(retry_after)
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"Todoist sync failed: {exc}", flush=True)
            time.sleep(self.interval_seconds)


def _notion_properties_from_todoist(
    task: dict[str, Any],
    current_status: str | None = None,
) -> dict[str, Any]:
    due = task.get("due") or {}
    deadline = task.get("deadline") or {}
    status = "Done" if task.get("is_completed") else _active_notion_status(current_status)
    properties = {
        "Task": _title(str(task.get("content") or "Без названия")),
        "Описание": _rich_text(str(task.get("description") or "")),
        "Статус": _status(status),
        "Strategic Impact": _select(_priority_to_strategic(todoist_priority(task.get("priority")))),
        "Срок выполнения": _date(due.get("date")),
        "Deadline": _date(deadline.get("date")),
        "Метки Todoist": _multi_select(_managed_labels(task.get("labels") or [])),
    }
    return properties


def _notion_routing_from_todoist(
    task: dict[str, Any],
    projects: dict[str, dict[str, str]],
    streams: dict[str, dict[str, str]],
    todoist_projects: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    project_id, stream_id, section_name, section_id = _routing_ids_from_todoist(
        task,
        projects,
        streams,
        todoist_projects,
        sections,
    )
    return {
        "Проект": _relation(project_id) if project_id else {"relation": []},
        "Stream": _relation(stream_id) if stream_id else {"relation": []},
        "Раздел": _rich_text(section_name),
        "Todoist Section ID": _rich_text(section_id),
    }


def _routing_ids_from_todoist(
    task: dict[str, Any],
    projects: dict[str, dict[str, str]],
    streams: dict[str, dict[str, str]],
    todoist_projects: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> tuple[str | None, str | None, str, str]:
    todoist_project_ids = {str(item.get("id")) for item in todoist_projects}
    todoist_project_id = str(task.get("project_id") or "")
    project = next(
        (
            item
            for item in projects.values()
            if str(item.get("todoist_project_id") or "") == todoist_project_id
            and todoist_project_id in todoist_project_ids
        ),
        None,
    )
    stream_id = str((project or {}).get("stream_id") or "")
    section_id = str(task.get("section_id") or "")
    section = next(
        (
            item
            for item in sections
            if str(item.get("id")) == section_id and str(item.get("project_id")) == todoist_project_id
        ),
        None,
    )
    return (
        project["id"] if project else None,
        stream_id or None,
        str((section or {}).get("name") or ""),
        section_id if section else "",
    )


def _todoist_routing_differs(
    notion_task: dict[str, Any],
    todoist_task: dict[str, Any],
    projects: dict[str, dict[str, str]],
    streams: dict[str, dict[str, str]],
    todoist_projects: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> bool:
    project_id, stream_id, section_name, section_id = _routing_ids_from_todoist(
        todoist_task, projects, streams, todoist_projects, sections
    )
    return (notion_task.get("project_id") or "") != (project_id or "") or (
        notion_task.get("stream_id") or ""
    ) != (stream_id or "") or (notion_task.get("section_name") or "") != section_name or (
        notion_task.get("todoist_section_id") or ""
    ) != section_id


def _project_stream_section(
    task: dict[str, Any],
    projects: dict[str, dict[str, str]],
    streams: dict[str, dict[str, str]],
    sections: dict[str, str],
) -> str | None:
    # Kept as a compatibility shim for callers from older deployments.
    # New routing never moves a task based on a project label or stream section.
    return None


def _title(value: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": value[:2000]}}]}


def _rich_text(value: str) -> dict[str, Any]:
    if not value:
        return {"rich_text": []}
    return {"rich_text": [{"type": "text", "text": {"content": value[:2000]}}]}


def _select(value: str | None) -> dict[str, Any]:
    return {"select": {"name": value}} if value else {"select": None}


def _multi_select(values: list[str]) -> dict[str, Any]:
    return {"multi_select": [{"name": value} for value in values]}


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


def _plain_multi_select(prop: dict[str, Any] | None) -> list[str]:
    return [str(item.get("name") or "") for item in (prop or {}).get("multi_select", []) if item.get("name")]


def _plain_checkbox(prop: dict[str, Any] | None) -> bool:
    return bool((prop or {}).get("checkbox"))


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


def _active_notion_status(value: str | None) -> str:
    return value if value in {"Backlog", "Next", "Waiting", "In Progress"} else "Backlog"


def _state_entry(notion_task: dict[str, Any], todoist_task: dict[str, Any]) -> dict[str, str]:
    return {
        "notion": _fingerprint(notion_task),
        "todoist": _fingerprint(todoist_task),
        "todoist_id": str(todoist_task.get("id") or notion_task.get("todoist_id") or ""),
    }


def _notion_stored_state(notion_task: dict[str, Any]) -> dict[str, str]:
    notion_hash = str(notion_task.get("sync_notion_hash") or "")
    todoist_hash = str(notion_task.get("sync_todoist_hash") or "")
    if not notion_hash and not todoist_hash:
        return {}
    return {
        "notion": notion_hash,
        "todoist": todoist_hash,
        "todoist_id": str(notion_task.get("todoist_id") or ""),
    }


def _apply_todoist_snapshot_to_notion(
    notion_task: dict[str, Any],
    todoist_task: dict[str, Any],
    projects: dict[str, dict[str, str]],
    streams: dict[str, dict[str, str]],
    todoist_projects: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> None:
    due = todoist_task.get("due") or {}
    deadline = todoist_task.get("deadline") or {}
    project_id, stream_id, section_name, section_id = _routing_ids_from_todoist(
        todoist_task, projects, streams, todoist_projects, sections
    )
    notion_task.update(
        {
            "title": str(todoist_task.get("content") or "Без названия"),
            "description": str(todoist_task.get("description") or ""),
            "status": "Done" if todoist_task.get("is_completed") else _active_notion_status(notion_task.get("status")),
            "priority": todoist_priority(todoist_task.get("priority")),
            "due_date": due.get("date"),
            "deadline": deadline.get("date"),
            "project_id": project_id,
            "stream_id": stream_id,
            "section_name": section_name,
            "todoist_section_id": section_id,
            "managed_labels": _managed_labels(todoist_task.get("labels") or []),
            "project_name": next(
                (project["name"] for project in projects.values() if project["id"] == project_id),
                "",
            ),
            "stream_name": next(
                (stream["name"] for stream in streams.values() if stream["id"] == stream_id),
                "",
            ),
        }
    )


def _apply_notion_snapshot_to_todoist(todoist_task: dict[str, Any], notion_task: dict[str, Any]) -> None:
    todoist_task.update(
        {
            "content": notion_task.get("title") or "Без названия",
            "description": notion_task.get("description") or "",
            "priority": {"P1": 4, "P2": 3, "P3": 2, "P4": 1}.get(notion_task.get("priority"), 1),
            "due": {"date": notion_task["due_date"]} if notion_task.get("due_date") else None,
            "deadline": {"date": notion_task["deadline"]} if notion_task.get("deadline") else None,
            "is_completed": notion_task.get("status") in {"Done", "Cancelled"},
        }
    )
    _apply_notion_routing_snapshot_to_todoist(todoist_task, notion_task)


def _apply_notion_routing_snapshot_to_todoist(todoist_task: dict[str, Any], notion_task: dict[str, Any]) -> None:
    existing = list(todoist_task.get("labels") or [])
    unmanaged = [label for label in existing if not _is_managed_label(label)]
    todoist_task["labels"] = sorted(set([*unmanaged, *notion_task.get("managed_labels", [])]))
    if notion_task.get("todoist_project_id"):
        todoist_task["project_id"] = notion_task["todoist_project_id"]
        todoist_task["section_id"] = notion_task.get("todoist_section_id")


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _retry_after(exc: HttpError) -> int:
    if exc.status != 429:
        return 300
    try:
        body = json.loads(exc.body)
        return max(int(body.get("error_extra", {}).get("retry_after") or 300), 300)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 300


def _fingerprint(value: dict[str, Any]) -> str:
    if "page_id" in value:
        relevant = {
            "title": value.get("title"),
            "description": value.get("description"),
            "status": value.get("status"),
            "priority": value.get("priority"),
            "due_date": value.get("due_date"),
            "deadline": value.get("deadline"),
            "todoist_id": value.get("todoist_id"),
            "project_name": value.get("project_name"),
            "stream_name": value.get("stream_name"),
            "section_name": value.get("section_name"),
            "todoist_section_id": value.get("todoist_section_id"),
            "managed_labels": value.get("managed_labels"),
        }
    else:
        relevant = {
            "content": value.get("content"),
            "description": value.get("description"),
            "priority": value.get("priority"),
            "due_date": (value.get("due") or {}).get("date"),
            "deadline": (value.get("deadline") or {}).get("date"),
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


def _managed_labels(labels: list[Any]) -> list[str]:
    return sorted(
        {
            _normalize_title(label).removeprefix("@")
            for label in labels
            if _is_managed_label(label)
        }
    )


def _is_managed_label(label: Any) -> bool:
    return _normalize_title(label).removeprefix("@") in MANAGED_TODOIST_LABELS
