from __future__ import annotations

import base64
import hashlib
import hmac
import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import get_settings
from .service import ConductorService
from .task_sync import TaskSyncLoop
from .telegram import extract_message, extract_text_and_file


settings = get_settings()
service = ConductorService(settings)
sync_loop = TaskSyncLoop(service.task_sync, settings.todoist_sync_interval_seconds)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(
                200,
                {
                    "ok": True,
                    "todoist_sync_enabled": service.task_sync.enabled,
                    "todoist_sync_mode": service.task_sync.mode,
                    "todoist_sync_summary": service.task_sync.read_inventory_summary(),
                },
            )
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/tasks/sync":
            self._handle_manual_sync()
            return
        if self.path == "/todoist/webhook":
            self._handle_todoist_webhook()
            return
        if self.path != "/telegram/webhook":
            self._json(404, {"error": "not found"})
            return
        if settings.telegram_webhook_secret:
            got = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if got != settings.telegram_webhook_secret:
                self._json(401, {"error": "bad secret"})
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            update = json.loads(body)
            result = handle_update(update)
            self._json(200, result)
        except Exception as exc:  # noqa: BLE001 - Telegram must get a response, not a dropped connection.
            print("Unhandled webhook error:", repr(exc), flush=True)
            traceback.print_exc()
            self._json(200, {"ok": False, "error": str(exc)})

    def _handle_manual_sync(self) -> None:
        if not settings.task_sync_secret:
            self._json(503, {"error": "task sync secret is not configured"})
            return
        if self.headers.get("X-Conductor-Sync-Secret", "") != settings.task_sync_secret:
            self._json(401, {"error": "bad sync secret"})
            return
        try:
            self._json(200, {"ok": True, **service.task_sync.sync()})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": str(exc)})

    def _handle_todoist_webhook(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not settings.todoist_webhook_secret:
            self._json(503, {"error": "Todoist webhook secret is not configured"})
            return
        signature = self.headers.get("X-Todoist-Hmac-SHA256", "")
        digest = hmac.new(settings.todoist_webhook_secret.encode(), raw, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode()
        if not hmac.compare_digest(signature, expected):
            self._json(401, {"error": "bad Todoist signature"})
            return
        try:
            event = json.loads(raw.decode("utf-8"))
            self._json(200, service.task_sync.handle_todoist_event(event))
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def handle_update(update: dict[str, Any]) -> dict[str, Any]:
    message = extract_message(update)
    if not message:
        return {"ok": True, "ignored": True}
    chat_id = int(message["chat"]["id"])
    text, file_info = extract_text_and_file(message)

    if file_info and file_info.get("kind") in {"voice", "audio"}:
        file_path, data = service.telegram.get_file_bytes(file_info["file_id"])
        content_type = file_info.get("mime_type") or "audio/ogg"
        return service.process_audio(file_path, data, content_type=content_type, chat_id=chat_id)

    if not text.strip():
        service.telegram.send_message(chat_id, "Пока MVP обрабатывает текст и голос. Для фото/документов добавь подпись текстом.")
        return {"ok": True, "message": "unsupported without text"}

    return service.process_text(text, chat_id=chat_id)


def main() -> None:
    sync_loop.start(sync_on_start=settings.todoist_sync_on_start)
    server = ThreadingHTTPServer((settings.host, settings.port), Handler)
    print(f"Conductor listening on http://{settings.host}:{settings.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
