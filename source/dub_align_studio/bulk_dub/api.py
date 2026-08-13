"""HTTP API 路由：所有路径以 /api/bulk_dub/ 前缀，避免与"一键成片"路由冲突。"""

from __future__ import annotations

import json
import time
from typing import Any

from ..engines.edge_tts import EDGE_STYLES, EDGE_VOICES
from .csv_export import to_bytes as csv_to_bytes
from .service import DEFAULT_SPEED, DEFAULT_VOICE_ID, BulkDubService, get_service

API_PREFIX = "/api/bulk_dub"


def _json_response(payload: Any, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


def dispatch_get(path: str, query: dict[str, str],
                 service: BulkDubService | None = None) -> tuple[bool, int, bytes, str]:
    if not path.startswith(API_PREFIX):
        return False, 404, b"", ""
    svc = service or get_service()
    route = path[len(API_PREFIX):]

    if route == "/voices":
        return True, *_json_response({
            "voices": [dict(v) for v in EDGE_VOICES],
            "styles": list(EDGE_STYLES),
            "default_voice_id": DEFAULT_VOICE_ID,
            "default_speed": DEFAULT_SPEED,
        })

    if route == "/summary":
        batch_id = query.get("batch_id") or None
        return True, *_json_response(svc.summary(batch_id))

    if route == "/tasks":
        batch_id = query.get("batch_id") or None
        status = query.get("status") or None
        excel_row = int(query["excel_row"]) if query.get("excel_row") else None
        q = query.get("q") or None
        try:
            limit = max(1, min(500, int(query.get("limit") or 100)))
            offset = max(0, int(query.get("offset") or 0))
        except ValueError:
            limit, offset = 100, 0
        rows = svc.list_tasks(batch_id=batch_id, status=status,
                               excel_row=excel_row, query=q,
                               limit=limit, offset=offset)
        counts = svc.store.count_by_status(batch_id)
        return True, *_json_response({
            "tasks": rows, "limit": limit, "offset": offset,
            "counts": counts,
        })

    if route == "/changes":
        since = float(query.get("since") or 0)
        batch_id = query.get("batch_id") or None
        return True, *_json_response({
            "changes": svc.changed_since(since, batch_id),
            "now": time.time(),
        })

    if route == "/csv":
        batch_id = query.get("batch_id") or None
        rows_iter = svc.store.iter_all(batch_id=batch_id)
        body = csv_to_bytes(rows_iter)
        return True, 200, body, "text/csv; charset=utf-8"

    if route == "/encoder_probe":
        snap = svc.summary()
        return True, *_json_response(snap.get("scheduler", {}).get("encoder", {}))

    return True, *_json_response({"error": "not found"}, 404)


def dispatch_post(path: str, query: dict[str, str], body: bytes,
                  content_type: str = "",
                  service: BulkDubService | None = None) -> tuple[bool, int, bytes, str]:
    if not path.startswith(API_PREFIX):
        return False, 404, b"", ""
    svc = service or get_service()
    route = path[len(API_PREFIX):]

    if route == "/preview":
        xlsx = _extract_xlsx_body(body, content_type)
        check_exists = str(query.get("check_exists", "1")) not in ("0", "false", "False")
        try:
            payload = svc.preview_excel(xlsx, check_exists=check_exists)
        except ValueError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        return True, *_json_response(payload)

    if route == "/start":
        xlsx = _extract_xlsx_body(body, content_type)
        try:
            result = svc.start_batch(
                source_bytes=xlsx,
                label=query.get("label") or "",
                output_dir=query.get("output_dir") or "",
                voice_id=query.get("voice_id") or DEFAULT_VOICE_ID,
                speed=float(query.get("speed") or DEFAULT_SPEED),
                pitch=int(query.get("pitch") or 0),
                style=query.get("style") or "general",
                keep_original_audio=str(query.get("keep_original_audio", "0")) in ("1", "true", "True"),
                zoom_percent=int(query.get("zoom_percent") or 130),
                tts_concurrency=int(query.get("tts_concurrency") or 4),
                video_concurrency=int(query.get("video_concurrency") or 0),
                encoder_preference=query.get("encoder_preference") or "auto",
                check_exists=str(query.get("check_exists", "1")) not in ("0", "false", "False"),
            )
        except ValueError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        return True, *_json_response(result)

    if route == "/pause":
        svc.pause()
        return True, *_json_response({"paused": True})
    if route == "/resume":
        svc.resume()
        return True, *_json_response({"paused": False})

    if route == "/cancel_task":
        task_id = query.get("task_id") or _read_json(body).get("task_id")
        if not task_id:
            return True, *_json_response({"error": "缺少 task_id"}, 400)
        ok = svc.cancel_task(task_id)
        return True, *_json_response({"cancelled": ok})

    if route == "/cancel_waiting":
        batch_id = query.get("batch_id") or _read_json(body).get("batch_id") or ""
        n = svc.cancel_waiting(batch_id) if batch_id else 0
        return True, *_json_response({"cancelled": n})

    if route == "/retry_failed":
        batch_id = query.get("batch_id") or _read_json(body).get("batch_id") or ""
        only = str(query.get("only_retryable", "0")) in ("1", "true", "True")
        n = svc.retry_failed(batch_id, only) if batch_id else 0
        return True, *_json_response({"retried": n})

    if route == "/try_voice":
        voice_id = query.get("voice_id") or DEFAULT_VOICE_ID
        speed = float(query.get("speed") or DEFAULT_SPEED)
        try:
            out = svc.probe_sample(voice_id=voice_id, speed=speed)
        except Exception as exc:  # noqa: BLE001
            return True, *_json_response({"error": str(exc)}, 502)
        return True, *_json_response({"audio_path": str(out)})

    return True, *_json_response({"error": "not found"}, 404)


def _extract_xlsx_body(body: bytes, content_type: str) -> bytes:
    if body and content_type and content_type.startswith("multipart/form-data"):
        try:
            return _parse_multipart_file(body, content_type)
        except Exception:
            return body
    return body


def _parse_multipart_file(body: bytes, content_type: str) -> bytes:
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part.split("=", 1)[1].strip().strip('"')
            break
    if not boundary:
        return body
    marker = ("--" + boundary).encode()
    parts = body.split(marker)
    for chunk in parts:
        if not chunk or chunk == b"--\r\n" or chunk.startswith(b"--"):
            continue
        header_end = chunk.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers = chunk[:header_end].decode("utf-8", "ignore").lower()
        data = chunk[header_end + 4:]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        if 'name="file"' in headers or 'name="xlsx"' in headers:
            return data
    return body


def _read_json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        v = json.loads(body.decode("utf-8", "ignore"))
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
