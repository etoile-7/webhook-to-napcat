#!/usr/bin/env python3
"""Listen for webhook POSTs and forward them to QQ via NapCat (OneBot v11 HTTP API)."""

from __future__ import annotations

import argparse
import json
import os
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
PRICE_TABLE_CACHE: dict[str, dict[str, float]] = {}


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


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)



def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")



def append_jsonl(cfg: Config, bucket: str, record: dict[str, Any]) -> None:
    if not cfg.log_dir:
        return

    try:
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



def build_napcat_request(cfg: Config) -> tuple[str, dict[str, str], str]:
    base_url = cfg.napcat_base_url.rstrip("/")
    endpoint = "/send_private_msg" if cfg.private is not None else "/send_group_msg"
    headers: dict[str, str] = {}
    token_q = ""

    if cfg.napcat_token:
        if cfg.napcat_token_mode == "header":
            headers["Authorization"] = "Bearer " + cfg.napcat_token
        else:
            token_q = ("&" if "?" in base_url else "?") + "access_token=" + urllib.parse.quote(cfg.napcat_token)

    return base_url + endpoint + token_q, headers, endpoint



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



def send_text_to_napcat(cfg: Config, text: str) -> tuple[list[dict[str, Any]], list[str]]:
    url, headers, _ = build_napcat_request(cfg)
    target_payload = {"user_id": cfg.private} if cfg.private is not None else {"group_id": cfg.group}

    results = []
    chunks = split_for_qq(text, cfg.chunk_size)
    for chunk in chunks:
        payload = dict(target_payload)
        payload["message"] = [{"type": "text", "data": {"text": chunk}}]
        results.append(post_json(url, payload, headers=headers, timeout=cfg.timeout, retries=cfg.retries))
    return results, chunks



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



def load_rules(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
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



def render_rule_output(rule: dict[str, Any], payload: Any) -> str | None:
    output = rule.get("output")
    if not isinstance(output, dict):
        return None

    kind = output.get("type", "summary")
    if kind == "field":
        field = str(output.get("field", "")).strip()
        value = get_field_value(payload, field) if field else None
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None:
            return str(value)
        return None

    if kind == "template":
        template = str(output.get("template", "")).strip()
        if not template:
            return None
        if isinstance(payload, dict):
            flat = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in payload.items()}
            try:
                return template.format(**flat).strip()
            except Exception:  # noqa: BLE001
                return template
        return template

    if kind == "summary":
        return summarize_payload(payload).strip()

    return None



def apply_rules(cfg: Config, handler: BaseHTTPRequestHandler, payload: Any) -> str | None:
    rules_doc = load_rules(cfg.rules_path)
    rules = rules_doc.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if rule_matches(rule, handler, payload):
                rendered = render_rule_output(rule, payload)
                if rendered:
                    return rendered

    default_rule = rules_doc.get("default")
    if isinstance(default_rule, dict):
        rendered = render_rule_output({"output": default_rule}, payload)
        if rendered:
            return rendered

    return None



def build_forward_text(cfg: Config, handler: BaseHTTPRequestHandler, payload: Any) -> str:
    rules_text = apply_rules(cfg, handler, payload)
    if rules_text:
        return rules_text

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
    return "\n".join(lines).strip()



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
            "payload": payload,
            "payload_summary": get_payload_summary(payload),
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



