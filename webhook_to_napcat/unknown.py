from __future__ import annotations

import json
from typing import Any

from .config import Config
from .internal import HandlerResult
from .logs import append_error_log, append_message_log
from .media import extract_payload_media, looks_like_base64_blob, save_base64_media, sanitize_for_log
from .napcat import default_targets, image_segment, send_segments, send_text
from .utils import compact_json, now_iso


def render_unknown_text(payload: Any) -> str:
    if isinstance(payload, (dict, list)):
        return compact_json(payload, indent=2)
    text = str(payload).strip()
    return text or "(empty)"


def handle_unknown_notification(
    cfg: Config,
    payload: Any,
    *,
    request_id: str,
    request_meta: dict[str, Any],
    auth: dict[str, Any],
) -> HandlerResult:
    attachments = []
    if isinstance(payload, (dict, list)):
        rendered_payload, attachments = extract_payload_media(cfg, payload, request_id, namespace="unknown")
        text = render_unknown_text(rendered_payload)
    elif isinstance(payload, str) and looks_like_base64_blob(payload):
        saved = save_base64_media(cfg, payload, request_id=request_id, namespace="unknown", path_hint="body")
        attachments = [saved] if saved is not None else []
        text = render_unknown_text(saved.payload_summary() if saved is not None else {"base64_omitted": True, "saved": False})
    else:
        text = render_unknown_text(payload)

    targets = default_targets(cfg)
    if not targets:
        record = {
            "ts": now_iso(),
            "request_id": request_id,
            "layer": "message",
            "route": "unknown",
            "outcome": "failed",
            "error": "no default NapCat targets configured",
            "request": request_meta,
            "auth": auth,
            "payload": sanitize_for_log(payload),
        }
        append_message_log(cfg, record)
        append_error_log(cfg, {**record, "layer": "error", "stage": "routing", "error_type": "no_targets"})
        return HandlerResult(502, {"ok": False, "error": "no default NapCat targets configured", "request_id": request_id})

    text_report = send_text(cfg, text, targets)
    image_reports = []
    for attachment in attachments:
        if attachment is not None and attachment.is_image:
            report = send_segments(cfg, [image_segment(attachment.public_path)], targets)
            image_reports.append({"attachment": attachment.log_summary(), **report.to_log()})

    status_code = 502 if text_report.all_failed else 200
    outcome = "failed" if status_code >= 500 else "forwarded"
    message_record = {
        "ts": now_iso(),
        "request_id": request_id,
        "layer": "message",
        "route": "unknown",
        "outcome": outcome,
        "request": request_meta,
        "auth": auth,
        "target": [target.to_log() for target in targets],
        "forward_text": text,
        "attachments": [attachment.log_summary() for attachment in attachments if attachment is not None],
        "image_reports": image_reports,
        **text_report.to_log(),
    }
    append_message_log(cfg, message_record)
    if status_code >= 500:
        append_error_log(
            cfg,
            {
                "ts": now_iso(),
                "request_id": request_id,
                "layer": "error",
                "route": "unknown",
                "stage": "egress",
                "error_type": "forward_failed",
                "request": request_meta,
                "auth": auth,
                "target": message_record["target"],
                "napcat": message_record.get("napcat", []),
            },
        )

    return HandlerResult(
        status_code,
        {
            "ok": status_code < 500,
            "route": "unknown",
            "request_id": request_id,
            "chunks": len(text_report.chunks),
            "targets": len(targets),
            "deliveries": text_report.attempted,
            "attachments": len(attachments),
        },
    )
