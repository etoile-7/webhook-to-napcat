#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

from .bililive import (
    AggregateBucket,
    apply_live_session_segments_to_bucket,
    build_aggregate_context,
    build_end_bucket_metrics,
    build_start_bucket_score,
    cancel_pending_start_after_end,
    clear_live_session_segments,
    clear_recent_forwarded_start,
    get_bucket_field_value,
    get_recent_forwarded_start,
    handle_bililive_notification,
    hold_start_after_recent_end,
    is_bililive_notification,
    is_meaningful_streaming_end_candidate,
    is_recording_segment_end_bucket,
    is_recording_segment_start_bucket,
    is_true_bililive_end_bucket,
    is_true_bililive_start_bucket,
    remember_live_session_segment,
    remember_recent_forwarded_start,
    should_replace_aggregate_bucket_event,
    should_suppress_recent_forwarded_end_candidate,
    should_suppress_recent_forwarded_start_candidate,
)
from .config import Config, parse_args
from .internal import HandlerResult, handle_internal_notification, is_internal_notification
from .logs import append_error_log, append_request_log, eprint, sanitized_headers
from .media import sanitize_for_log
from .unknown import handle_unknown_notification
from .utils import compact_json, now_iso, truncate_middle


def maybe_parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def parse_body(content_type: str, raw: bytes) -> Any:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype == "application/json":
        parsed = maybe_parse_json(raw)
        return parsed if parsed is not None else raw.decode("utf-8", errors="replace")
    if ctype == "application/x-www-form-urlencoded":
        parsed = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {key: value if len(value) != 1 else value[0] for key, value in parsed.items()}
    if ctype == "text/plain":
        return raw.decode("utf-8", errors="replace")
    parsed_json = maybe_parse_json(raw)
    if parsed_json is not None:
        return parsed_json
    return raw.decode("utf-8", errors="replace")


def summarize_payload(payload: Any, max_len: int = 500) -> str:
    safe_payload = sanitize_for_log(payload)
    if isinstance(safe_payload, (dict, list)):
        text = compact_json(safe_payload, indent=2)
    else:
        text = str(safe_payload)
    return text if len(text) <= max_len else truncate_middle(text, max_len)


