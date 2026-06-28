from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Union


BililiveTargetSpec = Union[str, dict[str, int]]


@dataclass(frozen=True)
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
    log_dir: str
    media_dir: str
    public_media_dir: str
    outbound_text_max_chars: int
    aggregate_window_ms: int
    notify_debounce_ms: int
    live_session_segment_ttl_ms: int
    post_end_start_confirm_ms: int
    internal_dedupe_ttl_seconds: int
    bililive_xml_base_dir: str
    bililive_xml_strip_prefixes: tuple[str, ...]
    bililive_gift_price_table: str
    bililive_cover_index_path: str
    bililive_targets: dict[str, tuple[BililiveTargetSpec, ...]]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_target(name: str) -> int | None:
    raw = os.getenv(name)
    return int(raw) if raw else None


def _csv_tuple(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _bililive_target_spec(item: Any) -> BililiveTargetSpec:
    if item == "default":
        return "default"
    if not isinstance(item, dict):
        raise ValueError("BILILIVE_TARGETS_JSON entries must be 'default' or target objects")
    if set(item) == {"group"}:
        return {"group": int(item["group"])}
    if set(item) == {"private"}:
        return {"private": int(item["private"])}
    raise ValueError("BILILIVE_TARGETS_JSON target objects must contain exactly group or private")


def parse_bililive_targets_json(raw: str) -> dict[str, tuple[BililiveTargetSpec, ...]]:
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("BILILIVE_TARGETS_JSON must be a JSON object")

    result: dict[str, tuple[BililiveTargetSpec, ...]] = {}
    for room_id, specs in data.items():
        room_key = str(room_id).strip()
        if not room_key:
            raise ValueError("BILILIVE_TARGETS_JSON room id cannot be empty")
        if not isinstance(specs, list):
            raise ValueError("BILILIVE_TARGETS_JSON room targets must be arrays")
        result[room_key] = tuple(_bililive_target_spec(item) for item in specs)
    return result


def normalize_path(path: str) -> str:
    path = path.strip() or "/webhook"
    return path if path.startswith("/") else f"/{path}"


def parse_args(argv: list[str] | None = None) -> Config:
    ap = argparse.ArgumentParser(description="Receive webhook HTTP requests and forward them to QQ through NapCat.")
    ap.add_argument("--listen-host", default=os.getenv("LISTEN_HOST", "0.0.0.0"))
    ap.add_argument("--listen-port", type=int, default=_env_int("LISTEN_PORT", 8787))
    ap.add_argument("--path", default=os.getenv("WEBHOOK_PATH", "/webhook"))
    ap.add_argument("--secret", default=os.getenv("WEBHOOK_SECRET", ""))
    ap.add_argument("--napcat-base-url", default=os.getenv("NAPCAT_BASE_URL", "http://127.0.0.1:3001"))
    ap.add_argument("--napcat-token", default=os.getenv("NAPCAT_TOKEN", ""))
    ap.add_argument("--napcat-token-mode", choices=["header", "query"], default=os.getenv("NAPCAT_TOKEN_MODE", "header"))
    ap.add_argument("--private", type=int, default=None)
    ap.add_argument("--group", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=_env_float("NAPCAT_TIMEOUT", 10.0))
    ap.add_argument("--retries", type=int, default=_env_int("NAPCAT_RETRIES", 5))
    ap.add_argument("--chunk-size", type=int, default=_env_int("QQ_CHUNK_SIZE", 280))
    ap.add_argument("--log-dir", default=os.getenv("WEBHOOK_LOG_DIR", "/logs"))
    ap.add_argument("--media-dir", default=os.getenv("WEBHOOK_MEDIA_DIR", "/app/media"))
    ap.add_argument("--public-media-dir", default=os.getenv("WEBHOOK_PUBLIC_MEDIA_DIR", "/opt/WebhookToNapcat/media"))
    ap.add_argument("--outbound-text-max-chars", type=int, default=_env_int("WEBHOOK_OUTBOUND_TEXT_MAX_CHARS", 5000))
    ap.add_argument("--aggregate-window-ms", type=int, default=_env_int("WEBHOOK_AGGREGATE_WINDOW_MS", 3000))
    ap.add_argument("--notify-debounce-ms", type=int, default=_env_int("WEBHOOK_NOTIFY_DEBOUNCE_MS", 15000))
    ap.add_argument("--live-session-segment-ttl-ms", type=int, default=_env_int("WEBHOOK_LIVE_SESSION_SEGMENT_TTL_MS", 18 * 60 * 60 * 1000))
    ap.add_argument("--post-end-start-confirm-ms", type=int, default=_env_int("WEBHOOK_POST_END_START_CONFIRM_MS", 45000))
    ap.add_argument("--internal-dedupe-ttl-seconds", type=int, default=_env_int("WEBHOOK_INTERNAL_DEDUPE_TTL_SECONDS", 24 * 60 * 60))
    ap.add_argument("--bililive-xml-base-dir", default=os.getenv("BILILIVE_XML_BASE_DIR", ""))
    ap.add_argument("--bililive-xml-strip-prefixes", default=os.getenv("BILILIVE_XML_STRIP_PREFIXES", ""))
    ap.add_argument("--bililive-gift-price-table", default=os.getenv("BILILIVE_GIFT_PRICE_TABLE", ""))
    ap.add_argument("--bililive-cover-index-path", default=os.getenv("BILILIVE_COVER_INDEX_PATH", ""))
    ap.add_argument("--bililive-targets-json", default=os.getenv("BILILIVE_TARGETS_JSON", ""))
    args = ap.parse_args(argv)

    private = args.private if args.private is not None else _env_target("NAPCAT_PRIVATE_QQ")
    group = args.group if args.group is not None else _env_target("NAPCAT_GROUP_QQ")

    return Config(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        path=normalize_path(args.path),
        secret=args.secret,
        napcat_base_url=args.napcat_base_url,
        napcat_token=args.napcat_token,
        napcat_token_mode=args.napcat_token_mode,
        private=private,
        group=group,
        timeout=max(0.1, args.timeout),
        retries=max(0, args.retries),
        chunk_size=max(50, args.chunk_size),
        log_dir=args.log_dir,
        media_dir=args.media_dir,
        public_media_dir=args.public_media_dir,
        outbound_text_max_chars=max(0, args.outbound_text_max_chars),
        aggregate_window_ms=max(0, args.aggregate_window_ms),
        notify_debounce_ms=max(0, args.notify_debounce_ms),
        live_session_segment_ttl_ms=max(0, args.live_session_segment_ttl_ms),
        post_end_start_confirm_ms=max(0, args.post_end_start_confirm_ms),
        internal_dedupe_ttl_seconds=max(0, args.internal_dedupe_ttl_seconds),
        bililive_xml_base_dir=args.bililive_xml_base_dir,
        bililive_xml_strip_prefixes=_csv_tuple(args.bililive_xml_strip_prefixes),
        bililive_gift_price_table=args.bililive_gift_price_table,
        bililive_cover_index_path=args.bililive_cover_index_path,
        bililive_targets=parse_bililive_targets_json(args.bililive_targets_json),
    )
