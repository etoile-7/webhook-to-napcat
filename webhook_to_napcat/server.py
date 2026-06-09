#!/usr/bin/env python3
"""Listen for webhook POSTs and forward them to QQ via NapCat (OneBot v11 HTTP API)."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4


SENSITIVE_HEADERS = {"authorization", "x-webhook-secret", "proxy-authorization"}
AGGREGATE_LOCK = threading.Lock()
AGGREGATE_BUCKETS: dict[str, "AggregateBucket"] = {}
PENDING_END_LOCK = threading.Lock()
PENDING_END_NOTIFICATIONS: dict[str, "PendingEndNotification"] = {}
PENDING_START_AFTER_END_LOCK = threading.Lock()
PENDING_START_AFTER_END_NOTIFICATIONS: dict[str, "PendingStartAfterEndNotification"] = {}
RECENT_START_LOCK = threading.Lock()
RECENT_FORWARDED_STARTS: dict[str, "RecentForwardedStart"] = {}
RECENT_END_LOCK = threading.Lock()
RECENT_FORWARDED_ENDS: dict[str, "RecentForwardedEnd"] = {}
LIVE_SESSION_SEGMENT_LOCK = threading.Lock()
LIVE_SESSION_SEGMENTS: dict[str, "LiveSessionSegmentAccumulator"] = {}
BILILIVE_SESSION_STATS_COMPUTED_KEY = "__bililive_live_session_merged_stats"
PRICE_TABLE_CACHE: dict[str, dict[str, float]] = {}
BILIBILI_ROOM_INFO_CACHE: dict[str, dict[str, Any]] = {}
BASE64_DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.*)$", re.S)
MEDIA_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/x-icon": ".ico",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "video/mp4": ".mp4",
    "video/mpeg": ".mpg",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/json": ".json",
    "text/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/markdown": ".md",
    "text/html": ".html",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/x-tar": ".tar",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/x-7z-compressed": ".7z",
    "application/x-rar-compressed": ".rar",
    "application/octet-stream": ".bin",
}
BASE64_FIELD_NAMES = {
    "base64",
    "base64_data",
    "base64_png",
    "png_base64",
    "image_base64",
    "cover_base64",
    "content_base64",
    "file_base64",
    "raw_base64",
    "blob",
}


@dataclass
class Config:
    listen_host: str
    listen_port: int
    path: str
    secret: str
    napcat_base_url: str
    napcat_token: str
    napcat_token_mode: str
    private: int | None
    group: int | None
    timeout: float
    retries: int
    chunk_size: int
    title_prefix: str
    include_headers: bool
    rules_path: str
    log_dir: str
    aggregate_window_ms: int
    notify_file_opening: bool
    notify_debounce_ms: int
    media_dir: str = "/app/media"
    public_media_dir: str = "/opt/WebhookToNapcat/media"


@dataclass
class AggregateBucket:
    key: str
    phase: str
    group_name: str
    group_config: dict[str, Any]
    created_at: float
    request_path: str
    remote_ip: str
    auth: dict[str, Any]
    target: dict[str, Any]
    request_ids: list[str] = field(default_factory=list)
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    payload_summaries: list[str] = field(default_factory=list)
    computed: dict[str, Any] = field(default_factory=dict)
    timer: threading.Timer | None = None


@dataclass
class PendingEndNotification:
    key: str
    bucket: AggregateBucket | None
    expires_at: float
    timer: threading.Timer | None = None
    stream_end_bucket: AggregateBucket | None = None
    created_at: float = field(default_factory=time.time)
    window_ms: int = 0


@dataclass
class PendingStartAfterEndNotification:
    key: str
    bucket: AggregateBucket
    expires_at: float
    timer: threading.Timer | None = None
    window_ms: int = 0


@dataclass
class RecentForwardedStart:
    key: str
    score: tuple[int, int, int, int]
    expires_at: float


@dataclass
class RecentForwardedEnd:
    key: str
    score: tuple[int, int, int, int, int, int, int]
    expires_at: float


@dataclass
class LiveSessionSegmentAccumulator:
    key: str
    expires_at: float
    segments: dict[str, dict[str, Any]] = field(default_factory=dict)


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)



def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")



def append_jsonl(cfg: Config, bucket: str, record: dict[str, Any]) -> None:
    if not cfg.log_dir:
        return

    try:
        record = sanitize_payload_for_log(record)
        log_dir = Path(cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        month = datetime.now().strftime("%Y-%m")
        log_path = log_dir / f"{bucket}-{month}.jsonl"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        eprint(f"[{bucket}-log-error] {exc}")



def append_request_log(cfg: Config, record: dict[str, Any]) -> None:
    append_jsonl(cfg, "requests", record)



def append_message_log(cfg: Config, record: dict[str, Any]) -> None:
    append_jsonl(cfg, "messages", record)



def append_error_log(cfg: Config, record: dict[str, Any]) -> None:
    append_jsonl(cfg, "errors", record)



def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float, retries: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json", **headers},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {"status": "ok", "raw": raw}
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < retries:
                time.sleep(0.4 * (2**i))
    raise RuntimeError(f"POST failed after retries: {last}")



def fetch_text(url: str, headers: dict[str, str] | None = None, timeout: float = 10.0, retries: int = 1) -> str:
    last = None
    request_headers = headers or {}
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=request_headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < retries:
                time.sleep(0.4 * (2**i))
    raise RuntimeError(f"GET failed after retries: {last}")



def napcat_response_ok(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    retcode = response.get("retcode")
    status = str(response.get("status") or "").lower()
    if retcode == 0:
        return True
    return status == "ok" and retcode in {None, "", 0}



def file_to_base64_uri(path: str) -> str | None:
    try:
        data = Path(path).read_bytes()
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    return "base64://" + base64.b64encode(data).decode("ascii")



def decode_base64_media(value: str) -> tuple[bytes | None, str | None]:
    text = value.strip()
    mime_type: str | None = None

    data_uri_match = BASE64_DATA_URI_RE.match(text)
    if data_uri_match:
        mime_type = data_uri_match.group("mime").strip().lower() or None
        text = data_uri_match.group("data").strip()
    elif text.startswith("base64://"):
        text = text[len("base64://") :].strip()

    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None, mime_type

    try:
        return base64.b64decode(compact, validate=True), mime_type
    except Exception:  # noqa: BLE001
        try:
            padded = compact + ("=" * (-len(compact) % 4))
            return base64.b64decode(padded, validate=False), mime_type
        except Exception:  # noqa: BLE001
            return None, mime_type



def media_extension(mime_type: str | None, file_name: Any = None, default: str = ".bin") -> str:
    if isinstance(file_name, str) and file_name.strip():
        suffix = Path(file_name.strip()).suffix.lower()
        if suffix:
            return suffix
    return MEDIA_MIME_EXTENSIONS.get((mime_type or "").strip().lower(), default)



def safe_media_stem(value: Any, default: str) -> str:
    raw = str(value or "").strip()
    if raw:
        raw = Path(raw).stem or Path(raw).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-_")
    return (cleaned[:96] if cleaned else default)



def safe_path_component(value: Any, default: str = "asset") -> str:
    raw = str(value or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-_")
    return (cleaned[:48] if cleaned else default)



def persist_base64_media(
    cfg: Config,
    base64_value: str,
    *,
    mime_type: str | None = None,
    file_name: Any = None,
    id_hint: Any = None,
    path_hint: Any = None,
    namespace: str = "bili_upload_auto",
) -> dict[str, Any]:
    data, uri_mime_type = decode_base64_media(base64_value)
    if data is None:
        return {"ok": False, "error": "base64_decode_failed"}

    resolved_mime_type = uri_mime_type or (mime_type.strip().lower() if isinstance(mime_type, str) and mime_type.strip() else None) or "application/octet-stream"
    ext = media_extension(resolved_mime_type, file_name=file_name)
    stem = safe_media_stem(id_hint or file_name or uuid4().hex, default="media")
    date_part = datetime.now().strftime("%Y-%m-%d")
    subdir = safe_path_component(path_hint, default="asset")
    rel_path = Path(namespace) / date_part / subdir / f"{stem}{ext}"

    media_dir = Path(cfg.media_dir or "media")
    public_media_dir = Path(cfg.public_media_dir or cfg.media_dir or "media")
    save_path = media_dir / rel_path
    public_path = public_media_dir / rel_path

    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(data)

    return {
        "ok": True,
        "path": str(public_path),
        "internal_path": str(save_path),
        "mime_type": resolved_mime_type,
        "size_bytes": len(data),
        "base64_uri": "base64://" + base64.b64encode(data).decode("ascii"),
    }



def _extract_asset_blob(asset: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    if not isinstance(asset, dict):
        return None, None, None, None, None

    file_name = None
    for key in ("file_name", "filename", "original_filename", "original_file_name"):
        value = asset.get(key)
        if isinstance(value, str) and value.strip():
            file_name = value
            break

    mime_type = None
    for key in ("mime_type", "mimetype", "content_type", "content_type_hint"):
        value = asset.get(key)
        if isinstance(value, str) and value.strip():
            mime_type = value
            break

    has_metadata = file_name is not None or mime_type is not None
    for key in ("base64", "base64_data", "content_base64", "file_base64", "raw_base64", "blob", "data"):
        value = asset.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        is_data_uri = BASE64_DATA_URI_RE.match(value) is not None
        explicit_base64_key = "base64" in key
        if key == "data" and not (has_metadata or is_data_uri or looks_like_base64_blob(value)):
            continue
        if key != "data" and not (has_metadata or explicit_base64_key or is_data_uri or looks_like_base64_blob(value)):
            continue
        return key, value, file_name, mime_type, safe_path_component(asset.get("name") or asset.get("title") or asset.get("id") or key)

    return None, None, file_name, mime_type, None



def persist_asset_dict(cfg: Config, asset: dict[str, Any], request_id: str, *, namespace: str, path_hint: str) -> dict[str, Any]:
    result_asset = copy.deepcopy(asset)
    blob_key, raw_base64, file_name, mime_type, asset_hint = _extract_asset_blob(result_asset)
    if blob_key is None or raw_base64 is None:
        return result_asset

    persisted = persist_base64_media(
        cfg,
        raw_base64,
        mime_type=mime_type,
        file_name=file_name,
        id_hint=result_asset.get("id") or result_asset.get("name") or request_id or asset_hint,
        path_hint=path_hint,
        namespace=namespace,
    )

    result_asset.pop(blob_key, None)
    if persisted.get("ok"):
        path = str(persisted["path"])
        result_asset["file_path"] = path
        result_asset["saved_path"] = path
        result_asset["internal_path"] = persisted.get("internal_path")
        if persisted.get("base64_uri"):
            result_asset["base64_uri"] = persisted.get("base64_uri")
        result_asset["size_bytes"] = persisted.get("size_bytes")
        result_asset["mime_type"] = persisted.get("mime_type") or mime_type or result_asset.get("mime_type")
        result_asset["base64_saved"] = True
    else:
        result_asset["base64_error"] = str(persisted.get("error") or "base64_decode_failed")
        result_asset["base64_saved"] = False

    return result_asset



def persist_payload_base64_assets(cfg: Config, payload: Any, request_id: str, *, namespace: str, path_parts: tuple[str, ...] = ()) -> Any:
    if isinstance(payload, dict):
        if _extract_asset_blob(payload)[0] is not None:
            return persist_asset_dict(cfg, payload, request_id, namespace=namespace, path_hint=".".join(path_parts) or "payload")
        return {
            str(key): persist_payload_base64_assets(
                cfg,
                value,
                request_id,
                namespace=namespace,
                path_parts=path_parts + (str(key),),
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [
            persist_payload_base64_assets(cfg, item, request_id, namespace=namespace, path_parts=path_parts + (str(index),))
            for index, item in enumerate(payload)
        ]
    return payload



def persist_incoming_base64_assets(cfg: Config, payload: Any, request_id: str) -> Any:
    if not isinstance(payload, dict):
        return payload

    namespace = str(payload.get("schema") or payload.get("event") or payload.get("type") or "webhook_media").replace(".", "_")
    persisted = persist_payload_base64_assets(cfg, payload, request_id, namespace=namespace)

    # Backward-compatible aliases used by existing upload-cover templates.
    if isinstance(persisted, dict):
        cover = persisted.get("cover")
        if isinstance(cover, dict) and isinstance(cover.get("file_path"), str) and cover.get("file_path"):
            persisted["cover_file"] = cover["file_path"]
            persisted["cover_path"] = cover["file_path"]
            if isinstance(cover.get("base64_uri"), str) and cover.get("base64_uri"):
                persisted["cover_base64"] = cover["base64_uri"]
            if cover.get("mime_type"):
                persisted["cover_mime_type"] = cover.get("mime_type")
    return persisted



def looks_like_base64_blob(value: str) -> bool:
    text = value.strip()
    if text.startswith("base64://") or BASE64_DATA_URI_RE.match(text):
        return True
    if len(text) < 512:
        return False
    compact = re.sub(r"\s+", "", text)
    return len(compact) >= 512 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact) is not None



def summarize_base64_for_log(value: str, key: str | None = None) -> dict[str, Any]:
    data, uri_mime_type = decode_base64_media(value)
    compact = value.strip()
    if BASE64_DATA_URI_RE.match(compact):
        match = BASE64_DATA_URI_RE.match(compact)
        compact = match.group("data").strip() if match else compact
    elif compact.startswith("base64://"):
        compact = compact[len("base64://") :].strip()
    compact = re.sub(r"\s+", "", compact)

    summary: dict[str, Any] = {
        "base64_omitted": True,
        "chars": len(value),
        "data_chars": len(compact),
    }
    if key:
        summary["field"] = key
    if uri_mime_type:
        summary["mime_type"] = uri_mime_type
    if data is not None:
        summary["decoded_bytes"] = len(data)
        summary["sha256"] = hashlib.sha256(data).hexdigest()
    return summary



def sanitize_payload_for_log(value: Any, key: str | None = None) -> Any:
    if isinstance(value, str):
        key_name = (key or "").strip().lower()
        if key_name in BASE64_FIELD_NAMES or looks_like_base64_blob(value):
            return summarize_base64_for_log(value, key=key_name or None)
        return value
    if isinstance(value, list):
        return [sanitize_payload_for_log(item, key=key) for item in value]
    if isinstance(value, dict):
        return {str(k): sanitize_payload_for_log(v, key=str(k)) for k, v in value.items()}
    return value



def load_room_cover_index(index_path: str = "/opt/WebhookToNapcat/cover/index.json") -> dict[str, Any]:
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}



def resolve_room_cover_path(room_id: Any, index_path: str = "/opt/WebhookToNapcat/cover/index.json") -> str | None:
    room_id_str = str(room_id or "").strip()
    if not room_id_str:
        return None
    index = load_room_cover_index(index_path=index_path)
    record = index.get(room_id_str)
    if not isinstance(record, dict):
        return None
    path_value = record.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    return path_value.strip()



def default_target_specs(cfg: Config) -> list[dict[str, int]]:
    targets: list[dict[str, int]] = []
    if cfg.private is not None:
        targets.append({"private": int(cfg.private)})
    if cfg.group is not None:
        targets.append({"group": int(cfg.group)})
    return targets



def normalize_target_spec(spec: Any, context: dict[str, Any] | None = None) -> dict[str, int] | None:
    if isinstance(spec, str):
        raw = spec.strip()
        if not raw:
            return None
        if context:
            try:
                raw = raw.format(**context)
            except Exception:  # noqa: BLE001
                pass
        lower = raw.lower()
        if lower == "default":
            return {"default": 1}
        if raw.startswith("private:"):
            value = raw.split(":", 1)[1].strip()
            return {"private": int(value)} if value else None
        if raw.startswith("group:"):
            value = raw.split(":", 1)[1].strip()
            return {"group": int(value)} if value else None
        return None

    if isinstance(spec, dict):
        if spec.get("default") is True:
            return {"default": 1}
        if "private" in spec and spec.get("private") not in {None, ""}:
            value = spec.get("private")
            if isinstance(value, str) and context:
                try:
                    value = value.format(**context)
                except Exception:  # noqa: BLE001
                    pass
            return {"private": int(value)}
        if "group" in spec and spec.get("group") not in {None, ""}:
            value = spec.get("group")
            if isinstance(value, str) and context:
                try:
                    value = value.format(**context)
                except Exception:  # noqa: BLE001
                    pass
            return {"group": int(value)}

    return None



def resolve_target_specs(cfg: Config, raw_targets: Any = None, context: dict[str, Any] | None = None) -> list[dict[str, int]]:
    items = raw_targets if isinstance(raw_targets, list) else ([raw_targets] if raw_targets is not None else [])
    resolved: list[dict[str, int]] = []

    if not items:
        resolved = default_target_specs(cfg)
    else:
        for item in items:
            normalized = normalize_target_spec(item, context=context)
            if not normalized:
                continue
            if "default" in normalized:
                resolved.extend(default_target_specs(cfg))
                continue
            resolved.append(normalized)

    deduped: list[dict[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for target in resolved:
        if "private" in target:
            key = ("private", int(target["private"]))
            payload = {"private": int(target["private"])}
        elif "group" in target:
            key = ("group", int(target["group"]))
            payload = {"group": int(target["group"])}
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(payload)

    return deduped



def build_napcat_request(cfg: Config, target: dict[str, int]) -> tuple[str, dict[str, str], str, dict[str, int]]:
    base_url = cfg.napcat_base_url.rstrip("/")
    if "private" in target:
        endpoint = "/send_private_msg"
        target_payload = {"user_id": int(target["private"])}
    else:
        endpoint = "/send_group_msg"
        target_payload = {"group_id": int(target["group"])}
    headers: dict[str, str] = {}
    token_q = ""

    if cfg.napcat_token:
        if cfg.napcat_token_mode == "header":
            headers["Authorization"] = "Bearer " + cfg.napcat_token
        else:
            token_q = ("&" if "?" in base_url else "?") + "access_token=" + urllib.parse.quote(cfg.napcat_token)

    return base_url + endpoint + token_q, headers, endpoint, target_payload



def split_for_qq(text: str, max_len: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return ["(empty)"]
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_len, len(text))
        if end < len(text):
            split_at = text.rfind("\n", start, end)
            if split_at <= start:
                split_at = text.rfind(" ", start, end)
            if split_at > start:
                end = split_at
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
        while start < len(text) and text[start] in {"\n", " ", "\t"}:
            start += 1

    return chunks or [text[i : i + max_len] for i in range(0, len(text), max_len)]



def send_text_to_napcat(cfg: Config, text: str, targets: list[dict[str, int]] | None = None) -> tuple[list[dict[str, Any]], list[str], list[dict[str, int]]]:
    resolved_targets = targets or default_target_specs(cfg)
    if not resolved_targets:
        raise RuntimeError("no NapCat targets resolved")

    results: list[dict[str, Any]] = []
    chunks = split_for_qq(text, cfg.chunk_size)
    for target in resolved_targets:
        url, headers, _, target_payload = build_napcat_request(cfg, target)
        for chunk in chunks:
            payload = dict(target_payload)
            payload["message"] = [{"type": "text", "data": {"text": chunk}}]
            response = post_json(url, payload, headers=headers, timeout=cfg.timeout, retries=cfg.retries)
            if not napcat_response_ok(response):
                raise RuntimeError(f"NapCat text send failed: {json.dumps(response, ensure_ascii=False)}")
            results.append({"target": target, "response": response})
    return results, chunks, resolved_targets



def send_segments_to_napcat(cfg: Config, segments: list[dict[str, Any]], targets: list[dict[str, int]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, int]]]:
    resolved_targets = targets or default_target_specs(cfg)
    if not resolved_targets:
        raise RuntimeError("no NapCat targets resolved")

    results: list[dict[str, Any]] = []
    for target in resolved_targets:
        url, headers, _, target_payload = build_napcat_request(cfg, target)
        payload = dict(target_payload)
        payload["message"] = json.loads(json.dumps(segments, ensure_ascii=False))
        response = post_json(url, payload, headers=headers, timeout=cfg.timeout, retries=cfg.retries)
        if not napcat_response_ok(response):
            raise RuntimeError(f"NapCat segment send failed: {json.dumps(response, ensure_ascii=False)}")
        results.append({"target": target, "response": response})
    return results, resolved_targets



def get_rendered_message_preview(message: dict[str, Any] | None) -> str | None:
    if not isinstance(message, dict):
        return None
    mode = str(message.get("mode") or "text")
    if mode == "text":
        text = message.get("text")
        return text.strip() if isinstance(text, str) and text.strip() else None

    fallback_text = message.get("fallback_text")
    if isinstance(fallback_text, str) and fallback_text.strip():
        return fallback_text.strip()

    parts: list[str] = []
    for segment in message.get("segments") or []:
        if isinstance(segment, dict) and segment.get("type") == "text":
            text = ((segment.get("data") or {}).get("text") if isinstance(segment.get("data"), dict) else None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip() or None



def summarize_segment_for_log(segment: Any) -> dict[str, Any] | None:
    if not isinstance(segment, dict):
        return None
    segment_type = str(segment.get("type") or "").strip().lower()
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
    if segment_type == "text":
        text = data.get("text") if isinstance(data, dict) else None
        return {
            "type": "text",
            "chars": len(text) if isinstance(text, str) else 0,
            "preview": truncate_value(text.strip(), max_len=120) if isinstance(text, str) and text.strip() else "",
        }
    if segment_type == "image":
        file_value = data.get("file") if isinstance(data, dict) else None
        summary: dict[str, Any] = {"type": "image"}
        if isinstance(file_value, str) and looks_like_base64_blob(file_value):
            summary["file_kind"] = "base64"
            summary["file"] = summarize_base64_for_log(file_value, key="file")
        elif isinstance(file_value, str):
            summary["file_kind"] = "reference"
            summary["file"] = truncate_value(file_value, max_len=240)
        else:
            summary["file_kind"] = "missing"
        for option in ("proxy", "cache", "timeout"):
            if isinstance(data, dict) and option in data:
                summary[option] = data[option]
        return summary
    return {"type": segment_type or "unknown"}



def summarize_message_for_log(message: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    mode = str(message.get("mode") or "text")
    summary: dict[str, Any] = {"mode": mode}
    if mode == "text":
        text = message.get("text")
        summary["text_chars"] = len(text) if isinstance(text, str) else 0
        summary["text_preview"] = truncate_value(text.strip(), max_len=160) if isinstance(text, str) and text.strip() else ""
        return summary

    segments = message.get("segments") if isinstance(message.get("segments"), list) else []
    summary["segments_count"] = len(segments)
    summary["segments"] = [item for item in (summarize_segment_for_log(segment) for segment in segments) if item is not None]
    fallback_text = message.get("fallback_text")
    if isinstance(fallback_text, str) and fallback_text.strip():
        summary["fallback_text_chars"] = len(fallback_text)
        summary["fallback_text_preview"] = truncate_value(fallback_text.strip(), max_len=160)
    return summary



def send_rendered_message_to_napcat(cfg: Config, message: dict[str, Any], targets: list[dict[str, int]] | None = None) -> dict[str, Any]:
    mode = str(message.get("mode") or "text")
    if mode == "segments":
        segments = message.get("segments") if isinstance(message.get("segments"), list) else []
        if not segments:
            raise RuntimeError("rendered segments message is empty")
        try:
            results, resolved_targets = send_segments_to_napcat(cfg, segments, targets=targets)
            return {
                "mode": "segments",
                "results": results,
                "resolved_targets": resolved_targets,
                "chunks": [],
                "used_fallback": False,
            }
        except Exception as exc:  # noqa: BLE001
            fallback_text = message.get("fallback_text")
            if not isinstance(fallback_text, str) or not fallback_text.strip():
                raise
            results, chunks, resolved_targets = send_text_to_napcat(cfg, fallback_text, targets=targets)
            return {
                "mode": "text",
                "results": results,
                "resolved_targets": resolved_targets,
                "chunks": chunks,
                "used_fallback": True,
                "fallback_reason": str(exc),
            }

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("rendered text message is empty")
    results, chunks, resolved_targets = send_text_to_napcat(cfg, text, targets=targets)
    return {
        "mode": "text",
        "results": results,
        "resolved_targets": resolved_targets,
        "chunks": chunks,
        "used_fallback": False,
    }



def maybe_parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None



def parse_body(content_type: str, raw: bytes) -> Any:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype == "application/json":
        return maybe_parse_json(raw)
    if ctype == "application/x-www-form-urlencoded":
        parsed = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {k: v if len(v) != 1 else v[0] for k, v in parsed.items()}
    if ctype == "text/plain":
        return raw.decode("utf-8", errors="replace")

    parsed_json = maybe_parse_json(raw)
    if parsed_json is not None:
        return parsed_json
    return raw.decode("utf-8", errors="replace")



def summarize_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        preferred_keys = [
            "title",
            "event",
            "type",
            "action",
            "status",
            "repo",
            "repository",
            "project",
            "message",
            "text",
            "content",
            "sender",
            "source",
            "EventType",
        ]
        lines = []
        for key in preferred_keys:
            if key in payload:
                value = payload[key]
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                lines.append(f"{key}: {value}")
        if lines:
            lines.append("")
            lines.append("payload:")
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
            return "\n".join(lines)
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if isinstance(payload, list):
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return str(payload)



def merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow recursive merge for JSON object configs."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key.startswith("$"):
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result



def normalize_rules_doc(data: dict[str, Any]) -> dict[str, Any]:
    """Expand named output/target references in rules.json.

    This keeps deployment configs layered: define shared outputs/targets once at the
    top level, then reference them from aggregate/rule outputs with `output_ref`,
    `use`, `output: {"$ref": "..."}`, or `targets_ref`.
    """
    doc = copy.deepcopy(data)
    output_defs = doc.get("outputs") if isinstance(doc.get("outputs"), dict) else {}
    target_defs = doc.get("targets") if isinstance(doc.get("targets"), dict) else {}

    resolving_outputs: set[str] = set()

    def resolve_targets_ref(obj: dict[str, Any]) -> dict[str, Any]:
        ref = obj.get("targets_ref")
        if isinstance(ref, str) and ref in target_defs and obj.get("targets") is None and obj.get("target") is None:
            obj["targets"] = copy.deepcopy(target_defs[ref])
        return obj

    def resolve_output_ref(ref: str) -> dict[str, Any]:
        if ref in resolving_outputs:
            raise ValueError(f"circular output_ref: {ref}")
        raw = output_defs.get(ref)
        if not isinstance(raw, dict):
            raise KeyError(f"unknown output_ref: {ref}")
        resolving_outputs.add(ref)
        try:
            return resolve_output_spec(copy.deepcopy(raw))
        finally:
            resolving_outputs.remove(ref)

    def resolve_output_spec(output: dict[str, Any]) -> dict[str, Any]:
        ref = output.get("$ref") or output.get("output_ref") or output.get("use")
        if isinstance(ref, str):
            base = resolve_output_ref(ref)
            overrides = {k: v for k, v in output.items() if k not in {"$ref", "output_ref", "use"}}
            output = merge_dicts(base, overrides)
        return resolve_targets_ref(output)

    def normalize_rule_like(rule: Any) -> Any:
        if not isinstance(rule, dict):
            return rule
        rule = copy.deepcopy(rule)
        ref = rule.pop("output_ref", None) or rule.pop("use", None)
        if isinstance(ref, str) and "output" not in rule:
            rule["output"] = resolve_output_ref(ref)
        elif isinstance(rule.get("output"), dict):
            rule["output"] = resolve_output_spec(rule["output"])
        resolve_targets_ref(rule)
        return rule

    if isinstance(doc.get("rules"), list):
        doc["rules"] = [normalize_rule_like(rule) for rule in doc["rules"]]

    if isinstance(doc.get("default"), dict):
        # `default` is an output spec, not a full rule object.
        doc["default"] = resolve_output_spec(copy.deepcopy(doc["default"]))

    aggregate = doc.get("aggregate")
    if isinstance(aggregate, dict) and isinstance(aggregate.get("groups"), list):
        for group in aggregate["groups"]:
            if not isinstance(group, dict):
                continue
            if isinstance(group.get("outputs"), list):
                group["outputs"] = [normalize_rule_like(item) for item in group["outputs"]]
            if isinstance(group.get("output"), dict):
                group["output"] = resolve_output_spec(group["output"])
            resolve_targets_ref(group)

    return doc



def load_rules(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return normalize_rules_doc(data)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        eprint(f"[rules] failed to load {path}: {exc}")
        return {}



def get_field_value(payload: Any, field: str) -> Any:
    current = payload
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current



def rule_matches(rule: dict[str, Any], handler: BaseHTTPRequestHandler, payload: Any) -> bool:
    match = rule.get("match")
    if not isinstance(match, dict):
        return True

    has_keys = match.get("has_keys")
    if has_keys is not None:
        if not isinstance(payload, dict):
            return False
        for key in has_keys:
            if key not in payload:
                return False

    field_exists = match.get("field_exists")
    if isinstance(field_exists, list):
        for key in field_exists:
            if get_field_value(payload, str(key)) in {None, ""}:
                return False

    path_contains = match.get("path_contains")
    if path_contains and path_contains not in handler.path:
        return False

    header_equals = match.get("header_equals")
    if isinstance(header_equals, dict):
        for key, expected in header_equals.items():
            if handler.headers.get(key, "") != str(expected):
                return False

    field_equals = match.get("field_equals")
    if isinstance(field_equals, dict):
        for key, expected in field_equals.items():
            if get_field_value(payload, key) != expected:
                return False

    field_in = match.get("field_in")
    if isinstance(field_in, dict):
        for key, expected_values in field_in.items():
            values = expected_values if isinstance(expected_values, list) else [expected_values]
            if get_field_value(payload, key) not in values:
                return False

    return True


class PartialFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"



def partial_format_template(template: str, values: dict[str, Any] | None) -> str:
    if not isinstance(values, dict):
        return template

    def stringify(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = get_field_value(values, key) if "." in key else values.get(key)
        if value is None or value == "":
            return "{" + key + "}"
        return stringify(value)

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}", replace, template)



def render_template_text(template: Any, payload: Any) -> str | None:
    if template is None:
        return None
    if not isinstance(template, str):
        return str(template)

    raw_template = template
    template = template.strip()
    if not template:
        return None
    if isinstance(payload, dict):
        return partial_format_template(raw_template, payload).strip()
    return template



def build_rendered_text_message(text: str | None) -> dict[str, Any] | None:
    if not isinstance(text, str) or not text.strip():
        return None
    return {"mode": "text", "text": text.strip()}



def build_template_with_cover_message(output: dict[str, Any], payload: Any) -> dict[str, Any] | None:
    text = render_template_text(output.get("template", ""), payload)
    cover_file = render_template_text(output.get("cover_file"), payload)
    if not cover_file:
        return build_rendered_text_message(text)

    if not isinstance(text, str) or not text.strip():
        return None

    text = text.strip()
    first_line, sep, rest = text.partition("\n")
    segments: list[dict[str, Any]] = []
    if first_line.strip():
        segments.append({"type": "text", "data": {"text": first_line.strip()}})

    image_spec: dict[str, Any] = {"type": "image", "file": cover_file}
    for source_key, target_key in {
        "cover_proxy": "proxy",
        "cover_cache": "cache",
        "cover_timeout": "timeout",
    }.items():
        if output.get(source_key) is not None:
            image_spec[target_key] = output.get(source_key)
    image_segment = render_message_segment(image_spec, payload)
    if image_segment is not None:
        segments.append(image_segment)

    if sep and rest.strip():
        segments.append({"type": "text", "data": {"text": rest.strip()}})

    if not segments:
        return build_rendered_text_message(text)
    return {"mode": "segments", "segments": segments, "fallback_text": text}



def render_message_segment(spec: Any, payload: Any) -> dict[str, Any] | None:
    if not isinstance(spec, dict):
        return None

    segment_type = str(spec.get("type") or "").strip().lower()
    if not segment_type:
        return None

    if segment_type == "text":
        text = render_template_text(spec.get("text") or spec.get("template"), payload)
        if not text:
            return None
        return {"type": "text", "data": {"text": text}}

    if segment_type == "image":
        file_value = render_template_text(spec.get("file"), payload)
        if not file_value:
            return None
        data: dict[str, Any] = {"file": file_value}
        if spec.get("proxy") is not None:
            data["proxy"] = 1 if safe_bool(spec.get("proxy")) else 0
        if spec.get("cache") is not None:
            data["cache"] = 1 if safe_bool(spec.get("cache")) else 0
        if spec.get("timeout") is not None:
            timeout_value = safe_int(spec.get("timeout"))
            if timeout_value is not None:
                data["timeout"] = timeout_value
        return {"type": "image", "data": data}

    return None



def render_rule_output(rule: dict[str, Any], payload: Any) -> dict[str, Any] | None:
    output = rule.get("output")
    if not isinstance(output, dict):
        return None

    kind = str(output.get("type", "summary") or "summary").strip().lower()
    if kind == "field":
        field = str(output.get("field", "")).strip()
        value = get_field_value(payload, field) if field else None
        if isinstance(value, str) and value.strip():
            return {"mode": "text", "text": value.strip()}
        if value is not None:
            return {"mode": "text", "text": str(value)}
        return None

    if kind == "template":
        if output.get("cover_file") is not None:
            return build_template_with_cover_message(output, payload)
        return build_rendered_text_message(render_template_text(output.get("template", ""), payload))

    if kind == "summary":
        return build_rendered_text_message(summarize_payload(payload).strip())

    if kind == "segments":
        segments_spec = output.get("segments")
        if not isinstance(segments_spec, list):
            return None
        segments: list[dict[str, Any]] = []
        for item in segments_spec:
            segment = render_message_segment(item, payload)
            if segment is not None:
                segments.append(segment)

        fallback_text = render_template_text(output.get("fallback_template"), payload)
        if not segments:
            return build_rendered_text_message(fallback_text)
        return {
            "mode": "segments",
            "segments": segments,
            "fallback_text": fallback_text.strip() if isinstance(fallback_text, str) and fallback_text.strip() else None,
        }

    return None



def get_rule_targets_spec(rule: dict[str, Any]) -> Any:
    output = rule.get("output") if isinstance(rule.get("output"), dict) else {}
    if isinstance(output, dict) and output.get("targets") is not None:
        return output.get("targets")
    if isinstance(output, dict) and output.get("target") is not None:
        return output.get("target")
    if rule.get("targets") is not None:
        return rule.get("targets")
    if rule.get("target") is not None:
        return rule.get("target")
    return None



def apply_rules(cfg: Config, handler: BaseHTTPRequestHandler, payload: Any) -> tuple[dict[str, Any] | None, list[dict[str, int]] | None]:
    rules_doc = load_rules(cfg.rules_path)
    rules = rules_doc.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if rule_matches(rule, handler, payload):
                rendered = render_rule_output(rule, payload)
                if rendered:
                    return rendered, resolve_target_specs(cfg, get_rule_targets_spec(rule), context=payload if isinstance(payload, dict) else None)

    default_rule = rules_doc.get("default")
    if isinstance(default_rule, dict):
        rendered = render_rule_output({"output": default_rule}, payload)
        if rendered:
            target_spec = default_rule.get("targets") if isinstance(default_rule, dict) else None
            if target_spec is None and isinstance(default_rule, dict):
                target_spec = default_rule.get("target")
            return rendered, resolve_target_specs(cfg, target_spec, context=payload if isinstance(payload, dict) else None)

    return None, None



def build_forward_message(cfg: Config, handler: BaseHTTPRequestHandler, payload: Any) -> tuple[dict[str, Any], list[dict[str, int]]]:
    rendered, rule_targets = apply_rules(cfg, handler, payload)
    if rendered:
        return rendered, (rule_targets or default_target_specs(cfg))

    parsed = urllib.parse.urlparse(handler.path)
    title = f"{cfg.title_prefix} 收到新 webhook"
    lines = [title]
    lines.append(f"路径: {parsed.path}")
    lines.append(f"来源IP: {handler.client_address[0]}")
    if cfg.include_headers:
        event = handler.headers.get("X-GitHub-Event") or handler.headers.get("X-Gitlab-Event") or handler.headers.get("X-Event-Key")
        ua = handler.headers.get("User-Agent")
        if event:
            lines.append(f"事件: {event}")
        if ua:
            lines.append(f"UA: {ua}")
    lines.append("")
    lines.append(summarize_payload(payload))
    return {"mode": "text", "text": "\n".join(lines).strip()}, default_target_specs(cfg)



def redact_header(name: str, value: str) -> str:
    if name.lower() in SENSITIVE_HEADERS:
        return "<redacted>"
    return value



def get_sanitized_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    return {key: redact_header(key, value) for key, value in handler.headers.items()}



def get_payload_summary(payload: Any, max_len: int = 500) -> str:
    text = summarize_payload(payload)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."



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
            "reason": "secret_not_configured",
        }

    if cfg.secret in {from_query, from_header}:
        return {
            "required": True,
            "provided_via_query": bool(from_query),
            "provided_via_header": bool(from_header),
            "status": "passed",
            "reason": "matched",
        }

    return {
        "required": True,
        "provided_via_query": bool(from_query),
        "provided_via_header": bool(from_header),
        "status": "failed",
        "reason": "invalid_secret",
    }



def build_request_record(handler: BaseHTTPRequestHandler, cfg: Config, request_id: str, parsed: urllib.parse.ParseResult, payload: Any, auth: dict[str, Any], outcome: str, note: str | None = None) -> dict[str, Any]:
    query_keys = sorted(set(urllib.parse.parse_qs(parsed.query).keys()))
    log_payload = sanitize_payload_for_log(payload)
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
            "query_keys": query_keys,
            "remote_ip": handler.client_address[0],
            "content_type": handler.headers.get("Content-Type", ""),
            "content_length": int(handler.headers.get("Content-Length", "0") or "0"),
            "headers": get_sanitized_headers(handler),
            "payload": log_payload,
            "payload_summary": get_payload_summary(log_payload),
        },
        "auth": auth,
        "target": {"private": cfg.private, "group": cfg.group},
    }



def is_aggregate_candidate(payload: Any) -> bool:
    return isinstance(payload, dict)



def truncate_middle(text: str, max_len: int = 96) -> str:
    if len(text) <= max_len:
        return text
    keep = max(8, (max_len - 3) // 2)
    return text[:keep] + "..." + text[-keep:]



def format_duration_human(value: Any) -> str:
    try:
        total_seconds = int(float(value))
    except Exception:  # noqa: BLE001
        return str(value)

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return "".join(parts)



def format_bytes_human(value: Any) -> str:
    try:
        size = float(value)
    except Exception:  # noqa: BLE001
        return str(value)

    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return str(value)



def get_file_name(path_value: Any) -> str | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    return Path(path_value).name or path_value



def format_money(value: Any) -> str:
    try:
        amount = float(value)
    except Exception:  # noqa: BLE001
        return str(value)
    if amount.is_integer():
        return str(int(amount))
    text = f"{amount:.2f}"
    return text.rstrip("0").rstrip(".")



def format_count_k(value: Any) -> str:
    try:
        count = float(value)
    except Exception:  # noqa: BLE001
        return str(value)
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    if count.is_integer():
        return str(int(count))
    return str(count)



def safe_int(value: Any) -> int | None:
    try:
        if value in {None, ""}:
            return None
        return int(float(value))
    except Exception:  # noqa: BLE001
        return None



def safe_float(value: Any) -> float | None:
    try:
        if value in {None, ""}:
            return None
        return float(value)
    except Exception:  # noqa: BLE001
        return None



def safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    return None



def build_guard_increment_line(captain: int, commander: int, governor: int) -> str:
    parts: list[str] = []
    if captain > 0:
        parts.append(f"新增舰长 ： {captain}")
    if commander > 0:
        parts.append(f"提督 ： {commander}")
    if governor > 0:
        parts.append(f"总督 ： {governor}")
    return " ｜ ".join(parts)



def load_markdown_price_table(path_value: str) -> dict[str, float]:
    cached = PRICE_TABLE_CACHE.get(path_value)
    if cached is not None:
        return cached

    prices: dict[str, float] = {}
    path = Path(path_value)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        PRICE_TABLE_CACHE[path_value] = prices
        return prices
    except Exception as exc:  # noqa: BLE001
        eprint(f"[price-table-error] {path_value}: {exc}")
        PRICE_TABLE_CACHE[path_value] = prices
        return prices

    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        parts = [part.strip() for part in line.strip("|").split("|")]
        if len(parts) != 2:
            continue
        name, price = parts
        if name in {"礼物名", "---"} or set(name) == {"-"}:
            continue
        try:
            prices[name] = float(price)
        except Exception:  # noqa: BLE001
            continue

    PRICE_TABLE_CACHE[path_value] = prices
    return prices



def derive_xml_path(relative_path: str, base_dir: str, strip_prefixes: list[str] | None = None) -> Path:
    normalized = relative_path.strip().replace("\\", "/")
    for prefix in strip_prefixes or []:
        prefix_text = str(prefix).strip().replace("\\", "/")
        if prefix_text and normalized.startswith(prefix_text):
            normalized = normalized[len(prefix_text) :]
            normalized = normalized.lstrip("/")
            break
    joined = Path(base_dir) / normalized
    return joined.with_suffix(".xml")



def compute_xml_live_stats(xml_path: Path, price_table_path: str | None = None, guard_level_map: dict[str, str] | None = None) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "xml_path": str(xml_path),
        "xml_exists": False,
        "bullet_count": "",
        "bullet_count_value": None,
        "bullet_count_display": "",
        "interaction_count": "",
        "interaction_count_value": None,
        "interaction_count_display": "",
        "sc_count": "",
        "sc_count_value": None,
        "sc_total": "",
        "sc_total_value": None,
        "guard_count": "",
        "guard_count_value": None,
        "guard_total": "",
        "guard_total_value": None,
        "gift_total": "",
        "gift_total_value": None,
        "total_revenue": "",
        "total_revenue_value": None,
        "gift_unknown_count": 0,
        "gift_unknown_summary": "",
        "gift_unknown_line": "",
        "guard_increment_line_block": "",
        "gift_total_label": "礼物营收",
        "total_revenue_label": "总营收",
        "_interaction_user_keys": [],
        "_gift_unknown_counts": {},
    }
    if not xml_path.exists():
        return stats

    try:
        root = ET.parse(xml_path).getroot()
    except Exception as exc:  # noqa: BLE001
        eprint(f"[xml-stats-error] {xml_path}: {exc}")
        return stats

    gift_prices = load_markdown_price_table(price_table_path) if price_table_path else {}
    guard_level_map = guard_level_map or {"3": "舰长", "2": "提督", "1": "总督"}

    bullet_count = 0
    sc_count = 0
    guard_count = 0
    captain_count = 0
    commander_count = 0
    governor_count = 0
    gift_total = 0.0
    sc_total = 0.0
    guard_total = 0.0
    gift_unknown: dict[str, int] = {}
    interaction_users: set[str] = set()

    def add_interaction_user(uid: Any = None, user: Any = None) -> None:
        uid_text = str(uid or "").strip()
        if uid_text:
            interaction_users.add(f"uid:{uid_text}")
            return
        user_text = str(user or "").strip()
        if user_text:
            interaction_users.add(f"user:{user_text}")

    for child in root:
        tag = child.tag.split("}")[-1]
        if tag == "d":
            bullet_count += 1
            p_value = str(child.attrib.get("p", ""))
            p_parts = p_value.split(",") if p_value else []
            add_interaction_user(p_parts[6] if len(p_parts) > 6 else "", child.attrib.get("user", ""))
        elif tag == "gift":
            add_interaction_user(child.attrib.get("uid", ""), child.attrib.get("user", ""))
            gift_name = child.attrib.get("giftname", "")
            try:
                gift_count = int(child.attrib.get("giftcount", "0") or "0")
            except Exception:  # noqa: BLE001
                gift_count = 0
            price = gift_prices.get(gift_name)
            if price is None:
                gift_unknown[gift_name] = gift_unknown.get(gift_name, 0) + gift_count
            else:
                gift_total += price * gift_count
        elif tag == "sc":
            add_interaction_user(child.attrib.get("uid", ""), child.attrib.get("user", ""))
            sc_count += 1
            try:
                sc_total += float(child.attrib.get("price", "0") or "0")
            except Exception:  # noqa: BLE001
                pass
        elif tag == "guard":
            add_interaction_user(child.attrib.get("uid", ""), child.attrib.get("user", ""))
            level = str(child.attrib.get("level", ""))
            try:
                count = int(child.attrib.get("count", "0") or "0")
            except Exception:  # noqa: BLE001
                count = 0
            guard_count += count
            if level == "3":
                captain_count += count
            elif level == "2":
                commander_count += count
            elif level == "1":
                governor_count += count
            level_name = guard_level_map.get(level, "")
            guard_total += gift_prices.get(level_name, 0.0) * count

    interaction_count = len(interaction_users)
    guard_increment_line = build_guard_increment_line(captain_count, commander_count, governor_count)
    gift_unknown_summary = "、".join(f"{name}×{count}" for name, count in sorted(gift_unknown.items()) if name)
    total_revenue = gift_total + sc_total + guard_total

    stats.update(
        {
            "xml_exists": True,
            "bullet_count": str(bullet_count),
            "bullet_count_value": bullet_count,
            "bullet_count_display": format_count_k(bullet_count),
            "interaction_count": str(interaction_count),
            "interaction_count_value": interaction_count,
            "interaction_count_display": format_count_k(interaction_count),
            "sc_count": str(sc_count),
            "sc_count_value": sc_count,
            "sc_total": format_money(sc_total),
            "sc_total_value": sc_total,
            "guard_count": str(guard_count),
            "guard_count_value": guard_count,
            "captain_count": str(captain_count),
            "commander_count": str(commander_count),
            "governor_count": str(governor_count),
            "guard_increment_line": guard_increment_line,
            "guard_increment_line_block": f"\n{guard_increment_line}" if guard_increment_line else "",
            "guard_total": format_money(guard_total),
            "guard_total_value": guard_total,
            "gift_total": format_money(gift_total),
            "gift_total_value": gift_total,
            "total_revenue": format_money(total_revenue),
            "total_revenue_value": total_revenue,
            "gift_unknown_count": len(gift_unknown),
            "gift_unknown_summary": gift_unknown_summary,
            "gift_unknown_line": f"\n未知礼物：{gift_unknown_summary}" if gift_unknown_summary else "",
            "gift_total_label": "礼物营收（已知）" if gift_unknown_summary else "礼物营收",
            "total_revenue_label": "总营收（已知）" if gift_unknown_summary else "总营收",
            "_interaction_user_keys": sorted(interaction_users),
            "_gift_unknown_counts": dict(sorted(gift_unknown.items())),
        }
    )
    return stats



def get_xml_live_stats(bucket: AggregateBucket, spec: dict[str, Any]) -> dict[str, Any]:
    merged_session_stats = bucket.computed.get(BILILIVE_SESSION_STATS_COMPUTED_KEY)
    if isinstance(merged_session_stats, dict) and isinstance(merged_session_stats.get("xml_live_stats"), dict):
        return merged_session_stats["xml_live_stats"]

    relative_path_field = str(spec.get("relative_path_field") or "EventData.RelativePath")
    base_dir = str(spec.get("base_dir") or "").strip()
    price_table_path = str(spec.get("gift_prices_markdown_path") or "").strip() or None
    strip_prefixes_raw = spec.get("strip_prefixes")
    strip_prefixes = [str(item) for item in strip_prefixes_raw] if isinstance(strip_prefixes_raw, list) else []
    guard_level_map_raw = spec.get("guard_level_map")
    guard_level_map = {str(k): str(v) for k, v in guard_level_map_raw.items()} if isinstance(guard_level_map_raw, dict) else None

    relative_path = get_bucket_field_value(bucket, relative_path_field)
    if not isinstance(relative_path, str) or not relative_path.strip() or not base_dir:
        return {
            "xml_exists": False,
            "xml_path": "",
            "bullet_count": "",
            "bullet_count_value": None,
            "bullet_count_display": "",
            "interaction_count": "",
            "interaction_count_value": None,
            "interaction_count_display": "",
            "sc_count": "",
            "sc_count_value": None,
            "sc_total": "",
            "sc_total_value": None,
            "captain_count": "",
            "commander_count": "",
            "governor_count": "",
            "guard_count": "",
            "guard_count_value": None,
            "guard_increment_line": "",
            "guard_increment_line_block": "",
            "guard_total": "",
            "guard_total_value": None,
            "gift_total": "",
            "gift_total_value": None,
            "gift_unknown_line": "",
            "total_revenue": "",
            "total_revenue_value": None,
            "gift_total_label": "礼物营收",
            "total_revenue_label": "总营收",
        }

    xml_path = derive_xml_path(relative_path, base_dir=base_dir, strip_prefixes=strip_prefixes)
    cache_key = json.dumps(
        {
            "source": "xml_live_stats",
            "xml_path": str(xml_path),
            "price_table_path": price_table_path,
            "guard_level_map": guard_level_map,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cached = bucket.computed.get(cache_key)
    if isinstance(cached, dict):
        return cached

    stats = compute_xml_live_stats(xml_path, price_table_path=price_table_path, guard_level_map=guard_level_map)
    bucket.computed[cache_key] = stats
    return stats



def get_bucket_field_value(bucket: AggregateBucket, field: str) -> Any:
    event_order = bucket.group_config.get("event_order")
    ordered_types: list[str] = []
    if isinstance(event_order, list):
        ordered_types.extend(str(item) for item in event_order)
    for event_type in bucket.events.keys():
        if event_type not in ordered_types:
            ordered_types.append(event_type)

    chosen = None
    for event_type in ordered_types:
        event = bucket.events.get(event_type)
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        value = get_field_value(payload, field)
        if value not in {None, ""}:
            chosen = value
    return chosen



def extract_bilibili_room_info_from_html(html: str) -> dict[str, Any]:
    if not isinstance(html, str) or not html.strip():
        return {}

    match = re.search(r"__NEPTUNE_IS_MY_WAIFU__\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not match:
        cover_match = re.search(r'"cover"\s*:\s*"([^"]+)"', html)
        if cover_match:
            try:
                return {"cover": json.loads('"' + cover_match.group(1) + '"')}
            except Exception:  # noqa: BLE001
                return {"cover": cover_match.group(1)}
        return {}

    try:
        data = json.loads(match.group(1))
    except Exception:  # noqa: BLE001
        return {}

    room_info = get_field_value(data, "roomInfoRes.data.room_info")
    return room_info if isinstance(room_info, dict) else {}



def fetch_bilibili_room_info(room_id: Any, timeout: float = 10.0, retries: int = 1) -> dict[str, Any]:
    room_id_str = str(room_id or "").strip()
    if not room_id_str:
        return {}

    page_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        "Referer": f"https://live.bilibili.com/{room_id_str}",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }

    api_url = f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={urllib.parse.quote(room_id_str)}"
    try:
        raw = fetch_text(api_url, headers=page_headers, timeout=timeout, retries=retries)
        api_obj = json.loads(raw)
        room_info = get_field_value(api_obj, "data.room_info")
        if isinstance(room_info, dict) and room_info:
            return room_info
    except Exception:  # noqa: BLE001
        pass

    html = fetch_text(f"https://live.bilibili.com/{urllib.parse.quote(room_id_str)}", headers=page_headers, timeout=timeout, retries=retries)
    return extract_bilibili_room_info_from_html(html)



def get_bilibili_room_info(bucket: AggregateBucket, spec: dict[str, Any]) -> dict[str, Any]:
    room_id_field = str(spec.get("room_id_field") or "EventData.RoomId")
    room_id = get_bucket_field_value(bucket, room_id_field)
    if room_id in {None, ""}:
        return {}

    room_id_str = str(room_id)
    cache_ttl_seconds = max(0, safe_int(spec.get("cache_ttl_seconds")) or 300)
    timeout = safe_float(spec.get("timeout")) or 10.0
    retries = max(0, safe_int(spec.get("retries")) or 1)
    cache_key = f"bilibili_room_info:{room_id_str}"
    now_ts = time.time()

    cached = bucket.computed.get(cache_key)
    if isinstance(cached, dict) and cached.get("expires_at", 0) > now_ts and isinstance(cached.get("data"), dict):
        return cached["data"]

    global_cached = BILIBILI_ROOM_INFO_CACHE.get(cache_key)
    if isinstance(global_cached, dict) and global_cached.get("expires_at", 0) > now_ts and isinstance(global_cached.get("data"), dict):
        bucket.computed[cache_key] = global_cached
        return global_cached["data"]

    room_info = fetch_bilibili_room_info(room_id_str, timeout=timeout, retries=retries)
    cache_record = {"expires_at": now_ts + cache_ttl_seconds, "data": room_info}
    bucket.computed[cache_key] = cache_record
    BILIBILI_ROOM_INFO_CACHE[cache_key] = cache_record
    return room_info



def truncate_value(value: Any, max_len: int) -> Any:
    if not isinstance(value, str):
        return value
    return truncate_middle(value, max_len=max_len)



def sanitize_log_value(value: Any, max_len: int = 240) -> Any:
    if isinstance(value, str):
        if looks_like_base64_blob(value):
            return summarize_base64_for_log(value)
        return truncate_value(value, max_len=max_len)
    if isinstance(value, list):
        return [sanitize_log_value(item, max_len=max_len) for item in value]
    if isinstance(value, dict):
        return {str(k): sanitize_log_value(v, max_len=max_len) for k, v in value.items()}
    return value



def parse_event_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None



def get_bucket_display_time(bucket: AggregateBucket, mode: str | None = None) -> str | None:
    parsed_values: list[datetime] = []
    raw_values: list[str] = []
    event_order = bucket.group_config.get("event_order")
    ordered_types: list[str] = []
    if isinstance(event_order, list):
        ordered_types.extend(str(item) for item in event_order)
    for event_type in bucket.events.keys():
        if event_type not in ordered_types:
            ordered_types.append(event_type)

    for event_type in ordered_types:
        event = bucket.events.get(event_type)
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            continue
        raw = payload.get("EventTimestamp")
        if isinstance(raw, str) and raw.strip():
            raw_values.append(raw.strip())
            parsed = parse_event_timestamp(raw)
            if parsed is not None:
                parsed_values.append(parsed)

    prefer = (mode or bucket.phase or "end").lower()
    if parsed_values:
        chosen = min(parsed_values) if prefer == "start" else max(parsed_values)
        return chosen.strftime("%Y-%m-%d %H:%M:%S")
    if raw_values:
        return raw_values[0] if prefer == "start" else raw_values[-1]
    return None



def apply_transform(value: Any, transform: str | None, spec: dict[str, Any] | None = None) -> Any:
    if transform is None:
        return value
    spec = spec or {}
    if transform == "basename":
        value = get_file_name(value)
    elif transform == "duration_human":
        value = format_duration_human(value)
    elif transform == "bytes_human":
        value = format_bytes_human(value)
    elif transform == "truncate":
        max_len = int(spec.get("max_len", 84) or 84)
        value = truncate_value(value, max_len)
    elif transform == "join_slash":
        if isinstance(value, list):
            value = "/".join(str(item) for item in value if item not in {None, ""})
    return value



def resolve_context_value(bucket: AggregateBucket, alias: str, spec: Any, context: dict[str, Any] | None = None) -> Any:
    context = context or {}
    if isinstance(spec, str):
        value = get_bucket_field_value(bucket, spec)
        return value if value not in {None, ""} else None

    if not isinstance(spec, dict):
        return spec

    source = str(spec.get("source") or "").strip().lower()
    value: Any = None
    if "value" in spec:
        value = spec.get("value")
    elif "field" in spec:
        value = get_bucket_field_value(bucket, str(spec.get("field")))
    elif "fields" in spec:
        fields = spec.get("fields")
        if isinstance(fields, list):
            values = [get_bucket_field_value(bucket, str(item)) for item in fields]
            sep = str(spec.get("separator", ""))
            value = sep.join(str(item) for item in values if item not in {None, ""})
    elif source == "template":
        value = render_template_text(spec.get("template"), context)
    elif source == "time":
        value = get_bucket_display_time(bucket, str(spec.get("mode") or bucket.phase))
    elif source == "phase":
        value = bucket.phase
    elif source == "event_types":
        value = sorted(bucket.events.keys())
    elif source == "group_name":
        value = bucket.group_name
    elif source == "request_count":
        value = len(bucket.request_ids)
    elif source == "event_count":
        value = len(bucket.events)
    elif source == "request_ids":
        value = list(bucket.request_ids)
    elif source == "xml_live_stats":
        stats = get_xml_live_stats(bucket, spec)
        stats_key = str(spec.get("key") or alias)
        value = stats.get(stats_key)
    elif source == "local_file_base64":
        path_value = render_template_text(spec.get("path") or spec.get("file") or spec.get("template"), context)
        if path_value:
            value = file_to_base64_uri(path_value)
    elif source == "room_cover_base64":
        room_id_value = context.get("room_id")
        if room_id_value in {None, ""}:
            room_id_field = str(spec.get("room_id_field") or "EventData.RoomId")
            room_id_value = get_bucket_field_value(bucket, room_id_field)
        index_path = render_template_text(spec.get("index_path") or "/opt/WebhookToNapcat/cover/index.json", context) or "/opt/WebhookToNapcat/cover/index.json"
        cover_path = resolve_room_cover_path(room_id_value, index_path=index_path)
        if cover_path:
            value = file_to_base64_uri(cover_path)

    transform = spec.get("transform") if isinstance(spec.get("transform"), str) else None
    value = apply_transform(value, transform, spec)
    if value in {None, ""} and "default" in spec:
        value = spec.get("default")
    return value



def build_aggregate_context(bucket: AggregateBucket) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "phase": bucket.phase,
        "group_name": bucket.group_name,
        "event_types": ", ".join(sorted(bucket.events.keys())),
        "event_types_json": json.dumps(sorted(bucket.events.keys()), ensure_ascii=False),
        "request_count": len(bucket.request_ids),
        "event_count": len(bucket.events),
    }

    area_parent = get_bucket_field_value(bucket, "EventData.AreaNameParent")
    area_child = get_bucket_field_value(bucket, "EventData.AreaNameChild")
    relative_path = get_bucket_field_value(bucket, "EventData.RelativePath")
    duration_raw = get_bucket_field_value(bucket, "EventData.Duration")
    file_size_raw = get_bucket_field_value(bucket, "EventData.FileSize")
    recording_raw = get_bucket_field_value(bucket, "EventData.Recording")
    streaming_raw = get_bucket_field_value(bucket, "EventData.Streaming")
    ctx.update(
        {
            "name": get_bucket_field_value(bucket, "EventData.Name") or "未知主播",
            "title": get_bucket_field_value(bucket, "EventData.Title") or "（无标题）",
            "room_id": get_bucket_field_value(bucket, "EventData.RoomId"),
            "short_id": get_bucket_field_value(bucket, "EventData.ShortId"),
            "session_id": get_bucket_field_value(bucket, "EventData.SessionId"),
            "area_parent": area_parent,
            "area_child": area_child,
            "area": "/".join(str(item) for item in [area_parent, area_child] if item not in {None, ""}),
            "file_path": relative_path,
            "file_name": truncate_value(get_file_name(relative_path), 84),
            "duration": format_duration_human(duration_raw),
            "duration_seconds": safe_float(duration_raw),
            "file_size": format_bytes_human(file_size_raw),
            "file_size_bytes": safe_int(file_size_raw),
            "recording": safe_bool(recording_raw),
            "streaming": safe_bool(streaming_raw),
            "has_stream_ended": "StreamEnded" in bucket.events,
            "has_file_closed": "FileClosed" in bucket.events,
            "has_session_ended": "SessionEnded" in bucket.events,
            "time": get_bucket_display_time(bucket),
        }
    )

    merged_session_stats = bucket.computed.get(BILILIVE_SESSION_STATS_COMPUTED_KEY)
    if isinstance(merged_session_stats, dict):
        merged_duration = safe_float(merged_session_stats.get("duration_seconds"))
        merged_file_size = safe_int(merged_session_stats.get("file_size_bytes"))
        if merged_duration is not None:
            ctx["duration_seconds"] = merged_duration
            ctx["duration"] = format_duration_human(merged_duration)
        if merged_file_size is not None:
            ctx["file_size_bytes"] = merged_file_size
            ctx["file_size"] = format_bytes_human(merged_file_size)
        for key in (
            "recording_segment_count",
            "recording_segment_paths",
            "recording_segment_names",
            "recording_segment_request_ids",
            "recording_segment_scope",
        ):
            if key in merged_session_stats:
                ctx[key] = merged_session_stats[key]

    context_spec = bucket.group_config.get("context")
    if isinstance(context_spec, dict):
        for alias, spec in context_spec.items():
            ctx[str(alias)] = resolve_context_value(bucket, alias=str(alias), spec=spec, context=ctx)

    return ctx



def render_output_spec(output: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    return render_rule_output({"output": output}, context)



def aggregate_output_matches(rule: dict[str, Any], context: dict[str, Any], event_types: set[str]) -> bool:
    match = rule.get("match")
    if not isinstance(match, dict):
        return True

    all_types = match.get("event_types_all")
    if isinstance(all_types, list):
        required = {str(item) for item in all_types}
        if not required.issubset(event_types):
            return False

    any_types = match.get("event_types_any")
    if isinstance(any_types, list):
        allowed = {str(item) for item in any_types}
        if event_types.isdisjoint(allowed):
            return False

    none_types = match.get("event_types_none")
    if isinstance(none_types, list):
        denied = {str(item) for item in none_types}
        if event_types & denied:
            return False

    field_equals = match.get("field_equals")
    if isinstance(field_equals, dict):
        for key, expected in field_equals.items():
            if context.get(key) != expected:
                return False

    field_in = match.get("field_in")
    if isinstance(field_in, dict):
        for key, expected_values in field_in.items():
            values = expected_values if isinstance(expected_values, list) else [expected_values]
            if context.get(key) not in values:
                return False

    return True



def get_output_targets_spec(rule: dict[str, Any], output: dict[str, Any] | None = None) -> Any:
    output = output if isinstance(output, dict) else (rule.get("output") if isinstance(rule.get("output"), dict) else {})
    if isinstance(output, dict) and output.get("targets") is not None:
        return output.get("targets")
    if isinstance(output, dict) and output.get("target") is not None:
        return output.get("target")
    if rule.get("targets") is not None:
        return rule.get("targets")
    if rule.get("target") is not None:
        return rule.get("target")
    return None



def select_aggregate_output(bucket: AggregateBucket, context: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], Any]:
    group = bucket.group_config
    outputs = group.get("outputs")
    event_types = set(bucket.events.keys())
    suppress_event_types = [str(item) for item in group.get("suppress_event_types", []) if isinstance(item, (str, int, float))]

    if isinstance(outputs, list):
        for item in outputs:
            if not isinstance(item, dict):
                continue
            if not aggregate_output_matches(item, context, event_types):
                continue
            output = item.get("output")
            if not isinstance(output, dict):
                continue
            rendered = render_output_spec(output, context)
            if rendered:
                return rendered, suppress_event_types, get_output_targets_spec(item, output)

    output = group.get("output")
    if isinstance(output, dict):
        rendered = render_output_spec(output, context)
        if rendered:
            return rendered, suppress_event_types, get_output_targets_spec(group, output)

    return None, suppress_event_types, None



def compute_aggregate_phase(group: dict[str, Any], payload: dict[str, Any]) -> str:
    phase = group.get("phase")
    if isinstance(phase, str) and phase.strip():
        return phase.strip()
    event_type = payload.get("EventType")
    if isinstance(event_type, str) and event_type.lower().endswith("started"):
        return "start"
    if isinstance(event_type, str) and event_type.lower().endswith("ended"):
        return "end"
    return "group"



def build_aggregate_key(group: dict[str, Any], payload: dict[str, Any], phase: str) -> str:
    key_fields = group.get("key_fields")
    parts = [f"aggregate:{group.get('name', 'group')}:{phase}"]
    if isinstance(key_fields, list):
        for field in key_fields:
            value = get_field_value(payload, str(field))
            parts.append(str(value) if value not in {None, ""} else "_")
    return ":".join(parts)



def get_aggregate_group(cfg: Config, handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    rules_doc = load_rules(cfg.rules_path)
    aggregate = rules_doc.get("aggregate")
    if not isinstance(aggregate, dict):
        return None, cfg.aggregate_window_ms
    if aggregate.get("enabled", True) is False:
        return None, cfg.aggregate_window_ms

    groups = aggregate.get("groups")
    default_window = int(aggregate.get("window_ms", cfg.aggregate_window_ms) or cfg.aggregate_window_ms)
    if not isinstance(groups, list):
        return None, default_window

    for group in groups:
        if not isinstance(group, dict):
            continue
        if group.get("enabled", True) is False:
            continue
        if rule_matches({"match": group.get("match")}, handler, payload):
            window_ms = int(group.get("window_ms", default_window) or default_window)
            return group, max(0, window_ms)

    return None, default_window



def should_flush_aggregate_immediately(bucket: AggregateBucket, event_type: str) -> bool:
    triggers = bucket.group_config.get("flush_immediately_on_event_types")
    if not isinstance(triggers, list):
        return False

    event_type = str(event_type or "").strip()
    if not event_type:
        return False

    for item in triggers:
        if event_type == str(item or "").strip():
            return True
    return False



def build_aggregate_message(bucket: AggregateBucket) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    context = build_aggregate_context(bucket)
    message, suppressed, targets_spec = select_aggregate_output(bucket, context)
    meta: dict[str, Any] = {
        "enabled": True,
        "group_name": bucket.group_name,
        "group_key": bucket.key,
        "phase": bucket.phase,
        "event_types": sorted(bucket.events.keys()),
        "suppressed": suppressed,
        "context": sanitize_log_value(context),
        "targets_spec": targets_spec,
    }
    return message, meta



def build_bililive_notification_key(aggregate_meta: dict[str, Any]) -> str | None:
    group_name = str(aggregate_meta.get("group_name") or "").strip()
    if group_name not in {"bililive_start", "bililive_end"}:
        return None

    context = aggregate_meta.get("context") if isinstance(aggregate_meta.get("context"), dict) else {}
    room_id = context.get("room_id")
    title = str(context.get("title") or "").strip()
    name = str(context.get("name") or "").strip()
    if room_id in {None, ""} or not title:
        return None
    return f"bililive:{room_id}:{name}:{title}"



def build_aggregate_message_record(bucket: AggregateBucket, aggregate_meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": now_iso(),
        "request_id": bucket.request_ids[0] if bucket.request_ids else uuid4().hex,
        "related_request_ids": bucket.request_ids,
        "layer": "message",
        "stage": "egress",
        "outcome": "queued",
        "request": {
            "path": bucket.request_path,
            "remote_ip": bucket.remote_ip,
        },
        "auth": bucket.auth,
        "payload_summary": "\n---\n".join(bucket.payload_summaries[-3:]),
        "target": bucket.target,
        "aggregate": aggregate_meta,
    }



def merge_aggregate_bucket(target: AggregateBucket, source: AggregateBucket | None) -> AggregateBucket:
    if source is None:
        return target

    for event_type, event in source.events.items():
        target.events[event_type] = event
    for request_id in source.request_ids:
        if request_id not in target.request_ids:
            target.request_ids.append(request_id)
    target.payload_summaries.extend(source.payload_summaries)
    target.request_path = source.request_path
    target.remote_ip = source.remote_ip
    target.auth = source.auth
    target.target = source.target
    target.computed.clear()
    return target



def get_xml_metrics_for_bucket(bucket: AggregateBucket) -> dict[str, Any]:
    context_spec = bucket.group_config.get("context")
    if isinstance(context_spec, dict):
        for spec in context_spec.values():
            if isinstance(spec, dict) and spec.get("source") == "xml_live_stats":
                return get_xml_live_stats(bucket, spec)
    return {}



def get_event_payload(bucket: AggregateBucket, event_type: str) -> dict[str, Any] | None:
    event = bucket.events.get(event_type)
    payload = event.get("payload") if isinstance(event, dict) else None
    return payload if isinstance(payload, dict) else None


def get_live_session_segment_ttl_ms(cfg: Config) -> int:
    configured = safe_int(os.getenv("WEBHOOK_LIVE_SESSION_SEGMENT_TTL_MS"))
    if configured is not None:
        return max(0, configured)
    return 18 * 60 * 60 * 1000


def cleanup_expired_live_session_segments(now_ts: float | None = None) -> None:
    now_ts = time.time() if now_ts is None else now_ts
    with LIVE_SESSION_SEGMENT_LOCK:
        for key in [key for key, acc in LIVE_SESSION_SEGMENTS.items() if acc.expires_at <= now_ts]:
            LIVE_SESSION_SEGMENTS.pop(key, None)


def build_live_session_segment_record(bucket: AggregateBucket) -> dict[str, Any] | None:
    payload = get_event_payload(bucket, "FileClosed")
    if not isinstance(payload, dict):
        return None

    relative_path = get_field_value(payload, "EventData.RelativePath")
    duration_seconds = safe_float(get_field_value(payload, "EventData.Duration"))
    file_size_bytes = safe_int(get_field_value(payload, "EventData.FileSize"))
    if relative_path in {None, ""} and duration_seconds is None and file_size_bytes is None:
        return None

    segment_id = str(relative_path or "").strip()
    if not segment_id:
        event = bucket.events.get("FileClosed") if isinstance(bucket.events.get("FileClosed"), dict) else {}
        segment_id = str(event.get("request_id") or (bucket.request_ids[-1] if bucket.request_ids else uuid4().hex))

    xml_stats = get_xml_metrics_for_bucket(bucket)
    return {
        "segment_id": segment_id,
        "relative_path": str(relative_path or ""),
        "file_name": get_file_name(relative_path),
        "duration_seconds": duration_seconds,
        "file_size_bytes": file_size_bytes,
        "xml_live_stats": copy.deepcopy(xml_stats) if isinstance(xml_stats, dict) else {},
        "request_ids": list(bucket.request_ids),
        "created_at": time.time(),
    }


def merge_xml_live_stats_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    xml_paths: list[str] = []
    interaction_users: set[str] = set()
    gift_unknown: dict[str, int] = {}
    bullet_count = 0
    sc_count = 0
    guard_count = 0
    captain_count = 0
    commander_count = 0
    governor_count = 0
    gift_total = 0.0
    sc_total = 0.0
    guard_total = 0.0
    xml_exists = False

    for record in records:
        stats = record.get("xml_live_stats") if isinstance(record.get("xml_live_stats"), dict) else {}
        if not isinstance(stats, dict):
            continue
        xml_exists = xml_exists or bool(stats.get("xml_exists"))
        xml_path = stats.get("xml_path")
        if isinstance(xml_path, str) and xml_path and xml_path not in xml_paths:
            xml_paths.append(xml_path)
        bullet_count += safe_int(stats.get("bullet_count_value")) or 0
        sc_count += safe_int(stats.get("sc_count_value")) or 0
        guard_count += safe_int(stats.get("guard_count_value")) or 0
        captain_count += safe_int(stats.get("captain_count")) or 0
        commander_count += safe_int(stats.get("commander_count")) or 0
        governor_count += safe_int(stats.get("governor_count")) or 0
        gift_total += safe_float(stats.get("gift_total_value")) or 0.0
        sc_total += safe_float(stats.get("sc_total_value")) or 0.0
        guard_total += safe_float(stats.get("guard_total_value")) or 0.0

        raw_users = stats.get("_interaction_user_keys")
        if isinstance(raw_users, list):
            interaction_users.update(str(item) for item in raw_users if str(item).strip())
        else:
            fallback_count = safe_int(stats.get("interaction_count_value")) or 0
            for index in range(fallback_count):
                interaction_users.add(f"segment:{record.get('segment_id')}:{index}")

        raw_unknown = stats.get("_gift_unknown_counts")
        if isinstance(raw_unknown, dict):
            for name, count in raw_unknown.items():
                name_text = str(name or "").strip()
                if not name_text:
                    continue
                gift_unknown[name_text] = gift_unknown.get(name_text, 0) + (safe_int(count) or 0)

    interaction_count = len(interaction_users)
    total_revenue = gift_total + sc_total + guard_total
    gift_unknown_summary = "、".join(f"{name}×{count}" for name, count in sorted(gift_unknown.items()) if name and count)
    guard_increment_line = build_guard_increment_line(captain_count, commander_count, governor_count)

    return {
        "xml_path": " | ".join(xml_paths),
        "xml_paths": xml_paths,
        "xml_exists": xml_exists,
        "bullet_count": str(bullet_count),
        "bullet_count_value": bullet_count,
        "bullet_count_display": format_count_k(bullet_count),
        "interaction_count": str(interaction_count),
        "interaction_count_value": interaction_count,
        "interaction_count_display": format_count_k(interaction_count),
        "sc_count": str(sc_count),
        "sc_count_value": sc_count,
        "sc_total": format_money(sc_total),
        "sc_total_value": sc_total,
        "captain_count": str(captain_count),
        "commander_count": str(commander_count),
        "governor_count": str(governor_count),
        "guard_count": str(guard_count),
        "guard_count_value": guard_count,
        "guard_increment_line": guard_increment_line,
        "guard_increment_line_block": f"\n{guard_increment_line}" if guard_increment_line else "",
        "guard_total": format_money(guard_total),
        "guard_total_value": guard_total,
        "gift_total": format_money(gift_total),
        "gift_total_value": gift_total,
        "total_revenue": format_money(total_revenue),
        "total_revenue_value": total_revenue,
        "gift_unknown_count": len(gift_unknown),
        "gift_unknown_summary": gift_unknown_summary,
        "gift_unknown_line": f"\n未知礼物：{gift_unknown_summary}" if gift_unknown_summary else "",
        "gift_total_label": "礼物营收（已知）" if gift_unknown_summary else "礼物营收",
        "total_revenue_label": "总营收（已知）" if gift_unknown_summary else "总营收",
        "_interaction_user_keys": sorted(interaction_users),
        "_gift_unknown_counts": dict(sorted(gift_unknown.items())),
    }


def remember_live_session_segment(cfg: Config, notify_key: str | None, bucket: AggregateBucket) -> bool:
    if not notify_key:
        return False
    record = build_live_session_segment_record(bucket)
    if record is None:
        return False

    ttl_ms = get_live_session_segment_ttl_ms(cfg)
    now_ts = time.time()
    cleanup_expired_live_session_segments(now_ts)
    with LIVE_SESSION_SEGMENT_LOCK:
        acc = LIVE_SESSION_SEGMENTS.get(notify_key)
        if acc is None or acc.expires_at <= now_ts:
            acc = LiveSessionSegmentAccumulator(key=notify_key, expires_at=now_ts + ttl_ms / 1000)
            LIVE_SESSION_SEGMENTS[notify_key] = acc
        else:
            acc.expires_at = max(acc.expires_at, now_ts + ttl_ms / 1000)
        acc.segments[str(record["segment_id"])] = record
    return True


def clear_live_session_segments(notify_key: str | None) -> None:
    if not notify_key:
        return
    with LIVE_SESSION_SEGMENT_LOCK:
        LIVE_SESSION_SEGMENTS.pop(notify_key, None)


def apply_live_session_segments_to_bucket(notify_key: str | None, bucket: AggregateBucket) -> dict[str, Any] | None:
    if not notify_key or bucket.group_name != "bililive_end":
        return None

    cleanup_expired_live_session_segments()
    with LIVE_SESSION_SEGMENT_LOCK:
        acc = LIVE_SESSION_SEGMENTS.get(notify_key)
        records_by_id = copy.deepcopy(acc.segments) if acc is not None else {}

    current_record = build_live_session_segment_record(bucket)
    if current_record is not None:
        records_by_id[str(current_record["segment_id"])] = current_record

    records = list(records_by_id.values())
    if not records:
        return None

    duration_values = [safe_float(record.get("duration_seconds")) for record in records]
    size_values = [safe_int(record.get("file_size_bytes")) for record in records]
    duration_total = sum(value for value in duration_values if value is not None)
    size_total = sum(value for value in size_values if value is not None)
    paths = [str(record.get("relative_path") or "") for record in records if str(record.get("relative_path") or "").strip()]
    names = [str(record.get("file_name") or "") for record in records if str(record.get("file_name") or "").strip()]
    request_ids: list[str] = []
    for record in records:
        raw_request_ids = record.get("request_ids")
        for request_id in raw_request_ids if isinstance(raw_request_ids, list) else []:
            request_id_text = str(request_id)
            if request_id_text not in request_ids:
                request_ids.append(request_id_text)

    merged = {
        "recording_segment_count": len(records),
        "recording_segment_paths": " | ".join(paths),
        "recording_segment_names": " | ".join(names),
        "recording_segment_request_ids": request_ids,
        "recording_segment_scope": "live_session" if len(records) > 1 else "single_segment",
        "duration_seconds": duration_total if any(value is not None for value in duration_values) else None,
        "file_size_bytes": size_total if any(value is not None for value in size_values) else None,
        "xml_live_stats": merge_xml_live_stats_records(records),
    }
    bucket.computed[BILILIVE_SESSION_STATS_COMPUTED_KEY] = merged
    return merged


def build_end_bucket_metrics(bucket: AggregateBucket) -> dict[str, Any]:
    context = build_aggregate_context(bucket)
    xml_metrics = get_xml_metrics_for_bucket(bucket)
    return {
        "xml_exists": bool(xml_metrics.get("xml_exists")),
        "duration_seconds": safe_float(context.get("duration_seconds")),
        "file_size_bytes": safe_int(context.get("file_size_bytes")),
        "interaction_count_value": safe_int(xml_metrics.get("interaction_count_value")),
        "bullet_count_value": safe_int(xml_metrics.get("bullet_count_value")),
        "sc_total_value": safe_float(xml_metrics.get("sc_total_value")),
        "total_revenue_value": safe_float(xml_metrics.get("total_revenue_value")),
        "streaming": context.get("streaming"),
        "recording": context.get("recording"),
        "has_stream_ended": bool(context.get("has_stream_ended")),
        "has_file_closed": bool(context.get("has_file_closed")),
        "has_session_ended": bool(context.get("has_session_ended")),
    }



def build_end_bucket_score(bucket: AggregateBucket) -> tuple[int, int, int, int, int, int, int]:
    metrics = build_end_bucket_metrics(bucket)
    interaction_count = safe_int(metrics.get("interaction_count_value"))
    bullet_count = safe_int(metrics.get("bullet_count_value"))
    file_size_bytes = safe_int(metrics.get("file_size_bytes"))
    return (
        1 if metrics.get("has_file_closed") else 0,
        1 if metrics.get("xml_exists") else 0,
        interaction_count if interaction_count is not None else -1,
        bullet_count if bullet_count is not None else -1,
        int(round((safe_float(metrics.get("total_revenue_value")) or 0.0) * 100)),
        int(round(safe_float(metrics.get("duration_seconds")) or 0.0)),
        file_size_bytes if file_size_bytes is not None else -1,
    )



def is_end_candidate_bucket(bucket: AggregateBucket) -> bool:
    return bool(set(bucket.events.keys()) & {"FileClosed", "SessionEnded"})



def get_end_tail_suppress_policy(bucket: AggregateBucket) -> dict[str, Any] | None:
    raw = bucket.group_config.get("tail_suppress")
    if isinstance(raw, dict):
        if raw.get("enabled") is False:
            return None
        return {
            "require_streaming_false": safe_bool(raw.get("require_streaming_false")) if raw.get("require_streaming_false") is not None else True,
            "duration_seconds_max": safe_float(raw.get("duration_seconds_max")),
            "file_size_bytes_max": safe_int(raw.get("file_size_bytes_max")),
            "interaction_count_max": safe_int(raw.get("interaction_count_max")),
            "bullet_count_max": safe_int(raw.get("bullet_count_max")),
            "sc_total_max": safe_float(raw.get("sc_total_max")),
            "total_revenue_max": safe_float(raw.get("total_revenue_max")),
        }

    if bucket.group_name != "bililive_end":
        return None

    return {
        "require_streaming_false": True,
        "duration_seconds_max": 15.0,
        "file_size_bytes_max": 5 * 1024 * 1024,
        "interaction_count_max": 20,
        "bullet_count_max": 30,
        "sc_total_max": 0.0,
        "total_revenue_max": 0.0,
    }



def is_trivial_trailing_end_bucket(bucket: AggregateBucket) -> bool:
    policy = get_end_tail_suppress_policy(bucket)
    if not policy:
        return False

    metrics = build_end_bucket_metrics(bucket)
    checks: list[bool] = []

    if policy.get("require_streaming_false"):
        if metrics.get("streaming") is not False:
            return False
        checks.append(True)

    threshold_fields = {
        "duration_seconds": policy.get("duration_seconds_max"),
        "file_size_bytes": policy.get("file_size_bytes_max"),
        "interaction_count_value": policy.get("interaction_count_max"),
        "bullet_count_value": policy.get("bullet_count_max"),
        "sc_total_value": policy.get("sc_total_max"),
        "total_revenue_value": policy.get("total_revenue_max"),
    }
    for field, max_value in threshold_fields.items():
        if max_value is None:
            continue
        value = metrics.get(field)
        if value is None:
            return False
        checks.append(value <= max_value)

    return bool(checks) and all(checks)



def build_start_bucket_score(bucket: AggregateBucket) -> tuple[int, int, int, int]:
    event_types = set(bucket.events.keys())
    return (
        1 if "StreamStarted" in event_types else 0,
        1 if "SessionStarted" in event_types else 0,
        1 if "FileOpening" in event_types else 0,
        len(bucket.request_ids),
    )



def get_recent_start_suppress_window_ms(cfg: Config, bucket: AggregateBucket) -> int:
    group_window_ms = int(bucket.group_config.get("window_ms", cfg.aggregate_window_ms) or cfg.aggregate_window_ms)
    base_window = max(0, int(cfg.notify_debounce_ms or 0))
    configured_window = safe_int(bucket.group_config.get("recent_start_suppress_ms"))
    if configured_window is not None:
        return max(0, configured_window)

    # A bililive start notification is a live-session level signal, not a recording-file
    # signal.  BililiveRecorder may later emit SessionStarted/FileOpening again when it
    # splits/reopens the recording while the stream is still live.  Keep the already
    # forwarded start active long enough to suppress those follow-up starts; a real end
    # notification clears it earlier.
    return max(12 * 60 * 60 * 1000, base_window, max(0, group_window_ms))



def get_recent_forwarded_start(notify_key: str) -> RecentForwardedStart | None:
    now_ts = time.time()
    with RECENT_START_LOCK:
        recent = RECENT_FORWARDED_STARTS.get(notify_key)
        if recent is None:
            return None
        if recent.expires_at <= now_ts:
            RECENT_FORWARDED_STARTS.pop(notify_key, None)
            return None
        return recent



def remember_recent_forwarded_start(cfg: Config, notify_key: str, bucket: AggregateBucket) -> None:
    if not notify_key:
        return

    recent = RecentForwardedStart(
        key=notify_key,
        score=build_start_bucket_score(bucket),
        expires_at=time.time() + get_recent_start_suppress_window_ms(cfg, bucket) / 1000,
    )
    with RECENT_START_LOCK:
        RECENT_FORWARDED_STARTS[notify_key] = recent



def clear_recent_forwarded_start(notify_key: str) -> None:
    if not notify_key:
        return
    with RECENT_START_LOCK:
        RECENT_FORWARDED_STARTS.pop(notify_key, None)



def should_suppress_recent_forwarded_start_candidate(
    recent_score: tuple[int, int, int, int], candidate_bucket: AggregateBucket
) -> bool:
    # Once a start notification for the same room/name/title has been forwarded, every
    # later start-shaped event with the same notify key is a duplicate only for the
    # current live lifecycle. A true end notification clears the remembered start, so a
    # reconnecting StreamStarted after 下播 has been forwarded is allowed through.
    # This intentionally suppresses even a stronger later candidate (for example
    # StreamStarted after SessionStarted) while the lifecycle is still active, because
    # the user-visible "开播" notification has already been sent.
    _ = recent_score
    _ = candidate_bucket
    return True



def is_true_bililive_start_bucket(bucket: AggregateBucket) -> bool:
    if bucket.group_name != "bililive_start":
        return False
    return "StreamStarted" in bucket.events



def is_recording_segment_start_bucket(bucket: AggregateBucket) -> bool:
    if bucket.group_name != "bililive_start":
        return False
    event_types = set(bucket.events.keys())
    if "StreamStarted" in event_types:
        return False
    return bool(event_types & {"SessionStarted", "FileOpening"})



def get_recent_end_suppress_window_ms(cfg: Config) -> int:
    base_window = max(0, int(cfg.notify_debounce_ms or 0))
    # Keep the recent end lifecycle around long enough to suppress recorder tail
    # jitter.  In particular, a StreamStarted emitted within 5 minutes after a
    # forwarded true end is considered post-end noise and is suppressed directly.
    return max(300_000, base_window * 20)



def get_recent_forwarded_end(notify_key: str) -> RecentForwardedEnd | None:
    now_ts = time.time()
    with RECENT_END_LOCK:
        recent = RECENT_FORWARDED_ENDS.get(notify_key)
        if recent is None:
            return None
        if recent.expires_at <= now_ts:
            RECENT_FORWARDED_ENDS.pop(notify_key, None)
            return None
        return recent



def remember_recent_forwarded_end(cfg: Config, notify_key: str, bucket: AggregateBucket) -> None:
    if not notify_key:
        return

    recent = RecentForwardedEnd(
        key=notify_key,
        score=build_end_bucket_score(bucket),
        expires_at=time.time() + get_recent_end_suppress_window_ms(cfg) / 1000,
    )
    with RECENT_END_LOCK:
        RECENT_FORWARDED_ENDS[notify_key] = recent



def clear_recent_forwarded_end(notify_key: str) -> None:
    if not notify_key:
        return
    with RECENT_END_LOCK:
        RECENT_FORWARDED_ENDS.pop(notify_key, None)



def get_start_after_end_confirm_window_ms(cfg: Config, bucket: AggregateBucket | None = None) -> int:
    configured_window = None
    if bucket is not None:
        configured_window = safe_int(bucket.group_config.get("post_end_start_confirm_ms"))
    if configured_window is None:
        configured_window = safe_int(os.getenv("WEBHOOK_POST_END_START_CONFIRM_MS"))
    if configured_window is not None:
        return max(0, configured_window)
    return 45_000



def flush_pending_start_after_end(cfg: Config, notify_key: str) -> None:
    with PENDING_START_AFTER_END_LOCK:
        pending = PENDING_START_AFTER_END_NOTIFICATIONS.pop(notify_key, None)
    if pending is None:
        return

    # The suspected reconnect survived the post-end confirmation window without a
    # follow-up true end, so treat it as a real reconnect/new start.
    clear_recent_forwarded_end(notify_key)
    deliver_aggregate_bucket(
        cfg,
        pending.bucket,
        debounce_info={
            "mode": "post_end_start_confirm_window",
            "status": "expired_forwarded_reconnect_start",
            "key": notify_key,
            "window_ms": pending.window_ms,
        },
    )



def hold_start_after_recent_end(cfg: Config, notify_key: str, bucket: AggregateBucket, preview_text: str) -> bool:
    window_ms = get_start_after_end_confirm_window_ms(cfg, bucket)
    if window_ms <= 0:
        return False

    created_pending = False
    merged_pending = False
    now_ts = time.time()
    with PENDING_START_AFTER_END_LOCK:
        pending = PENDING_START_AFTER_END_NOTIFICATIONS.get(notify_key)
        if pending is not None and pending.expires_at <= now_ts:
            pending = None
            PENDING_START_AFTER_END_NOTIFICATIONS.pop(notify_key, None)
        if pending is None:
            timer = threading.Timer(window_ms / 1000, flush_pending_start_after_end, args=(cfg, notify_key))
            timer.daemon = True
            pending = PendingStartAfterEndNotification(
                key=notify_key,
                bucket=bucket,
                expires_at=time.time() + window_ms / 1000,
                timer=timer,
                window_ms=window_ms,
            )
            PENDING_START_AFTER_END_NOTIFICATIONS[notify_key] = pending
            timer.start()
            created_pending = True
        else:
            merge_aggregate_bucket(pending.bucket, bucket)
            merged_pending = True

    message, aggregate_meta = build_aggregate_message(bucket)
    message_record = build_aggregate_message_record(bucket, aggregate_meta)
    message_record["forward_text"] = preview_text
    if message:
        message_record["message_mode"] = message.get("mode")
        message_record["message_summary"] = summarize_message_for_log(message)
    message_record["outcome"] = "held"
    message_record["debounce"] = {
        "mode": "post_end_start_confirm_window",
        "status": "held_reconnect_start_after_recent_end" if created_pending else "merged_reconnect_start_after_recent_end",
        "key": notify_key,
        "window_ms": window_ms,
        "remaining_ms": int((pending.expires_at - time.time()) * 1000),
    }
    if created_pending:
        message_record["debounce"]["created_pending"] = True
    if merged_pending:
        message_record["debounce"]["merged_pending"] = True
    append_message_log(cfg, message_record)
    eprint(
        json.dumps(
            {
                "event": "aggregate_post_end_start_held",
                "group_key": bucket.key,
                "group_name": bucket.group_name,
                "request_ids": bucket.request_ids,
                "debounce": message_record["debounce"],
            },
            ensure_ascii=False,
        )
    )
    return True



def cancel_pending_start_after_end(
    cfg: Config, notify_key: str, *, reason: str, end_bucket: AggregateBucket | None = None
) -> bool:
    with PENDING_START_AFTER_END_LOCK:
        pending = PENDING_START_AFTER_END_NOTIFICATIONS.pop(notify_key, None)
    if pending is None:
        return False

    if pending.timer is not None:
        pending.timer.cancel()

    debounce_info: dict[str, Any] = {
        "mode": "post_end_start_confirm_window",
        "status": reason,
        "key": notify_key,
        "window_ms": pending.window_ms,
    }
    if end_bucket is not None:
        debounce_info["cancelled_by_event_types"] = sorted(end_bucket.events.keys())
        debounce_info["cancelled_by_request_ids"] = list(end_bucket.request_ids)
    suppress_aggregate_bucket(cfg, pending.bucket, debounce_info=debounce_info)
    return True



def is_true_bililive_end_bucket(bucket: AggregateBucket) -> bool:
    if bucket.group_name != "bililive_end":
        return False

    event_types = set(bucket.events.keys())
    if "StreamEnded" in event_types:
        return True

    metrics = build_end_bucket_metrics(bucket)
    # Recording split events now happen every few hours while Streaming=true.  They may
    # contain large/meaningful stats, but they are not user-visible 下播.  Treat only
    # explicit StreamEnded or a non-streaming end candidate as the real lifecycle end.
    return metrics.get("streaming") is not True



def is_recording_segment_end_bucket(bucket: AggregateBucket) -> bool:
    if bucket.group_name != "bililive_end":
        return False
    if "StreamEnded" in bucket.events:
        return False
    if not is_end_candidate_bucket(bucket):
        return False
    metrics = build_end_bucket_metrics(bucket)
    return metrics.get("streaming") is True



def is_meaningful_streaming_end_candidate(bucket: AggregateBucket) -> bool:
    if not is_recording_segment_end_bucket(bucket):
        return False

    metrics = build_end_bucket_metrics(bucket)
    duration_seconds = safe_float(metrics.get("duration_seconds"))
    file_size_bytes = safe_int(metrics.get("file_size_bytes"))
    interaction_count = safe_int(metrics.get("interaction_count_value"))
    bullet_count = safe_int(metrics.get("bullet_count_value"))
    sc_total = safe_float(metrics.get("sc_total_value"))
    total_revenue = safe_float(metrics.get("total_revenue_value"))

    return any(
        [
            duration_seconds is not None and duration_seconds >= 60.0,
            file_size_bytes is not None and file_size_bytes >= 64 * 1024 * 1024,
            interaction_count is not None and interaction_count >= 100,
            bullet_count is not None and bullet_count >= 100,
            sc_total is not None and sc_total > 0.0,
            total_revenue is not None and total_revenue > 1.0,
        ]
    )



def suppress_aggregate_bucket(cfg: Config, bucket: AggregateBucket, debounce_info: dict[str, Any] | None = None) -> None:
    message, aggregate_meta = build_aggregate_message(bucket)
    message_record = build_aggregate_message_record(bucket, aggregate_meta)
    if message:
        message_record["forward_text"] = get_rendered_message_preview(message) or "(rich media message)"
        message_record["message_mode"] = message.get("mode")
        message_record["message_summary"] = summarize_message_for_log(message)
    message_record["outcome"] = "suppressed"
    if debounce_info:
        message_record["debounce"] = debounce_info
    append_message_log(cfg, message_record)
    eprint(
        json.dumps(
            {
                "event": "aggregate_suppressed",
                "group_key": bucket.key,
                "group_name": bucket.group_name,
                "phase": bucket.phase,
                "event_types": aggregate_meta.get("event_types"),
                "request_ids": bucket.request_ids,
                "debounce": debounce_info,
            },
            ensure_ascii=False,
        )
    )



def is_recent_tail_candidate_bucket(bucket: AggregateBucket) -> bool:
    if "StreamEnded" in bucket.events:
        return False

    metrics = build_end_bucket_metrics(bucket)
    duration_seconds = safe_float(metrics.get("duration_seconds"))
    file_size_bytes = safe_int(metrics.get("file_size_bytes"))
    interaction_count = safe_int(metrics.get("interaction_count_value"))
    bullet_count = safe_int(metrics.get("bullet_count_value"))
    sc_total = safe_float(metrics.get("sc_total_value"))
    total_revenue = safe_float(metrics.get("total_revenue_value"))

    checks: list[bool] = []
    if duration_seconds is not None:
        checks.append(duration_seconds <= 30.0)
    if file_size_bytes is not None:
        checks.append(file_size_bytes <= 16 * 1024 * 1024)
    if interaction_count is not None:
        checks.append(interaction_count <= 100)
    if bullet_count is not None:
        checks.append(bullet_count <= 100)
    if sc_total is not None:
        checks.append(sc_total <= 0.0)
    if total_revenue is not None:
        checks.append(total_revenue <= 1.0)

    return bool(checks) and all(checks)



def should_suppress_recent_forwarded_end_candidate(
    recent_score: tuple[int, int, int, int, int, int, int], candidate_bucket: AggregateBucket
) -> bool:
    candidate_score = build_end_bucket_score(candidate_bucket)
    if candidate_score >= recent_score:
        return False
    return is_recent_tail_candidate_bucket(candidate_bucket)


def should_suppress_start_after_recent_forwarded_end(recent_end: RecentForwardedEnd, candidate_bucket: AggregateBucket) -> bool:
    # User-facing policy: once a true 下播 notification has been forwarded, any
    # start-shaped StreamStarted for the same room/name/title while that recent-end
    # lifecycle is still remembered is recorder tail jitter, not a real user-visible
    # 开播.  The recent-end memory currently lasts at least 5 minutes.
    _ = recent_end
    return is_true_bililive_start_bucket(candidate_bucket)



def deliver_aggregate_bucket(cfg: Config, bucket: AggregateBucket, debounce_info: dict[str, Any] | None = None) -> None:
    if bucket.group_name == "bililive_end" and is_true_bililive_end_bucket(bucket):
        prelim_meta = {"group_name": bucket.group_name, "context": build_aggregate_context(bucket)}
        apply_live_session_segments_to_bucket(build_bililive_notification_key(prelim_meta), bucket)

    message, aggregate_meta = build_aggregate_message(bucket)
    message_record = build_aggregate_message_record(bucket, aggregate_meta)
    targets = resolve_target_specs(cfg, aggregate_meta.get("targets_spec"), context=aggregate_meta.get("context") if isinstance(aggregate_meta.get("context"), dict) else None)
    message_record["target"] = targets

    if not message:
        message_record["outcome"] = "suppressed"
        if debounce_info:
            message_record["debounce"] = debounce_info
        append_message_log(cfg, message_record)
        eprint(json.dumps({"event": "aggregate_suppressed", "request_ids": bucket.request_ids, "group_key": bucket.key}, ensure_ascii=False))
        return

    preview_text = get_rendered_message_preview(message) or "(rich media message)"
    message_record["forward_text"] = preview_text
    message_record["message_mode"] = message.get("mode")
    message_record["message_summary"] = summarize_message_for_log(message)
    if debounce_info:
        message_record["debounce"] = debounce_info

    try:
        send_info = send_rendered_message_to_napcat(cfg, message, targets=targets)
        results = send_info.get("results") or []
        chunks = send_info.get("chunks") or []
        resolved_targets = send_info.get("resolved_targets") or []
        message_record["outcome"] = "forwarded"
        message_record["chunks"] = chunks
        message_record["chunks_count"] = len(chunks)
        message_record["target_count"] = len(resolved_targets)
        message_record["delivery_count"] = len(results)
        message_record["target"] = resolved_targets
        message_record["napcat"] = results
        message_record["used_fallback"] = bool(send_info.get("used_fallback"))
        if send_info.get("fallback_reason"):
            message_record["fallback_reason"] = send_info.get("fallback_reason")
        append_message_log(cfg, message_record)
        notify_key = build_bililive_notification_key(aggregate_meta)
        if bucket.group_name == "bililive_start":
            remember_recent_forwarded_start(cfg, notify_key, bucket)
        elif bucket.group_name == "bililive_end":
            if is_true_bililive_end_bucket(bucket):
                clear_recent_forwarded_start(notify_key)
                clear_live_session_segments(notify_key)
            remember_recent_forwarded_end(cfg, notify_key, bucket)
        eprint(
            json.dumps(
                {
                    "event": "aggregate_forwarded",
                    "group_key": bucket.key,
                    "group_name": bucket.group_name,
                    "phase": bucket.phase,
                    "event_types": aggregate_meta["event_types"],
                    "chunks": len(chunks),
                    "targets": len(resolved_targets),
                    "deliveries": len(results),
                    "message_mode": send_info.get("mode"),
                    "used_fallback": bool(send_info.get("used_fallback")),
                    "request_ids": bucket.request_ids,
                },
                ensure_ascii=False,
            )
        )
    except Exception as exc:  # noqa: BLE001
        message_record["outcome"] = "failed"
        message_record["error"] = str(exc)
        append_message_log(cfg, message_record)
        append_error_log(
            cfg,
            {
                "ts": now_iso(),
                "request_id": message_record["request_id"],
                "related_request_ids": bucket.request_ids,
                "layer": "error",
                "stage": "egress",
                "error_type": "forward_failed",
                "error": str(exc),
                "request": message_record["request"],
                "auth": bucket.auth,
                "target": message_record["target"],
                "aggregate": aggregate_meta,
            },
        )
        eprint(f"[aggregate-forward-error] group_key={bucket.key} {exc}")



def flush_pending_end_notification(cfg: Config, notify_key: str) -> None:
    with PENDING_END_LOCK:
        pending = PENDING_END_NOTIFICATIONS.pop(notify_key, None)
    if pending is None:
        return

    bucket = pending.bucket
    if bucket is None:
        bucket = pending.stream_end_bucket
    elif pending.stream_end_bucket is not None and pending.stream_end_bucket is not bucket:
        merge_aggregate_bucket(bucket, pending.stream_end_bucket)

    if bucket is None:
        return

    debounce_info = {
        "mode": "pending_end_window",
        "status": "expired_forwarded_after_stream_end" if pending.stream_end_bucket is not None else "expired_forwarded",
        "key": notify_key,
        "window_ms": max(0, int(cfg.notify_debounce_ms or 0)),
    }
    if is_recording_segment_end_bucket(bucket):
        debounce_info["status"] = "suppressed_recording_segment_end_while_streaming"
        suppress_aggregate_bucket(cfg, bucket, debounce_info=debounce_info)
        return

    deliver_aggregate_bucket(cfg, bucket, debounce_info=debounce_info)



def handle_aggregate_notification(cfg: Config, bucket: AggregateBucket) -> bool:
    message, aggregate_meta = build_aggregate_message(bucket)
    message_record = build_aggregate_message_record(bucket, aggregate_meta)
    preview_text = get_rendered_message_preview(message) or "(rich media message)"

    if not message:
        message_record["outcome"] = "suppressed"
        append_message_log(cfg, message_record)
        eprint(json.dumps({"event": "aggregate_suppressed", "request_ids": bucket.request_ids, "group_key": bucket.key}, ensure_ascii=False))
        return True

    notify_key = build_bililive_notification_key(aggregate_meta)
    group_name = str(aggregate_meta.get("group_name") or "").strip()
    event_types = set(aggregate_meta.get("event_types") or [])
    window_ms = max(0, int(cfg.notify_debounce_ms or 0))

    if group_name == "bililive_end" and is_recording_segment_end_bucket(bucket):
        remember_live_session_segment(cfg, notify_key, bucket)

    if not notify_key or window_ms <= 0 or group_name not in {"bililive_start", "bililive_end"}:
        deliver_aggregate_bucket(cfg, bucket)
        return True

    now_ts = time.time()
    with PENDING_END_LOCK:
        expired_keys = [key for key, pending in PENDING_END_NOTIFICATIONS.items() if pending.expires_at <= now_ts]
    for expired_key in expired_keys:
        flush_pending_end_notification(cfg, expired_key)

    if group_name == "bililive_start":
        with PENDING_END_LOCK:
            pending = PENDING_END_NOTIFICATIONS.get(notify_key)
        if pending and pending.expires_at > time.time():
            message_record["forward_text"] = preview_text
            message_record["outcome"] = "suppressed"
            message_record["debounce"] = {
                "mode": "pending_end_window",
                "status": "suppressed_start_during_pending_end",
                "key": notify_key,
                "remaining_ms": int((pending.expires_at - time.time()) * 1000),
            }
            append_message_log(cfg, message_record)
            eprint(
                json.dumps(
                    {
                        "event": "aggregate_pending_end_suppressed_start",
                        "group_key": bucket.key,
                        "group_name": bucket.group_name,
                        "request_ids": bucket.request_ids,
                        "debounce": message_record["debounce"],
                    },
                    ensure_ascii=False,
                )
            )
            return True

        recent_start = get_recent_forwarded_start(notify_key)
        if recent_start is not None and should_suppress_recent_forwarded_start_candidate(recent_start.score, bucket):
            candidate_score = build_start_bucket_score(bucket)
            message_record["forward_text"] = preview_text
            message_record["outcome"] = "suppressed"
            message_record["debounce"] = {
                "mode": "recent_forwarded_start_window",
                "status": "suppressed_after_recent_forwarded_start",
                "key": notify_key,
                "candidate_score": list(candidate_score),
                "kept_score": list(recent_start.score),
                "remaining_ms": int((recent_start.expires_at - time.time()) * 1000),
            }
            append_message_log(cfg, message_record)
            eprint(
                json.dumps(
                    {
                        "event": "aggregate_recent_start_suppressed",
                        "group_key": bucket.key,
                        "group_name": bucket.group_name,
                        "request_ids": bucket.request_ids,
                        "debounce": message_record["debounce"],
                    },
                    ensure_ascii=False,
                )
            )
            return True

        if not is_true_bililive_start_bucket(bucket):
            message_record["forward_text"] = preview_text
            message_record["outcome"] = "suppressed"
            message_record["debounce"] = {
                "mode": "true_start_filter",
                "status": "suppressed_recording_segment_start_without_streamstarted",
                "key": notify_key,
                "event_types": sorted(event_types),
            }
            append_message_log(cfg, message_record)
            eprint(
                json.dumps(
                    {
                        "event": "aggregate_recording_segment_start_suppressed",
                        "group_key": bucket.key,
                        "group_name": bucket.group_name,
                        "request_ids": bucket.request_ids,
                        "debounce": message_record["debounce"],
                    },
                    ensure_ascii=False,
                )
            )
            return True

        recent_end = get_recent_forwarded_end(notify_key)
        if recent_end is not None and should_suppress_start_after_recent_forwarded_end(recent_end, bucket):
            candidate_score = build_start_bucket_score(bucket)
            message_record["forward_text"] = preview_text
            message_record["outcome"] = "suppressed"
            message_record["debounce"] = {
                "mode": "recent_forwarded_end_window",
                "status": "suppressed_start_within_5m_after_recent_end",
                "key": notify_key,
                "recent_window_ms": get_recent_end_suppress_window_ms(cfg),
                "candidate_score": list(candidate_score),
                "kept_end_score": list(recent_end.score),
                "remaining_ms": int((recent_end.expires_at - time.time()) * 1000),
            }
            append_message_log(cfg, message_record)
            eprint(
                json.dumps(
                    {
                        "event": "aggregate_recent_end_suppressed_start",
                        "group_key": bucket.key,
                        "group_name": bucket.group_name,
                        "request_ids": bucket.request_ids,
                        "debounce": message_record["debounce"],
                    },
                    ensure_ascii=False,
                )
            )
            return True

        clear_recent_forwarded_end(notify_key)
        deliver_aggregate_bucket(
            cfg,
            bucket,
            debounce_info={
                "mode": "pending_end_window",
                "status": "sent_immediately",
                "key": notify_key,
                "window_ms": window_ms,
            },
        )
        return True

    if is_true_bililive_end_bucket(bucket):
        cancelled_pending_start = cancel_pending_start_after_end(
            cfg,
            notify_key,
            reason="cancelled_by_followup_true_end",
            end_bucket=bucket,
        )
        if cancelled_pending_start:
            message_record["forward_text"] = preview_text
            message_record["outcome"] = "suppressed"
            message_record["debounce"] = {
                "mode": "post_end_start_confirm_window",
                "status": "suppressed_followup_true_end_cancelled_pending_reconnect_start",
                "key": notify_key,
                "event_types": sorted(event_types),
            }
            append_message_log(cfg, message_record)
            eprint(
                json.dumps(
                    {
                        "event": "aggregate_followup_end_after_post_end_start_suppressed",
                        "group_key": bucket.key,
                        "group_name": bucket.group_name,
                        "request_ids": bucket.request_ids,
                        "debounce": message_record["debounce"],
                    },
                    ensure_ascii=False,
                )
            )
            return True

    with PENDING_END_LOCK:
        existing_pending = PENDING_END_NOTIFICATIONS.get(notify_key)
        if existing_pending is not None and existing_pending.expires_at <= time.time():
            existing_pending = None

    if existing_pending is None and is_end_candidate_bucket(bucket):
        recent = get_recent_forwarded_end(notify_key)
        if recent is not None and should_suppress_recent_forwarded_end_candidate(recent.score, bucket):
            candidate_score = build_end_bucket_score(bucket)
            message_record["forward_text"] = preview_text
            message_record["outcome"] = "suppressed"
            message_record["debounce"] = {
                "mode": "pending_end_window",
                "status": "suppressed_after_recent_forwarded_end",
                "key": notify_key,
                "recent_window_ms": get_recent_end_suppress_window_ms(cfg),
                "candidate_score": list(candidate_score),
                "kept_score": list(recent.score),
                "remaining_ms": int((recent.expires_at - time.time()) * 1000),
            }
            append_message_log(cfg, message_record)
            eprint(
                json.dumps(
                    {
                        "event": "aggregate_recent_end_tail_suppressed",
                        "group_key": bucket.key,
                        "group_name": bucket.group_name,
                        "request_ids": bucket.request_ids,
                        "debounce": message_record["debounce"],
                    },
                    ensure_ascii=False,
                )
            )
            return True

    created_pending = False
    with PENDING_END_LOCK:
        pending = PENDING_END_NOTIFICATIONS.get(notify_key)
        if pending is None or pending.expires_at <= time.time():
            timer = threading.Timer(window_ms / 1000, flush_pending_end_notification, args=(cfg, notify_key))
            timer.daemon = True
            pending = PendingEndNotification(
                key=notify_key,
                bucket=None,
                expires_at=time.time() + window_ms / 1000,
                timer=timer,
                window_ms=window_ms,
            )
            PENDING_END_NOTIFICATIONS[notify_key] = pending
            timer.start()
            created_pending = True

    if "StreamEnded" in event_types:
        with PENDING_END_LOCK:
            if pending.stream_end_bucket is None:
                pending.stream_end_bucket = bucket
            elif pending.stream_end_bucket is not bucket:
                merge_aggregate_bucket(pending.stream_end_bucket, bucket)
            if pending.bucket is not None and pending.stream_end_bucket is not pending.bucket:
                merge_aggregate_bucket(pending.bucket, pending.stream_end_bucket)

    if is_end_candidate_bucket(bucket):
        candidate_bucket = bucket
        if pending.stream_end_bucket is not None and pending.stream_end_bucket is not candidate_bucket:
            merge_aggregate_bucket(candidate_bucket, pending.stream_end_bucket)

        previous_bucket = pending.bucket
        previous_score = build_end_bucket_score(previous_bucket) if previous_bucket is not None else None
        candidate_score = build_end_bucket_score(candidate_bucket)

        if previous_bucket is not None and pending.stream_end_bucket is not None and is_trivial_trailing_end_bucket(candidate_bucket) and candidate_score <= previous_score:
            message_record["forward_text"] = preview_text
            message_record["outcome"] = "suppressed"
            message_record["debounce"] = {
                "mode": "pending_end_window",
                "status": "suppressed_trivial_trailing_end",
                "key": notify_key,
                "remaining_ms": int((pending.expires_at - time.time()) * 1000),
                "candidate_score": list(candidate_score),
                "kept_score": list(previous_score),
            }
            append_message_log(cfg, message_record)
            eprint(
                json.dumps(
                    {
                        "event": "aggregate_pending_end_trivial_tail_suppressed",
                        "group_key": bucket.key,
                        "group_name": bucket.group_name,
                        "request_ids": bucket.request_ids,
                        "debounce": message_record["debounce"],
                    },
                    ensure_ascii=False,
                )
            )
            return True

        if previous_bucket is None or candidate_score > previous_score:
            pending.bucket = candidate_bucket
            status = "held_pending_end_candidate" if previous_bucket is None else "replaced_pending_end_candidate"
        else:
            message_record["forward_text"] = preview_text
            message_record["outcome"] = "suppressed"
            message_record["debounce"] = {
                "mode": "pending_end_window",
                "status": "suppressed_weaker_pending_end_candidate",
                "key": notify_key,
                "remaining_ms": int((pending.expires_at - time.time()) * 1000),
                "candidate_score": list(candidate_score),
                "kept_score": list(previous_score),
            }
            append_message_log(cfg, message_record)
            eprint(
                json.dumps(
                    {
                        "event": "aggregate_pending_end_weaker_candidate_suppressed",
                        "group_key": bucket.key,
                        "group_name": bucket.group_name,
                        "request_ids": bucket.request_ids,
                        "debounce": message_record["debounce"],
                    },
                    ensure_ascii=False,
                )
            )
            return True
    elif "StreamEnded" in event_types:
        status = "held_stream_end_waiting_for_stats" if pending.bucket is None else "merged_stream_end_into_pending"
    else:
        status = "held_pending_end"

    message_record["forward_text"] = preview_text
    message_record["outcome"] = "held"
    message_record["debounce"] = {
        "mode": "pending_end_window",
        "status": status,
        "key": notify_key,
        "window_ms": window_ms,
        "remaining_ms": int((pending.expires_at - time.time()) * 1000),
    }
    if created_pending:
        message_record["debounce"]["created_pending"] = True
    append_message_log(cfg, message_record)
    eprint(
        json.dumps(
            {
                "event": "aggregate_pending_end_held",
                "group_key": bucket.key,
                "group_name": bucket.group_name,
                "request_ids": bucket.request_ids,
                "debounce": message_record["debounce"],
            },
            ensure_ascii=False,
        )
    )
    return True



def should_replace_aggregate_bucket_event(bucket: AggregateBucket, event_type: str, existing_payload: dict[str, Any], new_payload: dict[str, Any]) -> bool:
    if bucket.group_name == "bililive_end" and event_type == "FileClosed":
        existing_duration = safe_float(get_field_value(existing_payload, "EventData.Duration")) or 0.0
        new_duration = safe_float(get_field_value(new_payload, "EventData.Duration")) or 0.0
        existing_size = safe_int(get_field_value(existing_payload, "EventData.FileSize")) or -1
        new_size = safe_int(get_field_value(new_payload, "EventData.FileSize")) or -1
        existing_score = (int(round(existing_duration)), existing_size)
        new_score = (int(round(new_duration)), new_size)
        return new_score >= existing_score
    return True



def flush_aggregate_bucket(cfg: Config, key: str) -> None:
    with AGGREGATE_LOCK:
        bucket = AGGREGATE_BUCKETS.pop(key, None)
    if bucket is None:
        return

    handle_aggregate_notification(cfg, bucket)

def queue_aggregate_event(cfg: Config, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult, payload: dict[str, Any], auth: dict[str, Any], request_record: dict[str, Any], request_id: str, remote_ip: str) -> dict[str, Any] | None:
    group, window_ms = get_aggregate_group(cfg, handler, payload)
    if group is None:
        return None

    phase = compute_aggregate_phase(group, payload)
    group_name = str(group.get("name") or "aggregate_group")
    key = build_aggregate_key(group, payload, phase)
    should_start_timer = False
    should_flush_now = False

    with AGGREGATE_LOCK:
        bucket = AGGREGATE_BUCKETS.get(key)
        if bucket is None:
            bucket = AggregateBucket(
                key=key,
                phase=phase,
                group_name=group_name,
                group_config=group,
                created_at=time.time(),
                request_path=parsed.path,
                remote_ip=remote_ip,
                auth=auth,
                target={"private": cfg.private, "group": cfg.group},
            )
            AGGREGATE_BUCKETS[key] = bucket
            should_start_timer = True

        if request_id not in bucket.request_ids:
            bucket.request_ids.append(request_id)
        event_type = str(payload.get("EventType") or request_id)
        existing_event = bucket.events.get(event_type)
        should_replace_event = True
        if isinstance(existing_event, dict):
            existing_payload = existing_event.get("payload")
            if isinstance(existing_payload, dict):
                should_replace_event = should_replace_aggregate_bucket_event(bucket, event_type, existing_payload, payload)
        if should_replace_event:
            bucket.events[event_type] = {
                "request_id": request_id,
                "payload": payload,
                "ts": request_record["ts"],
            }
        bucket.payload_summaries.append(request_record["request"]["payload_summary"])
        bucket.request_path = parsed.path
        bucket.remote_ip = remote_ip
        bucket.auth = auth
        bucket.target = {"private": cfg.private, "group": cfg.group}

        if should_start_timer:
            timer = threading.Timer(window_ms / 1000, flush_aggregate_bucket, args=(cfg, key))
            timer.daemon = True
            bucket.timer = timer
            timer.start()

        should_flush_now = should_flush_aggregate_immediately(bucket, event_type)

    if should_flush_now:
        flush_aggregate_bucket(cfg, key)

    return {
        "queued": True,
        "phase": phase,
        "group_name": group_name,
        "group_key": key,
        "window_ms": window_ms,
        "event_type": str(payload.get("EventType") or "unknown"),
        "flush_now": should_flush_now,
    }



class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "WebhookToNapCat/1.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        eprint("[http]", self.address_string(), "-", fmt % args)

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    def _send_json(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", self.cfg.path.rstrip("/") + "/health", "/health"}:
            self._send_json(200, {"ok": True, "status": "healthy"})
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        request_id = uuid4().hex
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        payload = parse_body(self.headers.get("Content-Type", ""), raw)
        auth = evaluate_secret(self.cfg, self)

        if parsed.path.rstrip("/") != self.cfg.path.rstrip("/"):
            request_record = build_request_record(
                self,
                self.cfg,
                request_id,
                parsed,
                payload,
                auth,
                outcome="path_not_matched",
                note="received POST on unmatched path",
            )
            append_request_log(self.cfg, request_record)
            append_error_log(
                self.cfg,
                {
                    "ts": now_iso(),
                    "request_id": request_id,
                    "layer": "error",
                    "stage": "routing",
                    "error_type": "path_not_matched",
                    "error": "path not matched",
                    "request": request_record["request"],
                    "auth": auth,
                },
            )
            self._send_json(404, {"ok": False, "error": "path not matched", "request_id": request_id})
            return

        if auth["status"] == "failed":
            request_record = build_request_record(
                self,
                self.cfg,
                request_id,
                parsed,
                payload,
                auth,
                outcome="rejected",
                note="webhook secret validation failed",
            )
            append_request_log(self.cfg, request_record)
            append_error_log(
                self.cfg,
                {
                    "ts": now_iso(),
                    "request_id": request_id,
                    "layer": "error",
                    "stage": "auth",
                    "error_type": "invalid_secret",
                    "error": "invalid secret",
                    "request": request_record["request"],
                    "auth": auth,
                },
            )
            eprint(json.dumps({"event": "webhook_rejected", "reason": "invalid_secret", "request_id": request_id, "remote_ip": self.client_address[0]}, ensure_ascii=False))
            self._send_json(401, {"ok": False, "error": "invalid secret", "request_id": request_id})
            return

        payload = persist_incoming_base64_assets(self.cfg, payload, request_id)

        request_record = build_request_record(
            self,
            self.cfg,
            request_id,
            parsed,
            payload,
            auth,
            outcome="accepted",
            note="webhook accepted for forwarding",
        )
        append_request_log(self.cfg, request_record)

        if is_aggregate_candidate(payload):
            aggregate_meta = queue_aggregate_event(self.cfg, self, parsed, payload, auth, request_record, request_id, self.client_address[0])
            if aggregate_meta is not None:
                self._send_json(200, {"ok": True, "queued": True, "aggregate": aggregate_meta, "request_id": request_id})
                return

        message, targets = build_forward_message(self.cfg, self, payload)
        preview_text = get_rendered_message_preview(message) or "(rich media message)"
        message_record: dict[str, Any] = {
            "ts": now_iso(),
            "request_id": request_id,
            "layer": "message",
            "stage": "egress",
            "outcome": "forwarding",
            "request": {
                "path": parsed.path,
                "remote_ip": self.client_address[0],
            },
            "auth": auth,
            "payload_summary": request_record["request"]["payload_summary"],
            "forward_text": preview_text,
            "message_mode": message.get("mode"),
            "message_summary": summarize_message_for_log(message),
            "target": targets,
        }

        try:
            send_info = send_rendered_message_to_napcat(self.cfg, message, targets=targets)
            results = send_info.get("results") or []
            chunks = send_info.get("chunks") or []
            resolved_targets = send_info.get("resolved_targets") or []
            message_record["outcome"] = "forwarded"
            message_record["chunks"] = chunks
            message_record["chunks_count"] = len(chunks)
            message_record["target_count"] = len(resolved_targets)
            message_record["delivery_count"] = len(results)
            message_record["target"] = resolved_targets
            message_record["napcat"] = results
            message_record["used_fallback"] = bool(send_info.get("used_fallback"))
            if send_info.get("fallback_reason"):
                message_record["fallback_reason"] = send_info.get("fallback_reason")
            append_message_log(self.cfg, message_record)
            eprint(
                json.dumps(
                    {
                        "event": "message_forwarded",
                        "path": parsed.path,
                        "chunks": len(chunks),
                        "targets": len(resolved_targets),
                        "deliveries": len(results),
                        "message_mode": send_info.get("mode"),
                        "used_fallback": bool(send_info.get("used_fallback")),
                        "remote_ip": self.client_address[0],
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                )
            )
            self._send_json(200, {"ok": True, "chunks": len(chunks), "targets": len(resolved_targets), "deliveries": len(results), "napcat": results, "request_id": request_id})
        except Exception as exc:  # noqa: BLE001
            message_record["outcome"] = "failed"
            message_record["error"] = str(exc)
            append_message_log(self.cfg, message_record)
            append_error_log(
                self.cfg,
                {
                    "ts": now_iso(),
                    "request_id": request_id,
                    "layer": "error",
                    "stage": "egress",
                    "error_type": "forward_failed",
                    "error": str(exc),
                    "request": request_record["request"],
                    "auth": auth,
                    "target": message_record["target"],
                },
            )
            eprint(f"[forward-error] request_id={request_id} {exc}")
            self._send_json(502, {"ok": False, "error": str(exc), "request_id": request_id})



def parse_args() -> Config:
    ap = argparse.ArgumentParser(description="Listen for webhook messages and forward them to QQ via NapCat")
    ap.add_argument("--listen-host", default=os.getenv("LISTEN_HOST", "0.0.0.0"))
    ap.add_argument("--listen-port", type=int, default=int(os.getenv("LISTEN_PORT", "8787")))
    ap.add_argument("--path", default=os.getenv("WEBHOOK_PATH", "/webhook"))
    ap.add_argument("--secret", default=os.getenv("WEBHOOK_SECRET", ""))

    ap.add_argument("--napcat-base-url", default=os.getenv("NAPCAT_BASE_URL", "http://127.0.0.1:3001"))
    ap.add_argument("--napcat-token", default=os.getenv("NAPCAT_TOKEN", ""))
    ap.add_argument("--napcat-token-mode", choices=["header", "query"], default=os.getenv("NAPCAT_TOKEN_MODE", "header"))

    private_env = os.getenv("NAPCAT_PRIVATE_QQ")
    group_env = os.getenv("NAPCAT_GROUP_QQ")
    ap.add_argument("--private", type=int, default=None, help="QQ 私聊 user_id；也可通过环境变量 NAPCAT_PRIVATE_QQ 提供")
    ap.add_argument("--group", type=int, default=None, help="QQ 群 group_id；也可通过环境变量 NAPCAT_GROUP_QQ 提供")

    ap.add_argument("--timeout", type=float, default=float(os.getenv("NAPCAT_TIMEOUT", "10")))
    ap.add_argument("--retries", type=int, default=int(os.getenv("NAPCAT_RETRIES", "5")))
    ap.add_argument("--chunk-size", type=int, default=int(os.getenv("QQ_CHUNK_SIZE", "280")))
    ap.add_argument("--title-prefix", default=os.getenv("TITLE_PREFIX", "📨"))
    ap.add_argument("--include-headers", action="store_true", default=os.getenv("INCLUDE_HEADERS", "1") not in {"0", "false", "False"})
    ap.add_argument("--rules-path", default=os.getenv("WEBHOOK_RULES_PATH", "/app/rules.json"), help="规则文件路径（JSON）；用于按配置决定不同 webhook 的转发内容")
    ap.add_argument("--log-dir", default=os.getenv("WEBHOOK_LOG_DIR", "/logs"), help="消息日志目录；按 requests/messages/errors 三层写入 JSONL")
    ap.add_argument("--media-dir", default=os.getenv("WEBHOOK_MEDIA_DIR", "/app/media"), help="收到的 base64 媒体文件落盘目录（服务容器/进程可写路径）")
    ap.add_argument("--public-media-dir", default=os.getenv("WEBHOOK_PUBLIC_MEDIA_DIR", "/opt/WebhookToNapcat/media"), help="写入 payload/log 并传给 NapCat 的媒体文件路径前缀；通常是宿主机可见路径")
    ap.add_argument("--aggregate-window-ms", type=int, default=int(os.getenv("WEBHOOK_AGGREGATE_WINDOW_MS", "3000")), help="BililiveRecorder 事件聚合窗口（毫秒）；开始/结束类事件会在窗口内合并后统一发送")
    ap.add_argument("--notify-file-opening", action="store_true", default=os.getenv("WEBHOOK_NOTIFY_FILE_OPENING", "0") in {"1", "true", "True"}, help="是否单独发送 FileOpening 类事件；默认关闭，仅参与聚合")
    ap.add_argument("--notify-debounce-ms", type=int, default=int(os.getenv("WEBHOOK_NOTIFY_DEBOUNCE_MS", "15000")), help="聚合消息结束阶段等待窗口（毫秒）；窗口内会缓存 StreamEnded 与候选结束统计，优先保留信息更完整的那条最终下发，并抑制尾声抖动 start / 空尾巴")
    args = ap.parse_args()

    private_target = args.private if args.private is not None else (int(private_env) if private_env else None)
    group_target = args.group if args.group is not None else (int(group_env) if group_env else None)

    # 允许同时配置默认私聊 + 默认群聊；若两者都不填，则需要依赖 rules.json 中的 per-rule / per-output targets。

    path = args.path.strip() or "/webhook"
    if not path.startswith("/"):
        path = "/" + path

    return Config(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        path=path,
        secret=args.secret,
        napcat_base_url=args.napcat_base_url,
        napcat_token=args.napcat_token,
        napcat_token_mode=args.napcat_token_mode,
        private=private_target,
        group=group_target,
        timeout=args.timeout,
        retries=args.retries,
        chunk_size=max(50, args.chunk_size),
        title_prefix=args.title_prefix,
        include_headers=args.include_headers,
        rules_path=args.rules_path,
        log_dir=args.log_dir,
        aggregate_window_ms=max(0, args.aggregate_window_ms),
        notify_file_opening=args.notify_file_opening,
        notify_debounce_ms=max(0, args.notify_debounce_ms),
        media_dir=args.media_dir,
        public_media_dir=args.public_media_dir,
    )



def main() -> int:
    cfg = parse_args()
    server = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), WebhookHandler)
    server.cfg = cfg  # type: ignore[attr-defined]

    print(json.dumps({"status": "listening"}, ensure_ascii=False))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