def evaluate_secret(cfg: Config, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    query = urllib.parse.urlparse(handler.path).query
    params = urllib.parse.parse_qs(query)
    from_query = params.get("secret", [""])[0]
    from_header = handler.headers.get("X-Webhook-Secret", "")
    if not cfg.secret:
        return {
            "required": False,
            "provided_via_query": bool(from_query),
            "provided_via_header": bool(from_header),
            "status": "not_configured",
        }
    if cfg.secret in {from_query, from_header}:
        return {
            "required": True,
            "provided_via_query": bool(from_query),
            "provided_via_header": bool(from_header),
            "status": "passed",
        }
    return {
        "required": True,
        "provided_via_query": bool(from_query),
        "provided_via_header": bool(from_header),
        "status": "failed",
    }


def build_request_record(
    handler: BaseHTTPRequestHandler,
    cfg: Config,
    request_id: str,
    parsed: urllib.parse.ParseResult,
    payload: Any,
    auth: dict[str, Any],
    outcome: str,
    note: str = "",
) -> dict[str, Any]:
    return {
        "ts": now_iso(),
        "request_id": request_id,
        "layer": "request",
        "stage": "ingress",
        "outcome": outcome,
        "note": note,
        "request": {
            "method": handler.command,
            "path": parsed.path,
            "query_keys": sorted(set(urllib.parse.parse_qs(parsed.query).keys())),
            "remote_ip": handler.client_address[0],
            "content_type": handler.headers.get("Content-Type", ""),
            "content_length": int(handler.headers.get("Content-Length", "0") or "0"),
            "headers": sanitized_headers(handler.headers),
            "payload": sanitize_for_log(payload),
            "payload_summary": summarize_payload(payload),
        },
        "auth": auth,
        "target": {"private": cfg.private, "group": cfg.group},
    }


def dispatch_notification(
    cfg: Config,
    payload: Any,
    *,
    request_id: str,
    request_meta: dict[str, Any],
    auth: dict[str, Any],
    payload_summary: str = "",
) -> HandlerResult:
    if is_internal_notification(payload):
        return handle_internal_notification(cfg, payload, request_id=request_id, request_meta=request_meta, auth=auth)
    if is_bililive_notification(payload):
        return handle_bililive_notification(
            cfg,
            payload,
            request_id=request_id,
            request_meta=request_meta,
            auth=auth,
            payload_summary=payload_summary,
        )
    return handle_unknown_notification(cfg, payload, request_id=request_id, request_meta=request_meta, auth=auth)


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "WebhookToNapCat/2.0"

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        eprint("[http]", self.address_string(), "-", fmt % args)

    def send_json(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/health", self.cfg.path.rstrip("/") + "/health"}:
            self.send_json(200, {"ok": True, "status": "healthy"})
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        request_id = uuid4().hex
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        payload = parse_body(self.headers.get("Content-Type", ""), raw)
        auth = evaluate_secret(self.cfg, self)

        if parsed.path.rstrip("/") != self.cfg.path.rstrip("/"):
            request_record = build_request_record(self, self.cfg, request_id, parsed, payload, auth, "path_not_matched", "received POST on unmatched path")
            append_request_log(self.cfg, request_record)
            append_error_log(
                self.cfg,
                {
                    "ts": now_iso(),
                    "request_id": request_id,
                    "layer": "error",
                    "stage": "routing",
                    "error_type": "path_not_matched",
                    "request": request_record["request"],
                    "auth": auth,
                },
            )
            self.send_json(404, {"ok": False, "error": "path not matched", "request_id": request_id})
            return

        if auth["status"] == "failed":
            request_record = build_request_record(self, self.cfg, request_id, parsed, payload, auth, "rejected", "webhook secret validation failed")
            append_request_log(self.cfg, request_record)
            append_error_log(
                self.cfg,
                {
                    "ts": now_iso(),
                    "request_id": request_id,
                    "layer": "error",
                    "stage": "auth",
                    "error_type": "invalid_secret",
                    "request": request_record["request"],
                    "auth": auth,
                },
            )
            self.send_json(401, {"ok": False, "error": "invalid secret", "request_id": request_id})
            return

        request_record = build_request_record(self, self.cfg, request_id, parsed, payload, auth, "accepted", "webhook accepted")
        append_request_log(self.cfg, request_record)
        request_meta = {"path": parsed.path, "remote_ip": self.client_address[0]}

        result = dispatch_notification(
            self.cfg,
            payload,
            request_id=request_id,
            request_meta=request_meta,
            auth=auth,
            payload_summary=request_record["request"]["payload_summary"],
        )

        self.send_json(result.status_code, result.body)


def main() -> int:
    cfg = parse_args()
    server = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), WebhookHandler)
    server.cfg = cfg  # type: ignore[attr-defined]
    print(json.dumps({"status": "listening", "host": cfg.listen_host, "port": cfg.listen_port, "path": cfg.path}, ensure_ascii=False))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AggregateBucket",
    "Config",
    "dispatch_notification",
    "HandlerResult",
    "apply_live_session_segments_to_bucket",
    "build_aggregate_context",
    "build_end_bucket_metrics",
    "build_start_bucket_score",
    "cancel_pending_start_after_end",
    "clear_live_session_segments",
    "clear_recent_forwarded_start",
    "get_bucket_field_value",
    "get_recent_forwarded_start",
    "hold_start_after_recent_end",
    "is_meaningful_streaming_end_candidate",
    "is_recording_segment_end_bucket",
    "is_recording_segment_start_bucket",
    "is_true_bililive_end_bucket",
    "is_true_bililive_start_bucket",
    "main",
    "parse_body",
    "remember_live_session_segment",
    "remember_recent_forwarded_start",
    "sanitize_for_log",
    "should_replace_aggregate_bucket_event",
    "should_suppress_recent_forwarded_end_candidate",
    "should_suppress_recent_forwarded_start_candidate",
    "summarize_payload",
]