def build_guard_increment_line(captain: int, commander: int, governor: int) -> str:
    parts: list[str] = []
    if captain > 0:
        parts.append(f"新增舰长：{captain}")
    if commander > 0:
        parts.append(f"提督：{commander}")
    if governor > 0:
        parts.append(f"总督：{governor}")
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
        "bullet_count_display": "",
        "interaction_count": "",
        "interaction_count_display": "",
        "sc_count": "",
        "sc_total": "",
        "guard_count": "",
        "guard_total": "",
        "gift_total": "",
        "total_revenue": "",
        "gift_unknown_count": 0,
        "gift_unknown_summary": "",
        "gift_unknown_line": "",
        "gift_total_label": "礼物营收",
        "total_revenue_label": "总营收",
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
    gift_unknown_summary = "、".join(f"{name}×{count}" for name, count in sorted(gift_unknown.items()) if name)
    total_revenue = gift_total + sc_total + guard_total

    stats.update(
        {
            "xml_exists": True,
            "bullet_count": str(bullet_count),
            "bullet_count_display": format_count_k(bullet_count),
            "interaction_count": str(interaction_count),
            "interaction_count_display": format_count_k(interaction_count),
            "sc_count": str(sc_count),
            "sc_total": format_money(sc_total),
            "guard_count": str(guard_count),
            "captain_count": str(captain_count),
            "commander_count": str(commander_count),
            "governor_count": str(governor_count),
            "guard_increment_line": build_guard_increment_line(captain_count, commander_count, governor_count),
            "guard_total": format_money(guard_total),
            "gift_total": format_money(gift_total),
            "total_revenue": format_money(total_revenue),
            "gift_unknown_count": len(gift_unknown),
            "gift_unknown_summary": gift_unknown_summary,
            "gift_unknown_line": f"\n未知礼物：{gift_unknown_summary}" if gift_unknown_summary else "",
            "gift_total_label": "礼物营收（已知）" if gift_unknown_summary else "礼物营收",
            "total_revenue_label": "总营收（已知）" if gift_unknown_summary else "总营收",
        }
    )
    return stats



def get_xml_live_stats(bucket: AggregateBucket, spec: dict[str, Any]) -> dict[str, Any]:
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
            "bullet_count_display": "",
            "interaction_count_display": "",
            "captain_count": "",
            "commander_count": "",
            "governor_count": "",
            "guard_increment_line": "",
            "gift_unknown_line": "",
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



def truncate_value(value: Any, max_len: int) -> Any:
    if not isinstance(value, str):
        return value
    return truncate_middle(value, max_len=max_len)



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



def resolve_context_value(bucket: AggregateBucket, alias: str, spec: Any) -> Any:
    if isinstance(spec, str):
        value = get_bucket_field_value(bucket, spec)
        return value if value not in {None, ""} else None

    if not isinstance(spec, dict):
        return spec

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
    elif spec.get("source") == "time":
        value = get_bucket_display_time(bucket, str(spec.get("mode") or bucket.phase))
    elif spec.get("source") == "phase":
        value = bucket.phase
    elif spec.get("source") == "event_types":
        value = sorted(bucket.events.keys())
    elif spec.get("source") == "group_name":
        value = bucket.group_name
    elif spec.get("source") == "request_count":
        value = len(bucket.request_ids)
    elif spec.get("source") == "event_count":
        value = len(bucket.events)
    elif spec.get("source") == "request_ids":
        value = list(bucket.request_ids)
    elif spec.get("source") == "xml_live_stats":
        stats = get_xml_live_stats(bucket, spec)
        stats_key = str(spec.get("key") or alias)
        value = stats.get(stats_key)

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
            "duration": format_duration_human(get_bucket_field_value(bucket, "EventData.Duration")),
            "file_size": format_bytes_human(get_bucket_field_value(bucket, "EventData.FileSize")),
            "time": get_bucket_display_time(bucket),
        }
    )

    context_spec = bucket.group_config.get("context")
    if isinstance(context_spec, dict):
        for alias, spec in context_spec.items():
            ctx[str(alias)] = resolve_context_value(bucket, alias=str(alias), spec=spec)

    return ctx



def render_output_spec(output: dict[str, Any], context: dict[str, Any]) -> str | None:
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



def select_aggregate_output(bucket: AggregateBucket, context: dict[str, Any]) -> tuple[str | None, list[str]]:
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
                return rendered, suppress_event_types

    output = group.get("output")
    if isinstance(output, dict):
        rendered = render_output_spec(output, context)
        if rendered:
            return rendered, suppress_event_types

    return None, suppress_event_types



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



def build_aggregate_message(bucket: AggregateBucket) -> tuple[str | None, dict[str, Any]]:
    context = build_aggregate_context(bucket)
    text, suppressed = select_aggregate_output(bucket, context)
    meta: dict[str, Any] = {
        "enabled": True,
        "group_name": bucket.group_name,
        "group_key": bucket.key,
        "phase": bucket.phase,
        "event_types": sorted(bucket.events.keys()),
        "suppressed": suppressed,
        "context": context,
    }
    return text, meta



