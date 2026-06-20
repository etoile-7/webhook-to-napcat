from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import Config
from .logs import append_error_log, append_message_log, eprint
from .media import PersistedMedia, decode_base64_media, save_media_bytes, sanitize_for_log
from .napcat import NapCatTarget, parse_internal_targets, send_file, send_text
from .utils import now_iso, safe_int


REQUIRED_FIELDS = {
    "notification_id",
    "program_id",
    "program_name",
    "targets",
    "summary",
    "sent_at",
    "attachments",
}
DEDUP_LOCK = threading.Lock()
SEEN_NOTIFICATIONS: dict[str, float] = {}


@dataclass(frozen=True)
class HandlerResult:
    status_code: int
    body: dict[str, Any]


def is_internal_notification(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("program_id") == "ito"


def cleanup_seen_notifications(now_ts: float | None = None) -> None:
    now_ts = time.time() if now_ts is None else now_ts
    with DEDUP_LOCK:
        for key in [key for key, expires_at in SEEN_NOTIFICATIONS.items() if expires_at <= now_ts]:
            SEEN_NOTIFICATIONS.pop(key, None)


def remember_notification(notification_id: str, ttl_seconds: int) -> bool:
    now_ts = time.time()
    cleanup_seen_notifications(now_ts)
    with DEDUP_LOCK:
        expires_at = SEEN_NOTIFICATIONS.get(notification_id)
        if expires_at is not None and expires_at > now_ts:
            return False
        SEEN_NOTIFICATIONS[notification_id] = now_ts + max(0, ttl_seconds)
    return True


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
                try:
                    int(target_id_text)
                except Exception:
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


def send_file_attachments(cfg: Config, targets: list[NapCatTarget], attachments: list[PersistedMedia]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for attachment in attachments:
        report = send_file(cfg, attachment.public_path, attachment.file_name, targets)
        reports.append({"attachment": attachment.log_summary(), **report.to_log()})
    return reports


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

    notification_id = payload["notification_id"].strip()
    targets, ignored_targets = parse_internal_targets(payload.get("targets"))

    if not remember_notification(notification_id, cfg.internal_dedupe_ttl_seconds):
        append_message_log(
            cfg,
            {
                "ts": now_iso(),
                "request_id": request_id,
                "layer": "message",
                "route": "ito",
                "outcome": "duplicate",
                "notification_id": notification_id,
                "request": request_meta,
                "auth": auth,
            },
        )
        return HandlerResult(200, {"ok": True, "duplicate": True, "request_id": request_id})

    saved_attachments: list[PersistedMedia] = []
    attachment_errors: list[dict[str, Any]] = []
    for index, attachment in enumerate(payload.get("attachments") or []):
        saved, error = persist_internal_attachment(cfg, attachment, request_id, index)
        if saved is not None:
            saved_attachments.append(saved)
        if error is not None:
            attachment_errors.append(error)

    summary_report = send_text(cfg, payload["summary"], targets) if targets else None
    file_reports = send_file_attachments(cfg, targets, saved_attachments) if targets else []

    outcome = "forwarded"
    status_code = 200
    if summary_report is not None and summary_report.all_failed:
        outcome = "failed"
        status_code = 502
    elif not targets:
        outcome = "accepted_no_targets"

    message_record: dict[str, Any] = {
        "ts": now_iso(),
        "request_id": request_id,
        "layer": "message",
        "route": "ito",
        "outcome": outcome,
        "notification_id": notification_id,
        "program_name": payload.get("program_name"),
        "sent_at": payload.get("sent_at"),
        "request": request_meta,
        "auth": auth,
        "target": [target.to_log() for target in targets],
        "ignored_targets": ignored_targets,
        "summary_chars": len(payload["summary"]),
        "attachments": [attachment.log_summary() for attachment in saved_attachments],
        "attachment_errors": attachment_errors,
        "file_reports": file_reports,
    }
    if summary_report is not None:
        message_record.update(summary_report.to_log())
    append_message_log(cfg, message_record)

    if status_code >= 500:
        append_error_log(
            cfg,
            {
                "ts": now_iso(),
                "request_id": request_id,
                "layer": "error",
                "route": "ito",
                "stage": "egress",
                "error_type": "forward_failed",
                "request": request_meta,
                "auth": auth,
                "target": message_record["target"],
                "napcat": message_record.get("napcat", []),
            },
        )

    eprint(
        json.dumps(
            {
                "event": "internal_notification_handled",
                "outcome": outcome,
                "notification_id": notification_id,
                "targets": len(targets),
                "request_id": request_id,
            },
            ensure_ascii=False,
        )
    )
    return HandlerResult(
        status_code,
        {
            "ok": status_code < 500,
            "route": "ito",
            "request_id": request_id,
            "targets": len(targets),
            "deliveries": 0 if summary_report is None else summary_report.attempted,
            "attachments": len(saved_attachments),
            "attachment_errors": len(attachment_errors),
        },
    )
