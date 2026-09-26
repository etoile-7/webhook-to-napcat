from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from .config import Config
from .logs import append_error_log, append_message_log
from .media import PersistedMedia, decode_base64_media, file_to_base64_uri, save_media_bytes, sanitize_for_log
from .napcat import image_segment, parse_internal_targets, send_file, send_segments, send_text
from .utils import now_iso, safe_int, split_text_for_qq
from .delivery_store import DeliveryBusy, DeliveryConflict, DeliveryStore


REQUIRED_FIELDS = {
    "notification_id",
    "program_id",
    "program_name",
    "targets",
    "summary",
    "sent_at",
    "attachments",
}


@dataclass(frozen=True)
class HandlerResult:
    status_code: int
    body: dict[str, Any]


def is_internal_notification(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("program_id") == "ito"


def validate_internal_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    payload_keys = set(payload.keys())
    missing = sorted(REQUIRED_FIELDS - payload_keys)
    unexpected = sorted(payload_keys - REQUIRED_FIELDS)
    if missing:
        errors.append("missing_fields:" + ",".join(missing))
    if unexpected:
        errors.append("unexpected_fields:" + ",".join(unexpected))
    if not isinstance(payload.get("notification_id"), str) or not payload.get("notification_id", "").strip():
        errors.append("notification_id_invalid")
    if payload.get("program_id") != "ito":
        errors.append("program_id_invalid")
    if not isinstance(payload.get("program_name"), str) or not payload.get("program_name", "").strip():
        errors.append("program_name_invalid")
    if not isinstance(payload.get("targets"), list):
        errors.append("targets_invalid")
    if not isinstance(payload.get("summary"), str) or not payload.get("summary", "").strip():
        errors.append("summary_invalid")
    if not isinstance(payload.get("sent_at"), str) or not payload.get("sent_at", "").strip():
        errors.append("sent_at_invalid")
    try:
        stamp = datetime.fromisoformat(str(payload.get('sent_at', '')).replace('Z', '+00:00'))
        if stamp.utcoffset() != timedelta(0):
            errors.append('sent_at_not_utc')
    except ValueError:
        errors.append('sent_at_invalid')
    if not isinstance(payload.get("attachments"), list):
        errors.append("attachments_invalid")
    if isinstance(payload.get("targets"), list):
        for index, target in enumerate(payload["targets"]):
            if not isinstance(target, dict):
                errors.append(f"target_{index}_not_object")
                continue
            target_type = str(target.get("type") or "").strip().lower()
            target_id = target.get("id")
            target_id_text = str(target_id or "").strip()
            if target_type not in {"user", "group"}:
                errors.append(f"target_{index}_type_invalid")
            if not target_id_text:
                errors.append(f"target_{index}_id_empty")
            else:
                if not target_id_text.isascii() or not target_id_text.isdigit() or int(target_id_text) <= 0:
                    errors.append(f"target_{index}_id_not_numeric")
    return errors


def persist_internal_attachment(cfg: Config, attachment: Any, request_id: str, index: int) -> tuple[PersistedMedia | None, dict[str, Any] | None]:
    if not isinstance(attachment, dict):
        return None, {"index": index, "error": "attachment_not_object"}

    required = {"type", "file_name", "mime_type", "base64"}
    missing = sorted(required - set(attachment.keys()))
    if missing:
        return None, {"index": index, "error": "missing_fields", "fields": missing}

    file_name = str(attachment.get("file_name") or "").strip()
    mime_type = str(attachment.get("mime_type") or "").strip().lower()
    raw_base64 = attachment.get("base64")
    if not file_name or not mime_type or not isinstance(raw_base64, str) or not raw_base64.strip():
        return None, {"index": index, "error": "invalid_attachment_fields"}

    data, uri_mime = decode_base64_media(raw_base64)
    if data is None:
        return None, {"index": index, "file_name": file_name, "error": "base64_decode_failed"}

    expected_size = safe_int(attachment.get("size_bytes"))
    if expected_size is not None and expected_size != len(data):
        return None, {
            "index": index,
            "file_name": file_name,
            "error": "size_mismatch",
            "expected": expected_size,
            "actual": len(data),
        }

    digest = hashlib.sha256(data).hexdigest()
    expected_sha = str(attachment.get("sha256") or "").strip().lower()
    if expected_sha and expected_sha != digest:
        return None, {"index": index, "file_name": file_name, "error": "sha256_mismatch"}

    kind = str(attachment.get("type") or "file").strip().lower() or "file"
    saved = save_media_bytes(
        cfg,
        data,
        file_name=file_name,
        mime_type=uri_mime or mime_type,
        request_id=request_id,
        namespace="ito",
        path_hint=str(index),
        kind=kind,
        caption=str(attachment.get("caption") or "").strip(),
    )
    return saved, None


def handle_internal_notification(
    cfg: Config,
    payload: dict[str, Any],
    *,
    request_id: str,
    request_meta: dict[str, Any],
    auth: dict[str, Any],
) -> HandlerResult:
    errors = validate_internal_payload(payload)
    if errors:
        record = {
            "ts": now_iso(),
            "request_id": request_id,
            "layer": "message",
            "route": "ito",
            "outcome": "rejected",
            "errors": errors,
            "request": request_meta,
            "auth": auth,
            "payload": sanitize_for_log(payload),
        }
        append_message_log(cfg, record)
        append_error_log(cfg, {**record, "layer": "error", "stage": "validation", "error_type": "internal_notification_invalid"})
        return HandlerResult(400, {"ok": False, "route": "ito", "error": "invalid internal notification", "errors": errors, "request_id": request_id})

    store = DeliveryStore(cfg.media_dir)
    try:
        with store.claim(payload) as prior:
            if prior is not None:
                return HandlerResult(200, {**prior, "duplicate": True})
            return _forward_durable(cfg, payload, store, request_id)
    except DeliveryBusy:
        return HandlerResult(503, {"ok": False, "state": "in_progress", "request_id": request_id})
    except DeliveryConflict:
        return HandlerResult(409, {"ok": False, "state": "identity_conflict", "request_id": request_id})
    finally:
        store.close()


def _forward_durable(cfg, payload, store, request_id):
    key = payload["notification_id"]
    targets, _ = parse_internal_targets(payload["targets"])
    # A transport timeout may mean QQ already accepted the message. Do not
    # blindly replay an uncertain downstream side effect inside post_json.
    outbound = replace(cfg, retries=0)
    chunks = store.text_plan(key, split_text_for_qq(payload["summary"], cfg.chunk_size, outbound_limit=0))
    counts = {"confirmed": 0, "failed": 0, "uncertain": 0}

    def deliver(step, send):
        state = store.state(key, step)
        if state == "confirmed":
            return state
        if state == "uncertain":
            return state
        store.set_state(key, step, "sending")
        try:
            report = send()
            state = ("confirmed" if report.results and all(r.get("ok") is True for r in report.results)
                     else "uncertain" if not report.results or any(r.get("error") for r in report.results)
                     else "failed")
        except Exception:
            state = "uncertain"
        store.set_state(key, step, state)
        return state

    for target in targets:
        for index, chunk in enumerate(chunks):
            step = f"text:{target.kind}:{target.id}:{index}"
            state = deliver(step, lambda: send_text(outbound, chunk, [target]))
            counts[state] += 1
            if state != "confirmed":
                # Preserve order within a target; other targets still proceed.
                break

    attachment_failures = 0
    # Text is always attempted before attachment decoding or sending.
    if targets and counts["failed"] == 0 and counts["uncertain"] == 0:
        for index, attachment in enumerate(payload["attachments"]):
            try:
                saved, error = persist_internal_attachment(cfg, attachment, request_id, index)
            except Exception:
                saved, error = None, {"error": "attachment_preparation_failed"}
            if error or saved is None:
                attachment_failures += 1
                continue
            for target in targets:
                step = f"attachment:{target.kind}:{target.id}:{index}"
                if saved.is_image:
                    uri = file_to_base64_uri(saved.internal_path)
                    if uri is None:
                        attachment_failures += 1
                        continue
                    send = lambda: send_segments(outbound, [image_segment(uri)], [target])
                else:
                    send = lambda: send_file(outbound, saved.public_path, saved.file_name, [target])
                if deliver(step, send) != "confirmed":
                    attachment_failures += 1

    state = ("accepted_no_targets" if not targets else
             "uncertain" if counts["uncertain"] else
             "partial" if counts["failed"] and counts["confirmed"] else
             "failed" if counts["failed"] else "forwarded")
    status = 409 if state == "uncertain" else 502 if counts["failed"] else 200
    body = {"ok": state == "forwarded", "state": state, "route": "ito",
            "request_id": request_id, "targets": len(targets),
            "deliveries": counts["confirmed"], "text_confirmed": counts["confirmed"],
            "text_failed": counts["failed"], "text_uncertain": counts["uncertain"],
            "attachments": len(payload["attachments"]),
            "attachment_failures": attachment_failures}
    if state in {"forwarded", "accepted_no_targets"}:
        store.finish(key, body)
    record = {"ts": now_iso(), "notification_id": key, **body}
    append_message_log(cfg, record)
    if status != 200 or attachment_failures:
        append_error_log(cfg, {**record, "error_type": "attachment_forward_failed" if attachment_failures else "forward_failed"})
    return HandlerResult(status, body)
