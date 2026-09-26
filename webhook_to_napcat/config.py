from __future__ import annotations

import argparse
import os
from dataclasses import dataclass


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
    )
