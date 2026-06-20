from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .media import sanitize_for_log


SENSITIVE_HEADERS = {"authorization", "x-webhook-secret", "proxy-authorization"}


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def append_jsonl(cfg: Config, bucket: str, record: dict[str, Any]) -> None:
    if not cfg.log_dir:
        return
    try:
        log_dir = Path(cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        month = datetime.now().strftime("%Y-%m")
        log_path = log_dir / f"{bucket}-{month}.jsonl"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(sanitize_for_log(record), ensure_ascii=False) + "\n")
    except Exception as exc:
        eprint(f"[{bucket}-log-error] {exc}")


def append_request_log(cfg: Config, record: dict[str, Any]) -> None:
    append_jsonl(cfg, "requests", record)


def append_message_log(cfg: Config, record: dict[str, Any]) -> None:
    append_jsonl(cfg, "messages", record)


def append_error_log(cfg: Config, record: dict[str, Any]) -> None:
    append_jsonl(cfg, "errors", record)


def redact_header(name: str, value: str) -> str:
    if name.lower() in SENSITIVE_HEADERS:
        return "<redacted>"
    return value


def sanitized_headers(headers: Any) -> dict[str, str]:
    return {key: redact_header(key, value) for key, value in headers.items()}
