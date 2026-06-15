from __future__ import annotations

import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, parse, request


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    timeout: int = 60,
) -> Any:
    if query:
        encoded = parse.urlencode({key: value for key, value in query.items() if value is not None})
        url = f"{url}{'&' if '?' in url else '?'}{encoded}"
    body = None
    req_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    raw = ""
    for attempt in range(5):
        req = request.Request(url, data=body, method=method, headers=req_headers)
        try:
            with request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
            break
        except error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            if exc.code != 429 or attempt == 4:
                raise HttpError(exc.code, response_body) from exc
            try:
                retry_after = float(exc.headers.get("Retry-After") or 1)
            except (TypeError, ValueError):
                retry_after = 1
            time.sleep(max(retry_after, 0.1))
    if not raw:
        return {}
    return json.loads(raw)


def request_bytes(url: str, *, headers: dict[str, str] | None = None, timeout: int = 60) -> bytes:
    req = request.Request(url, method="GET", headers=headers or {})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except error.HTTPError as exc:
        raise HttpError(exc.code, exc.read().decode("utf-8", errors="replace")) from exc


def request_multipart(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
    timeout: int = 120,
) -> dict[str, Any]:
    boundary = f"----conductor-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode())
        chunks.append(b"\r\n")

    for name, (filename, data, content_type) in files.items():
        guessed = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{Path(filename).name}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {guessed}\r\n\r\n".encode())
        chunks.append(data)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    req_headers = dict(headers or {})
    req_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = request.Request(url, data=body, method="POST", headers=req_headers)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        raise HttpError(exc.code, exc.read().decode("utf-8", errors="replace")) from exc
    return json.loads(raw) if raw else {}


def url_join(base: str, path: str) -> str:
    return parse.urljoin(base.rstrip("/") + "/", path.lstrip("/"))
