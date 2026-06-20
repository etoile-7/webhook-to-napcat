from __future__ import annotations

import json
from datetime import datetime
from typing import Any


DEFAULT_OUTBOUND_TEXT_MAX_CHARS = 5000


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_int(value: Any) -> int | None:
    try:
        if value in {None, ""}:
            return None
        return int(float(value))
    except Exception:
        return None


def safe_float(value: Any) -> float | None:
    try:
        if value in {None, ""}:
            return None
        return float(value)
    except Exception:
        return None


def safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return None


def get_field_value(payload: Any, field: str) -> Any:
    current = payload
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def compact_json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent)


def truncate_middle(text: str, max_len: int = 96) -> str:
    if len(text) <= max_len:
        return text
    keep = max(8, (max_len - 3) // 2)
    return text[:keep] + "..." + text[-keep:]


def truncate_text(text: str | None, max_chars: int = DEFAULT_OUTBOUND_TEXT_MAX_CHARS) -> str | None:
    if not isinstance(text, str):
        return text
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    suffix = f"\n\n[message truncated: original_chars={len(text)}, limit={max_chars}]"
    if len(suffix) >= max_chars:
        return suffix[:max_chars]
    return text[: max_chars - len(suffix)].rstrip() + suffix


def split_text_for_qq(text: str, max_len: int, *, outbound_limit: int = DEFAULT_OUTBOUND_TEXT_MAX_CHARS) -> list[str]:
    text = truncate_text((text or "").strip(), outbound_limit) or ""
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