def flush_aggregate_bucket(cfg: Config, key: str) -> None:
    with AGGREGATE_LOCK:
        bucket = AGGREGATE_BUCKETS.pop(key, None)
    if bucket is None:
        return

    text, aggregate_meta = build_aggregate_message(bucket)
    message_record: dict[str, Any] = {
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

    if not text:
        message_record["outcome"] = "suppressed"
        append_message_log(cfg, message_record)
        eprint(json.dumps({"event": "aggregate_suppressed", "request_ids": bucket.request_ids, "group_key": key}, ensure_ascii=False))
        return

    message_record["forward_text"] = text

    try:
        results, chunks = send_text_to_napcat(cfg, text)
        message_record["outcome"] = "forwarded"
        message_record["chunks"] = chunks
        message_record["chunks_count"] = len(chunks)
        message_record["napcat"] = results
        append_message_log(cfg, message_record)
        eprint(
            json.dumps(
                {
                    "event": "aggregate_forwarded",
                    "group_key": key,
                    "group_name": bucket.group_name,
                    "phase": bucket.phase,
                    "event_types": aggregate_meta["event_types"],
                    "chunks": len(chunks),
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
                "target": bucket.target,
                "aggregate": aggregate_meta,
            },
        )
        eprint(f"[aggregate-forward-error] group_key={key} {exc}")



def queue_aggregate_event(cfg: Config, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult, payload: dict[str, Any], auth: dict[str, Any], request_record: dict[str, Any], request_id: str, remote_ip: str) -> dict[str, Any] | None:
    group, window_ms = get_aggregate_group(cfg, handler, payload)
    if group is None:
        return None

    phase = compute_aggregate_phase(group, payload)
    group_name = str(group.get("name") or "aggregate_group")
    key = build_aggregate_key(group, payload, phase)
    should_start_timer = False

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

    return {
        "queued": True,
        "phase": phase,
        "group_name": group_name,
        "group_key": key,
        "window_ms": window_ms,
        "event_type": str(payload.get("EventType") or "unknown"),
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

        text = build_forward_text(self.cfg, self, payload)
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
            "forward_text": text,
            "target": {"private": self.cfg.private, "group": self.cfg.group},
        }

        try:
            results, chunks = send_text_to_napcat(self.cfg, text)
            message_record["outcome"] = "forwarded"
            message_record["chunks"] = chunks
            message_record["chunks_count"] = len(chunks)
            message_record["napcat"] = results
            append_message_log(self.cfg, message_record)
            eprint(
                json.dumps(
                    {
                        "event": "message_forwarded",
                        "path": parsed.path,
                        "chunks": len(chunks),
                        "remote_ip": self.client_address[0],
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                )
            )
            self._send_json(200, {"ok": True, "chunks": len(results), "napcat": results, "request_id": request_id})
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
    ap.add_argument("--aggregate-window-ms", type=int, default=int(os.getenv("WEBHOOK_AGGREGATE_WINDOW_MS", "3000")), help="BililiveRecorder 事件聚合窗口（毫秒）；开始/结束类事件会在窗口内合并后统一发送")
    ap.add_argument("--notify-file-opening", action="store_true", default=os.getenv("WEBHOOK_NOTIFY_FILE_OPENING", "0") in {"1", "true", "True"}, help="是否单独发送 FileOpening 类事件；默认关闭，仅参与聚合")
    args = ap.parse_args()

    private_target = args.private if args.private is not None else (int(private_env) if private_env else None)
    group_target = args.group if args.group is not None else (int(group_env) if group_env else None)

    if private_target is not None and group_target is not None:
        ap.error("请只设置一个目标：--private / NAPCAT_PRIVATE_QQ 与 --group / NAPCAT_GROUP_QQ 二选一，不要同时填写")
    if private_target is None and group_target is None:
        ap.error("必须设置一个目标：请提供 --private 或 --group，或设置环境变量 NAPCAT_PRIVATE_QQ / NAPCAT_GROUP_QQ（私聊和群聊二选一）")

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
