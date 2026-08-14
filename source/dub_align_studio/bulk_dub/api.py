"""HTTP API 路由：所有路径以 /api/bulk_dub/ 前缀。

R11 修复：
    R11-5 /changes 用复合游标 (updated_at, task_id)；返回 next_cursor + has_more
    R11-8 /csv 通过 web_server 层流式；这里只提供 iter_csv_chunks 引用
    R11-11 参数校验严格：batch_id / task_id 存在性；multipart 明确失败 400
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..engines.edge_tts import EDGE_STYLES, EDGE_VOICES, edge_tts_endpoint
from .excel_reader import (
    MAX_XLSX_UPLOAD_BYTES, ExcelSizeError,
)
from .service import (
    DEFAULT_SPEED, DEFAULT_VOICE_ID, BulkDubService, ValidationError, get_service,
)
from .store import ALL_STATUSES, is_safe_id


API_PREFIX = "/api/bulk_dub"
MAX_JSON_BODY_BYTES = 256 * 1024


def _json_response(payload: Any, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


def _safe_batch_id(v: str) -> bool:
    return bool(v and is_safe_id(v))


def _safe_task_id(v: str) -> bool:
    return bool(v and is_safe_id(v))


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
            "edge_endpoint": edge_tts_endpoint(),
            "edge_endpoint_configured": bool(edge_tts_endpoint()),
        })

    if route == "/summary":
        batch_id = query.get("batch_id") or None
        if batch_id:
            if not _safe_batch_id(batch_id):
                return True, *_json_response({"error": "非法 batch_id"}, 400)
            if not svc.store.batch_exists(batch_id):
                return True, *_json_response({"error": f"batch 不存在：{batch_id}"}, 404)
        return True, *_json_response(svc.summary(batch_id))

    if route == "/batches":
        try:
            limit = max(1, min(100, int(query.get("limit") or 50)))
        except ValueError:
            return True, *_json_response({"error": "非法 limit"}, 400)
        return True, *_json_response({"batches": svc.list_batches(limit)})

    if route == "/tasks":
        batch_id = query.get("batch_id") or None
        if batch_id:
            if not _safe_batch_id(batch_id):
                return True, *_json_response({"error": "非法 batch_id"}, 400)
            if not svc.store.batch_exists(batch_id):
                return True, *_json_response({"error": f"batch 不存在：{batch_id}"}, 404)
        status = query.get("status") or None
        if status and status not in ALL_STATUSES:
            return True, *_json_response({"error": f"未知 status：{status}"}, 400)
        excel_row = None
        if query.get("excel_row"):
            try:
                excel_row = int(query["excel_row"])
            except ValueError:
                return True, *_json_response({"error": "非法 excel_row"}, 400)
        q = query.get("q") or None
        try:
            limit = max(1, min(500, int(query.get("limit") or 100)))
            offset = max(0, int(query.get("offset") or 0))
        except ValueError:
            return True, *_json_response({"error": "非法 limit/offset"}, 400)
        rows = svc.list_tasks(batch_id=batch_id, status=status,
                               excel_row=excel_row, query=q,
                               limit=limit, offset=offset)
        counts = svc.store.count_by_status(batch_id)
        return True, *_json_response({
            "tasks": rows, "limit": limit, "offset": offset,
            "counts": counts,
        })

    if route == "/changes":
        cursor = query.get("cursor")
        if cursor is None and query.get("since"):
            try:
                since = float(query.get("since") or 0)
                cursor = f"{since:.9f}|"
            except ValueError:
                return True, *_json_response({"error": "非法 since"}, 400)
        batch_id = query.get("batch_id") or None
        if batch_id:
            if not _safe_batch_id(batch_id):
                return True, *_json_response({"error": "非法 batch_id"}, 400)
            if not svc.store.batch_exists(batch_id):
                return True, *_json_response({"error": f"batch 不存在：{batch_id}"}, 404)
        try:
            limit = max(1, min(500, int(query.get("limit") or 500)))
        except ValueError:
            return True, *_json_response({"error": "非法 limit"}, 400)
        payload = svc.changed_since_cursor(cursor, batch_id, limit)
        payload["now"] = time.time()
        return True, *_json_response(payload)

    if route == "/encoder_probe":
        snap = svc.summary()
        return True, *_json_response(snap.get("scheduler", {}).get("encoder", {}))

    if route == "/csv":
        # CSV 由 web_server 直接流式发送——这里只做参数校验
        batch_id = query.get("batch_id") or None
        if batch_id:
            if not _safe_batch_id(batch_id):
                return True, *_json_response({"error": "非法 batch_id"}, 400)
            if not svc.store.batch_exists(batch_id):
                return True, *_json_response({"error": f"batch 不存在：{batch_id}"}, 404)
        return True, 200, b"__STREAM_CSV__", "text/csv; charset=utf-8"

    return True, *_json_response({"error": "not found"}, 404)


def dispatch_post(path: str, query: dict[str, str], body: bytes,
                  content_type: str = "",
                  service: BulkDubService | None = None) -> tuple[bool, int, bytes, str]:
    if not path.startswith(API_PREFIX):
        return False, 404, b"", ""
    svc = service or get_service()
    route = path[len(API_PREFIX):]

    is_xlsx_upload = route in ("/preview", "/start")
    max_bytes = MAX_XLSX_UPLOAD_BYTES if is_xlsx_upload else MAX_JSON_BODY_BYTES
    if body and len(body) > max_bytes:
        return True, *_json_response(
            {"error": f"上传体积过大：{len(body)} > {max_bytes}"}, 413,
        )

    if route == "/preview":
        try:
            xlsx = _extract_xlsx_body(body, content_type)
        except ValueError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        check_exists = str(query.get("check_exists", "1")) not in ("0", "false", "False")
        try:
            payload = svc.preview_excel(xlsx, check_exists=check_exists)
        except ExcelSizeError as exc:
            return True, *_json_response({"error": str(exc)}, 413)
        except (ValueError, ValidationError) as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        return True, *_json_response(payload)

    if route == "/start":
        try:
            xlsx = _extract_xlsx_body(body, content_type)
        except ValueError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        try:
            speed = float(query.get("speed") or DEFAULT_SPEED)
            pitch = int(query.get("pitch") or 0)
            zoom = int(query.get("zoom_percent") or 130)
            tts_c = int(query.get("tts_concurrency") or 4)
            video_c = int(query.get("video_concurrency") or 0)
        except ValueError as exc:
            return True, *_json_response({"error": f"参数不是数字：{exc}"}, 400)
        # R12-12：HTTP 层**不接受** require_endpoint——生产始终要求 Endpoint。
        # 测试通过 svc._skip_endpoint_check=True 或直接注入 Mock backend 跳过。
        try:
            result = svc.start_batch(
                source_bytes=xlsx,
                label=query.get("label") or "",
                output_dir=query.get("output_dir") or "",
                voice_id=query.get("voice_id") or DEFAULT_VOICE_ID,
                speed=speed, pitch=pitch,
                style=query.get("style") or "general",
                keep_original_audio=str(query.get("keep_original_audio", "0")) in ("1", "true", "True"),
                zoom_percent=zoom,
                tts_concurrency=tts_c,
                video_concurrency=video_c,
                encoder_preference=query.get("encoder_preference") or "auto",
                check_exists=str(query.get("check_exists", "1")) not in ("0", "false", "False"),
            )
        except ExcelSizeError as exc:
            return True, *_json_response({"error": str(exc)}, 413)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        except ValueError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        return True, *_json_response(result)

    if route == "/resize_pools":
        try:
            tts = int(query["tts"]) if query.get("tts") else None
            video = int(query["video"]) if query.get("video") else None
        except ValueError:
            return True, *_json_response({"error": "非法数字"}, 400)
        try:
            r = svc.resize_pools(tts=tts, video=video)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        return True, *_json_response(r)

    if route == "/pause":
        svc.pause()
        return True, *_json_response({"paused": True})
    if route == "/resume":
        svc.resume()
        return True, *_json_response({"paused": False})

    if route == "/pause_batch":
        batch_id = query.get("batch_id") or ""
        if not _safe_batch_id(batch_id):
            return True, *_json_response({"error": "非法 batch_id"}, 400)
        try:
            svc.pause_batch(batch_id)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 404)
        return True, *_json_response({"paused_batch": batch_id})
    if route == "/resume_batch":
        batch_id = query.get("batch_id") or ""
        if not _safe_batch_id(batch_id):
            return True, *_json_response({"error": "非法 batch_id"}, 400)
        try:
            svc.resume_batch(batch_id)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 404)
        return True, *_json_response({"resumed_batch": batch_id})

    if route == "/cancel_task":
        task_id = query.get("task_id") or _read_json(body).get("task_id") or ""
        if not _safe_task_id(task_id):
            return True, *_json_response({"error": "非法 task_id"}, 400)
        try:
            ok = svc.cancel_task(task_id)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 404)
        return True, *_json_response({"cancelled": ok})

    if route == "/cancel_waiting":
        batch_id = query.get("batch_id") or _read_json(body).get("batch_id") or ""
        if not _safe_batch_id(batch_id):
            return True, *_json_response({"error": "非法 batch_id"}, 400)
        try:
            n = svc.cancel_waiting(batch_id)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 404)
        return True, *_json_response({"cancelled": n})

    if route == "/retry_failed":
        batch_id = query.get("batch_id") or _read_json(body).get("batch_id") or ""
        if not _safe_batch_id(batch_id):
            return True, *_json_response({"error": "非法 batch_id"}, 400)
        only = str(query.get("only_retryable", "0")) in ("1", "true", "True")
        try:
            n = svc.retry_failed(batch_id, only)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 404)
        return True, *_json_response({"retried": n})

    if route == "/try_voice":
        voice_id = query.get("voice_id") or DEFAULT_VOICE_ID
        try:
            speed = float(query.get("speed") or DEFAULT_SPEED)
        except ValueError:
            return True, *_json_response({"error": "非法 speed"}, 400)
        try:
            out = svc.probe_sample(voice_id=voice_id, speed=speed)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return True, *_json_response({"error": str(exc)}, 502)
        return True, *_json_response({"audio_path": str(out)})

    return True, *_json_response({"error": "not found"}, 404)


def _extract_xlsx_body(body: bytes, content_type: str) -> bytes:
    if body and content_type and content_type.startswith("multipart/form-data"):
        result = _parse_multipart_file(body, content_type)
        if result is None:
            raise ValueError("multipart 中未找到 file/xlsx 字段")
        return result
    return body


def _parse_multipart_file(body: bytes, content_type: str) -> bytes | None:
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part.split("=", 1)[1].strip().strip('"')
            break
    if not boundary:
        return None
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
    return None


def _read_json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        v = json.loads(body.decode("utf-8", "ignore"))
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
