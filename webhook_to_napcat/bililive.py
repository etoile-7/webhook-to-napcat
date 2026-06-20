from __future__ import annotations

import copy
import json
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Config
from .internal import HandlerResult
from .logs import append_error_log, append_message_log, eprint
from .media import sanitize_for_log
from .napcat import DeliveryReport, default_targets, send_text
from .utils import get_field_value, now_iso, safe_bool, safe_float, safe_int, truncate_middle


START_EVENTS = {"StreamStarted", "SessionStarted", "FileOpening"}
END_EVENTS = {"FileClosed", "SessionEnded", "StreamEnded"}
BILILIVE_EVENTS = START_EVENTS | END_EVENTS
BILILIVE_SESSION_STATS_KEY = "__bililive_live_session_merged_stats"

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
PRICE_TABLE_CACHE: dict[str, dict[str, float]] = {}


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
    target: dict[str, Any] = field(default_factory=dict)
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


def is_bililive_notification(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("EventType"), str)
        and payload.get("EventType") in BILILIVE_EVENTS
        and isinstance(payload.get("EventData"), dict)
    )


def event_phase(event_type: str) -> str:
    return "start" if event_type in START_EVENTS else "end"


def default_group_config(phase: str) -> dict[str, Any]:
    if phase == "start":
        return {"event_order": ["StreamStarted", "SessionStarted", "FileOpening"]}
    return {"event_order": ["FileClosed", "SessionEnded", "StreamEnded"]}


def bucket_key(payload: dict[str, Any], phase: str) -> str:
    room_id = get_field_value(payload, "EventData.RoomId")
    name = get_field_value(payload, "EventData.Name")
    return f"bililive:{phase}:{room_id or '_'}:{name or '_'}"


def format_duration_human(value: Any) -> str:
    try:
        total_seconds = int(float(value))
    except Exception:
        return str(value or "")
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
    except Exception:
        return str(value or "")
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return str(value)


def format_money(value: Any) -> str:
    try:
        amount = float(value)
    except Exception:
        return str(value or "")
    if amount.is_integer():
        return str(int(amount))
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def format_count_k(value: Any) -> str:
    try:
        count = float(value)
    except Exception:
        return str(value or "")
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    if count.is_integer():
        return str(int(count))
    return str(count)


def get_file_name(path_value: Any) -> str | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    return Path(path_value).name or path_value


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
        except Exception:
            continue
    PRICE_TABLE_CACHE[path_value] = prices
    return prices


def derive_xml_path(relative_path: str, base_dir: str, strip_prefixes: tuple[str, ...] = ()) -> Path:
    normalized = relative_path.strip().replace("\\", "/")
    for prefix in strip_prefixes:
        prefix_text = str(prefix).strip().replace("\\", "/")
        if prefix_text and normalized.startswith(prefix_text):
            normalized = normalized[len(prefix_text) :].lstrip("/")
            break
    return (Path(base_dir) / normalized).with_suffix(".xml")


def empty_xml_stats(xml_path: str = "") -> dict[str, Any]:
    return {
        "xml_path": xml_path,
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
        "captain_count": "",
        "commander_count": "",
        "governor_count": "",
        "guard_count": "",
        "guard_count_value": None,
        "guard_increment_line": "",
        "guard_total": "",
        "guard_total_value": None,
        "gift_total": "",
        "gift_total_value": None,
        "gift_unknown_summary": "",
        "gift_unknown_line": "",
        "total_revenue": "",
        "total_revenue_value": None,
        "gift_total_label": "礼物营收",
        "total_revenue_label": "总营收",
        "_interaction_user_keys": [],
        "_gift_unknown_counts": {},
    }


def compute_xml_live_stats(xml_path: Path, price_table_path: str = "") -> dict[str, Any]:
    stats = empty_xml_stats(str(xml_path))
    if not xml_path.exists():
        return stats
    try:
        root = ET.parse(xml_path).getroot()
    except Exception as exc:
        eprint(f"[xml-stats-error] {xml_path}: {exc}")
        return stats

    gift_prices = load_markdown_price_table(price_table_path) if price_table_path else {}
    guard_level_map = {"3": "舰长", "2": "提督", "1": "总督"}
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
        user_text = str(user or "").strip()
        if uid_text:
            interaction_users.add(f"uid:{uid_text}")
        elif user_text:
            interaction_users.add(f"user:{user_text}")

    for child in root:
        tag = child.tag.split("}")[-1]
        if tag == "d":
            bullet_count += 1
            p_parts = str(child.attrib.get("p", "")).split(",")
            add_interaction_user(p_parts[6] if len(p_parts) > 6 else "", child.attrib.get("user", ""))
        elif tag == "gift":
            add_interaction_user(child.attrib.get("uid", ""), child.attrib.get("user", ""))
            gift_name = child.attrib.get("giftname", "")
            gift_count = safe_int(child.attrib.get("giftcount")) or 0
            price = gift_prices.get(gift_name)
            if price is None:
                gift_unknown[gift_name] = gift_unknown.get(gift_name, 0) + gift_count
            else:
                gift_total += price * gift_count
        elif tag == "sc":
            add_interaction_user(child.attrib.get("uid", ""), child.attrib.get("user", ""))
            sc_count += 1
            sc_total += safe_float(child.attrib.get("price")) or 0.0
        elif tag == "guard":
            add_interaction_user(child.attrib.get("uid", ""), child.attrib.get("user", ""))
            level = str(child.attrib.get("level", ""))
            count = safe_int(child.attrib.get("count")) or 0
            guard_count += count
            if level == "3":
                captain_count += count
            elif level == "2":
                commander_count += count
            elif level == "1":
                governor_count += count
            guard_total += gift_prices.get(guard_level_map.get(level, ""), 0.0) * count

    interaction_count = len(interaction_users)
    total_revenue = gift_total + sc_total + guard_total
    gift_unknown_summary = "、".join(f"{name}×{count}" for name, count in sorted(gift_unknown.items()) if name and count)
    guard_increment_line = build_guard_increment_line(captain_count, commander_count, governor_count)
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
            "captain_count": str(captain_count),
            "commander_count": str(commander_count),
            "governor_count": str(governor_count),
            "guard_count": str(guard_count),
            "guard_count_value": guard_count,
            "guard_increment_line": guard_increment_line,
            "guard_total": format_money(guard_total),
            "guard_total_value": guard_total,
            "gift_total": format_money(gift_total),
            "gift_total_value": gift_total,
            "gift_unknown_summary": gift_unknown_summary,
            "gift_unknown_line": f"未知礼物：{gift_unknown_summary}" if gift_unknown_summary else "",
            "total_revenue": format_money(total_revenue),
            "total_revenue_value": total_revenue,
            "gift_total_label": "礼物营收（已知）" if gift_unknown_summary else "礼物营收",
            "total_revenue_label": "总营收（已知）" if gift_unknown_summary else "总营收",
            "_interaction_user_keys": sorted(interaction_users),
            "_gift_unknown_counts": dict(sorted(gift_unknown.items())),
        }
    )
    return stats


def get_bucket_field_value(bucket: AggregateBucket, field: str) -> Any:
    event_order = bucket.group_config.get("event_order")
    ordered_types: list[str] = []
    if isinstance(event_order, list):
        ordered_types.extend(str(item) for item in event_order)
    for event_type in bucket.events:
        if event_type not in ordered_types:
            ordered_types.append(event_type)

    chosen = None
    for event_type in ordered_types:
        event = bucket.events.get(event_type)
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            continue
        value = get_field_value(payload, field)
        if value not in {None, ""}:
            chosen = value
    return chosen


def parse_event_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().replace("T", " ").replace("Z", "")


def get_bucket_display_time(bucket: AggregateBucket, mode: str | None = None) -> str | None:
    values: list[str] = []
    event_order = bucket.group_config.get("event_order")
    ordered_types = [str(item) for item in event_order] if isinstance(event_order, list) else list(bucket.events)
    for event_type in ordered_types:
        event = bucket.events.get(event_type)
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            continue
        parsed = parse_event_timestamp(payload.get("EventTimestamp"))
        if parsed:
            values.append(parsed)
    if not values:
        return None
    return values[0] if (mode or bucket.phase) == "start" else values[-1]


def get_xml_live_stats(bucket: AggregateBucket, cfg: Config | None = None) -> dict[str, Any]:
    merged = bucket.computed.get(BILILIVE_SESSION_STATS_KEY)
    if isinstance(merged, dict) and isinstance(merged.get("xml_live_stats"), dict):
        return merged["xml_live_stats"]
    if cfg is None or not cfg.bililive_xml_base_dir:
        return empty_xml_stats()
    relative_path = get_bucket_field_value(bucket, "EventData.RelativePath")
    if not isinstance(relative_path, str) or not relative_path.strip():
        return empty_xml_stats()
    xml_path = derive_xml_path(relative_path, cfg.bililive_xml_base_dir, cfg.bililive_xml_strip_prefixes)
    cache_key = f"xml:{xml_path}:{cfg.bililive_gift_price_table}"
    cached = bucket.computed.get(cache_key)
    if isinstance(cached, dict):
        return cached
    stats = compute_xml_live_stats(xml_path, cfg.bililive_gift_price_table)
    bucket.computed[cache_key] = stats
    return stats


def build_aggregate_context(bucket: AggregateBucket, cfg: Config | None = None) -> dict[str, Any]:
    area_parent = get_bucket_field_value(bucket, "EventData.AreaNameParent")
    area_child = get_bucket_field_value(bucket, "EventData.AreaNameChild")
    relative_path = get_bucket_field_value(bucket, "EventData.RelativePath")
    duration_raw = get_bucket_field_value(bucket, "EventData.Duration")
    file_size_raw = get_bucket_field_value(bucket, "EventData.FileSize")
    ctx: dict[str, Any] = {
        "phase": bucket.phase,
        "group_name": bucket.group_name,
        "event_types": sorted(bucket.events.keys()),
        "request_count": len(bucket.request_ids),
        "event_count": len(bucket.events),
        "name": get_bucket_field_value(bucket, "EventData.Name") or "未知主播",
        "title": get_bucket_field_value(bucket, "EventData.Title") or "（无标题）",
        "room_id": get_bucket_field_value(bucket, "EventData.RoomId"),
        "short_id": get_bucket_field_value(bucket, "EventData.ShortId"),
        "session_id": get_bucket_field_value(bucket, "EventData.SessionId"),
        "area_parent": area_parent,
        "area_child": area_child,
        "area": "/".join(str(item) for item in [area_parent, area_child] if item not in {None, ""}),
        "file_path": relative_path,
        "file_name": truncate_middle(get_file_name(relative_path) or "", 84),
        "duration": format_duration_human(duration_raw),
        "duration_seconds": safe_float(duration_raw),
        "file_size": format_bytes_human(file_size_raw),
        "file_size_bytes": safe_int(file_size_raw),
        "recording": safe_bool(get_bucket_field_value(bucket, "EventData.Recording")),
        "streaming": safe_bool(get_bucket_field_value(bucket, "EventData.Streaming")),
        "has_stream_ended": "StreamEnded" in bucket.events,
        "has_file_closed": "FileClosed" in bucket.events,
        "has_session_ended": "SessionEnded" in bucket.events,
        "time": get_bucket_display_time(bucket),
    }

    merged = bucket.computed.get(BILILIVE_SESSION_STATS_KEY)
    if isinstance(merged, dict):
        if safe_float(merged.get("duration_seconds")) is not None:
            ctx["duration_seconds"] = safe_float(merged.get("duration_seconds"))
            ctx["duration"] = format_duration_human(ctx["duration_seconds"])
        if safe_int(merged.get("file_size_bytes")) is not None:
            ctx["file_size_bytes"] = safe_int(merged.get("file_size_bytes"))
            ctx["file_size"] = format_bytes_human(ctx["file_size_bytes"])
        for key in ("recording_segment_count", "recording_segment_paths", "recording_segment_names", "recording_segment_request_ids", "recording_segment_scope"):
            if key in merged:
                ctx[key] = merged[key]

    xml_stats = get_xml_live_stats(bucket, cfg)
    ctx.update({key: value for key, value in xml_stats.items() if not key.startswith("_")})
    return ctx


def build_bililive_notification_key(bucket: AggregateBucket) -> str | None:
    context = build_aggregate_context(bucket)
    room_id = context.get("room_id")
    name = str(context.get("name") or "").strip()
    title = str(context.get("title") or "").strip()
    if room_id in {None, ""} or not title:
        return None
    return f"bililive:{room_id}:{name}:{title}"


def build_start_bucket_score(bucket: AggregateBucket) -> tuple[int, int, int, int]:
    event_types = set(bucket.events)
    return (
        1 if "StreamStarted" in event_types else 0,
        1 if "SessionStarted" in event_types else 0,
        1 if "FileOpening" in event_types else 0,
        len(bucket.request_ids),
    )


def build_end_bucket_metrics(bucket: AggregateBucket, cfg: Config | None = None) -> dict[str, Any]:
    context = build_aggregate_context(bucket, cfg)
    xml_metrics = get_xml_live_stats(bucket, cfg)
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


def build_end_bucket_score(bucket: AggregateBucket, cfg: Config | None = None) -> tuple[int, int, int, int, int, int, int]:
    metrics = build_end_bucket_metrics(bucket, cfg)
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
    return bool(set(bucket.events) & {"FileClosed", "SessionEnded"})


def is_true_bililive_start_bucket(bucket: AggregateBucket) -> bool:
    return bucket.group_name == "bililive_start" and "StreamStarted" in bucket.events


def is_recording_segment_start_bucket(bucket: AggregateBucket) -> bool:
    if bucket.group_name != "bililive_start" or "StreamStarted" in bucket.events:
        return False
    return bool(set(bucket.events) & {"SessionStarted", "FileOpening"})


def is_true_bililive_end_bucket(bucket: AggregateBucket) -> bool:
    if bucket.group_name != "bililive_end":
        return False
    if "StreamEnded" in bucket.events:
        return True
    return build_end_bucket_metrics(bucket).get("streaming") is not True


def is_recording_segment_end_bucket(bucket: AggregateBucket) -> bool:
    if bucket.group_name != "bililive_end" or "StreamEnded" in bucket.events or not is_end_candidate_bucket(bucket):
        return False
    return build_end_bucket_metrics(bucket).get("streaming") is True


def is_meaningful_streaming_end_candidate(bucket: AggregateBucket) -> bool:
    if not is_recording_segment_end_bucket(bucket):
        return False
    metrics = build_end_bucket_metrics(bucket)
    return any(
        [
            (safe_float(metrics.get("duration_seconds")) or 0) >= 60.0,
            (safe_int(metrics.get("file_size_bytes")) or 0) >= 64 * 1024 * 1024,
            (safe_int(metrics.get("interaction_count_value")) or 0) >= 100,
            (safe_int(metrics.get("bullet_count_value")) or 0) >= 100,
            (safe_float(metrics.get("sc_total_value")) or 0.0) > 0.0,
            (safe_float(metrics.get("total_revenue_value")) or 0.0) > 1.0,
        ]
    )


def is_recent_tail_candidate_bucket(bucket: AggregateBucket) -> bool:
    if "StreamEnded" in bucket.events:
        return False
    metrics = build_end_bucket_metrics(bucket)
    checks: list[bool] = []
    thresholds = {
        "duration_seconds": 30.0,
        "file_size_bytes": 16 * 1024 * 1024,
        "interaction_count_value": 100,
        "bullet_count_value": 100,
        "sc_total_value": 0.0,
        "total_revenue_value": 1.0,
    }
    for key, threshold in thresholds.items():
        value = metrics.get(key)
        if value is not None:
            checks.append(value <= threshold)
    return bool(checks) and all(checks)


def should_suppress_recent_forwarded_end_candidate(
    recent_score: tuple[int, int, int, int, int, int, int], candidate_bucket: AggregateBucket
) -> bool:
    candidate_score = build_end_bucket_score(candidate_bucket)
    if candidate_score >= recent_score:
        return False
    return is_recent_tail_candidate_bucket(candidate_bucket)


def should_suppress_recent_forwarded_start_candidate(
    recent_score: tuple[int, int, int, int], candidate_bucket: AggregateBucket
) -> bool:
    _ = recent_score
    _ = candidate_bucket
    return True


def should_replace_aggregate_bucket_event(bucket: AggregateBucket, event_type: str, existing_payload: dict[str, Any], new_payload: dict[str, Any]) -> bool:
    if bucket.group_name == "bililive_end" and event_type == "FileClosed":
        existing_duration = safe_float(get_field_value(existing_payload, "EventData.Duration")) or 0.0
        new_duration = safe_float(get_field_value(new_payload, "EventData.Duration")) or 0.0
        existing_size = safe_int(get_field_value(existing_payload, "EventData.FileSize")) or -1
        new_size = safe_int(get_field_value(new_payload, "EventData.FileSize")) or -1
        return (int(round(new_duration)), new_size) >= (int(round(existing_duration)), existing_size)
    return True


def get_recent_start_suppress_window_ms(cfg: Config, bucket: AggregateBucket) -> int:
    return max(12 * 60 * 60 * 1000, cfg.notify_debounce_ms, cfg.aggregate_window_ms)


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


def remember_recent_forwarded_start(cfg: Config, notify_key: str | None, bucket: AggregateBucket) -> None:
    if not notify_key:
        return
    with RECENT_START_LOCK:
        RECENT_FORWARDED_STARTS[notify_key] = RecentForwardedStart(
            key=notify_key,
            score=build_start_bucket_score(bucket),
            expires_at=time.time() + get_recent_start_suppress_window_ms(cfg, bucket) / 1000,
        )


def clear_recent_forwarded_start(notify_key: str | None) -> None:
    if not notify_key:
        return
    with RECENT_START_LOCK:
        RECENT_FORWARDED_STARTS.pop(notify_key, None)


def get_recent_end_suppress_window_ms(cfg: Config) -> int:
    return max(300_000, cfg.notify_debounce_ms * 20)


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


def remember_recent_forwarded_end(cfg: Config, notify_key: str | None, bucket: AggregateBucket) -> None:
    if not notify_key:
        return
    with RECENT_END_LOCK:
        RECENT_FORWARDED_ENDS[notify_key] = RecentForwardedEnd(
            key=notify_key,
            score=build_end_bucket_score(bucket),
            expires_at=time.time() + get_recent_end_suppress_window_ms(cfg) / 1000,
        )


def clear_recent_forwarded_end(notify_key: str | None) -> None:
    if not notify_key:
        return
    with RECENT_END_LOCK:
        RECENT_FORWARDED_ENDS.pop(notify_key, None)


def cleanup_expired_live_session_segments(now_ts: float | None = None) -> None:
    now_ts = time.time() if now_ts is None else now_ts
    with LIVE_SESSION_SEGMENT_LOCK:
        for key in [key for key, acc in LIVE_SESSION_SEGMENTS.items() if acc.expires_at <= now_ts]:
            LIVE_SESSION_SEGMENTS.pop(key, None)


def build_live_session_segment_record(bucket: AggregateBucket, cfg: Config | None = None) -> dict[str, Any] | None:
    event = bucket.events.get("FileClosed")
    payload = event.get("payload") if isinstance(event, dict) else None
    if not isinstance(payload, dict):
        return None
    relative_path = get_field_value(payload, "EventData.RelativePath")
    duration_seconds = safe_float(get_field_value(payload, "EventData.Duration"))
    file_size_bytes = safe_int(get_field_value(payload, "EventData.FileSize"))
    if relative_path in {None, ""} and duration_seconds is None and file_size_bytes is None:
        return None
    segment_id = str(relative_path or event.get("request_id") or (bucket.request_ids[-1] if bucket.request_ids else uuid4().hex))
    return {
        "segment_id": segment_id,
        "relative_path": str(relative_path or ""),
        "file_name": get_file_name(relative_path),
        "duration_seconds": duration_seconds,
        "file_size_bytes": file_size_bytes,
        "xml_live_stats": copy.deepcopy(get_xml_live_stats(bucket, cfg)),
        "request_ids": list(bucket.request_ids),
        "created_at": time.time(),
    }


def merge_xml_live_stats_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    merged = empty_xml_stats()
    xml_paths: list[str] = []
    interaction_users: set[str] = set()
    gift_unknown: dict[str, int] = {}
    sums = {
        "bullet_count_value": 0,
        "sc_count_value": 0,
        "guard_count_value": 0,
        "captain_count": 0,
        "commander_count": 0,
        "governor_count": 0,
        "gift_total_value": 0.0,
        "sc_total_value": 0.0,
        "guard_total_value": 0.0,
    }
    xml_exists = False
    for record in records:
        stats = record.get("xml_live_stats") if isinstance(record.get("xml_live_stats"), dict) else {}
        xml_exists = xml_exists or bool(stats.get("xml_exists"))
        path = stats.get("xml_path")
        if isinstance(path, str) and path and path not in xml_paths:
            xml_paths.append(path)
        for key in ("bullet_count_value", "sc_count_value", "guard_count_value", "captain_count", "commander_count", "governor_count"):
            sums[key] += safe_int(stats.get(key)) or 0
        for key in ("gift_total_value", "sc_total_value", "guard_total_value"):
            sums[key] += safe_float(stats.get(key)) or 0.0
        raw_users = stats.get("_interaction_user_keys")
        if isinstance(raw_users, list):
            interaction_users.update(str(item) for item in raw_users if str(item).strip())
        raw_unknown = stats.get("_gift_unknown_counts")
        if isinstance(raw_unknown, dict):
            for name, count in raw_unknown.items():
                name_text = str(name or "").strip()
                if name_text:
                    gift_unknown[name_text] = gift_unknown.get(name_text, 0) + (safe_int(count) or 0)

    total_revenue = sums["gift_total_value"] + sums["sc_total_value"] + sums["guard_total_value"]
    gift_unknown_summary = "、".join(f"{name}×{count}" for name, count in sorted(gift_unknown.items()) if name and count)
    guard_increment_line = build_guard_increment_line(int(sums["captain_count"]), int(sums["commander_count"]), int(sums["governor_count"]))
    merged.update(
        {
            "xml_path": " | ".join(xml_paths),
            "xml_paths": xml_paths,
            "xml_exists": xml_exists,
            "bullet_count": str(sums["bullet_count_value"]),
            "bullet_count_value": int(sums["bullet_count_value"]),
            "bullet_count_display": format_count_k(sums["bullet_count_value"]),
            "interaction_count": str(len(interaction_users)),
            "interaction_count_value": len(interaction_users),
            "interaction_count_display": format_count_k(len(interaction_users)),
            "sc_count": str(sums["sc_count_value"]),
            "sc_count_value": int(sums["sc_count_value"]),
            "sc_total": format_money(sums["sc_total_value"]),
            "sc_total_value": sums["sc_total_value"],
            "captain_count": str(sums["captain_count"]),
            "commander_count": str(sums["commander_count"]),
            "governor_count": str(sums["governor_count"]),
            "guard_count": str(sums["guard_count_value"]),
            "guard_count_value": int(sums["guard_count_value"]),
            "guard_increment_line": guard_increment_line,
            "guard_total": format_money(sums["guard_total_value"]),
            "guard_total_value": sums["guard_total_value"],
            "gift_total": format_money(sums["gift_total_value"]),
            "gift_total_value": sums["gift_total_value"],
            "gift_unknown_summary": gift_unknown_summary,
            "gift_unknown_line": f"未知礼物：{gift_unknown_summary}" if gift_unknown_summary else "",
            "total_revenue": format_money(total_revenue),
            "total_revenue_value": total_revenue,
            "gift_total_label": "礼物营收（已知）" if gift_unknown_summary else "礼物营收",
            "total_revenue_label": "总营收（已知）" if gift_unknown_summary else "总营收",
            "_interaction_user_keys": sorted(interaction_users),
            "_gift_unknown_counts": dict(sorted(gift_unknown.items())),
        }
    )
    return merged


def remember_live_session_segment(cfg: Config, notify_key: str | None, bucket: AggregateBucket) -> bool:
    if not notify_key:
        return False
    record = build_live_session_segment_record(bucket, cfg)
    if record is None:
        return False
    now_ts = time.time()
    cleanup_expired_live_session_segments(now_ts)
    with LIVE_SESSION_SEGMENT_LOCK:
        acc = LIVE_SESSION_SEGMENTS.get(notify_key)
        if acc is None or acc.expires_at <= now_ts:
            acc = LiveSessionSegmentAccumulator(key=notify_key, expires_at=now_ts + cfg.live_session_segment_ttl_ms / 1000)
            LIVE_SESSION_SEGMENTS[notify_key] = acc
        else:
            acc.expires_at = max(acc.expires_at, now_ts + cfg.live_session_segment_ttl_ms / 1000)
        acc.segments[str(record["segment_id"])] = record
    return True


def clear_live_session_segments(notify_key: str | None) -> None:
    if not notify_key:
        return
    with LIVE_SESSION_SEGMENT_LOCK:
        LIVE_SESSION_SEGMENTS.pop(notify_key, None)


def apply_live_session_segments_to_bucket(notify_key: str | None, bucket: AggregateBucket, cfg: Config | None = None) -> dict[str, Any] | None:
    if not notify_key or bucket.group_name != "bililive_end":
        return None
    cleanup_expired_live_session_segments()
    with LIVE_SESSION_SEGMENT_LOCK:
        acc = LIVE_SESSION_SEGMENTS.get(notify_key)
        records_by_id = copy.deepcopy(acc.segments) if acc is not None else {}
    current = build_live_session_segment_record(bucket, cfg)
    if current is not None:
        records_by_id[str(current["segment_id"])] = current
    records = list(records_by_id.values())
    if not records:
        return None
    duration_values = [safe_float(record.get("duration_seconds")) for record in records]
    size_values = [safe_int(record.get("file_size_bytes")) for record in records]
    paths = [str(record.get("relative_path") or "") for record in records if str(record.get("relative_path") or "").strip()]
    names = [str(record.get("file_name") or "") for record in records if str(record.get("file_name") or "").strip()]
    request_ids: list[str] = []
    for record in records:
        for request_id in record.get("request_ids") if isinstance(record.get("request_ids"), list) else []:
            if str(request_id) not in request_ids:
                request_ids.append(str(request_id))
    merged = {
        "recording_segment_count": len(records),
        "recording_segment_paths": " | ".join(paths),
        "recording_segment_names": " | ".join(names),
        "recording_segment_request_ids": request_ids,
        "recording_segment_scope": "live_session" if len(records) > 1 else "single_segment",
        "duration_seconds": sum(value for value in duration_values if value is not None) if any(value is not None for value in duration_values) else None,
        "file_size_bytes": sum(value for value in size_values if value is not None) if any(value is not None for value in size_values) else None,
        "xml_live_stats": merge_xml_live_stats_records(records),
    }
    bucket.computed[BILILIVE_SESSION_STATS_KEY] = merged
    return merged


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


def build_bililive_message(bucket: AggregateBucket, cfg: Config | None = None) -> str:
    context = build_aggregate_context(bucket, cfg)
    if bucket.phase == "start":
        lines = [f"🟢［{context['name']}］开播啦！"]
        candidates = [
            ("标题", context.get("title")),
            ("分区", context.get("area")),
            ("房间", context.get("room_id")),
            ("时间", context.get("time")),
        ]
    else:
        lines = [f"🔴［{context['name']}］下播了"]
        candidates = [
            ("标题", context.get("title")),
            ("时长", context.get("duration") if context.get("duration_seconds") is not None else None),
            ("文件", context.get("recording_segment_names") or context.get("file_name")),
            ("大小", context.get("file_size") if context.get("file_size_bytes") is not None else None),
            ("弹幕", context.get("bullet_count_display") if context.get("bullet_count_value") is not None else None),
            ("互动", context.get("interaction_count_display") if context.get("interaction_count_value") is not None else None),
            ("SC数量", context.get("sc_count") if context.get("sc_count_value") is not None else None),
            ("SC金额", f"¥{context.get('sc_total')}" if context.get("sc_total_value") is not None else None),
            ("舰长", context.get("captain_count") if context.get("captain_count") not in {None, ""} else None),
            ("提督", context.get("commander_count") if context.get("commander_count") not in {None, ""} else None),
            ("总督", context.get("governor_count") if context.get("governor_count") not in {None, ""} else None),
            (str(context.get("gift_total_label") or "礼物营收"), f"¥{context.get('gift_total')}" if context.get("gift_total_value") is not None else None),
            (str(context.get("total_revenue_label") or "总营收"), f"¥{context.get('total_revenue')}" if context.get("total_revenue_value") is not None else None),
            ("时间", context.get("time")),
        ]
    for label, value in candidates:
        if value not in {None, ""}:
            lines.append(f"{label}：{value}")
    if bucket.phase == "end" and context.get("gift_unknown_line"):
        lines.append(str(context["gift_unknown_line"]))
    return "\n".join(lines)


def write_bililive_log(
    cfg: Config,
    bucket: AggregateBucket,
    *,
    outcome: str,
    reason: str = "",
    text: str | None = None,
    delivery: DeliveryReport | None = None,
    debounce: dict[str, Any] | None = None,
) -> None:
    record: dict[str, Any] = {
        "ts": now_iso(),
        "request_id": bucket.request_ids[0] if bucket.request_ids else uuid4().hex,
        "related_request_ids": list(bucket.request_ids),
        "layer": "message",
        "route": "bililive",
        "outcome": outcome,
        "reason": reason,
        "request": {"path": bucket.request_path, "remote_ip": bucket.remote_ip},
        "auth": bucket.auth,
        "event_types": sorted(bucket.events),
        "context": sanitize_for_log(build_aggregate_context(bucket, cfg)),
    }
    if text is not None:
        record["forward_text"] = text
    if debounce:
        record["debounce"] = debounce
    if delivery is not None:
        record.update(delivery.to_log())
    append_message_log(cfg, record)
    if outcome == "failed":
        append_error_log(cfg, {**record, "layer": "error", "stage": "egress", "error_type": "bililive_forward_failed"})


def deliver_aggregate_bucket(cfg: Config, bucket: AggregateBucket, debounce: dict[str, Any] | None = None) -> None:
    notify_key = build_bililive_notification_key(bucket)
    if bucket.group_name == "bililive_end" and is_true_bililive_end_bucket(bucket):
        apply_live_session_segments_to_bucket(notify_key, bucket, cfg)
    text = build_bililive_message(bucket, cfg)
    targets = default_targets(cfg)
    if not targets:
        write_bililive_log(cfg, bucket, outcome="failed", reason="no default NapCat targets configured", text=text, debounce=debounce)
        return
    report = send_text(cfg, text, targets)
    outcome = "failed" if report.all_failed else "forwarded"
    write_bililive_log(cfg, bucket, outcome=outcome, text=text, delivery=report, debounce=debounce)
    if report.all_failed:
        return
    if bucket.group_name == "bililive_start":
        remember_recent_forwarded_start(cfg, notify_key, bucket)
    elif bucket.group_name == "bililive_end":
        if is_true_bililive_end_bucket(bucket):
            clear_recent_forwarded_start(notify_key)
            clear_live_session_segments(notify_key)
        remember_recent_forwarded_end(cfg, notify_key, bucket)
    eprint(json.dumps({"event": "bililive_forwarded", "phase": bucket.phase, "event_types": sorted(bucket.events), "request_ids": bucket.request_ids}, ensure_ascii=False))


def suppress_aggregate_bucket(cfg: Config, bucket: AggregateBucket, *, reason: str, debounce: dict[str, Any] | None = None) -> None:
    write_bililive_log(cfg, bucket, outcome="suppressed", reason=reason, text=build_bililive_message(bucket, cfg), debounce=debounce)
    eprint(json.dumps({"event": "bililive_suppressed", "reason": reason, "event_types": sorted(bucket.events), "request_ids": bucket.request_ids}, ensure_ascii=False))


def flush_pending_start_after_end(cfg: Config, notify_key: str) -> None:
    with PENDING_START_AFTER_END_LOCK:
        pending = PENDING_START_AFTER_END_NOTIFICATIONS.pop(notify_key, None)
    if pending is None:
        return
    clear_recent_forwarded_end(notify_key)
    deliver_aggregate_bucket(
        cfg,
        pending.bucket,
        debounce={"mode": "post_end_start_confirm_window", "status": "expired_forwarded_reconnect_start", "key": notify_key, "window_ms": pending.window_ms},
    )


def hold_start_after_recent_end(cfg: Config, notify_key: str, bucket: AggregateBucket, preview_text: str = "") -> bool:
    window_ms = cfg.post_end_start_confirm_ms
    if window_ms <= 0:
        return False
    now_ts = time.time()
    created = False
    with PENDING_START_AFTER_END_LOCK:
        pending = PENDING_START_AFTER_END_NOTIFICATIONS.get(notify_key)
        if pending is not None and pending.expires_at <= now_ts:
            PENDING_START_AFTER_END_NOTIFICATIONS.pop(notify_key, None)
            pending = None
        if pending is None:
            timer = threading.Timer(window_ms / 1000, flush_pending_start_after_end, args=(cfg, notify_key))
            timer.daemon = True
            pending = PendingStartAfterEndNotification(notify_key, bucket, now_ts + window_ms / 1000, timer=timer, window_ms=window_ms)
            PENDING_START_AFTER_END_NOTIFICATIONS[notify_key] = pending
            timer.start()
            created = True
        else:
            merge_aggregate_bucket(pending.bucket, bucket)
    write_bililive_log(
        cfg,
        bucket,
        outcome="held",
        reason="start_after_recent_end",
        text=preview_text or build_bililive_message(bucket, cfg),
        debounce={
            "mode": "post_end_start_confirm_window",
            "status": "held_reconnect_start_after_recent_end" if created else "merged_reconnect_start_after_recent_end",
            "key": notify_key,
            "window_ms": window_ms,
        },
    )
    return True


def cancel_pending_start_after_end(cfg: Config, notify_key: str, *, reason: str, end_bucket: AggregateBucket | None = None) -> bool:
    with PENDING_START_AFTER_END_LOCK:
        pending = PENDING_START_AFTER_END_NOTIFICATIONS.pop(notify_key, None)
    if pending is None:
        return False
    if pending.timer is not None:
        pending.timer.cancel()
    suppress_aggregate_bucket(
        cfg,
        pending.bucket,
        reason=reason,
        debounce={"mode": "post_end_start_confirm_window", "status": reason, "key": notify_key},
    )
    return True


def handle_start_bucket(cfg: Config, bucket: AggregateBucket) -> None:
    notify_key = build_bililive_notification_key(bucket)
    if not is_true_bililive_start_bucket(bucket):
        suppress_aggregate_bucket(cfg, bucket, reason="recording_segment_start_without_streamstarted")
        return
    if notify_key:
        recent_start = get_recent_forwarded_start(notify_key)
        if recent_start and should_suppress_recent_forwarded_start_candidate(recent_start.score, bucket):
            suppress_aggregate_bucket(cfg, bucket, reason="duplicate_start_after_recent_forwarded_start")
            return
        recent_end = get_recent_forwarded_end(notify_key)
        if recent_end and hold_start_after_recent_end(cfg, notify_key, bucket, build_bililive_message(bucket, cfg)):
            return
        clear_recent_forwarded_end(notify_key)
    deliver_aggregate_bucket(cfg, bucket, debounce={"mode": "start", "status": "sent"})


def flush_pending_end_notification(cfg: Config, notify_key: str) -> None:
    with PENDING_END_LOCK:
        pending = PENDING_END_NOTIFICATIONS.pop(notify_key, None)
    if pending is None:
        return
    bucket = pending.bucket or pending.stream_end_bucket
    if pending.bucket is not None and pending.stream_end_bucket is not None and pending.stream_end_bucket is not pending.bucket:
        bucket = merge_aggregate_bucket(pending.bucket, pending.stream_end_bucket)
    if bucket is None:
        return
    debounce = {"mode": "pending_end_window", "status": "expired_forwarded", "key": notify_key, "window_ms": pending.window_ms}
    if is_recording_segment_end_bucket(bucket):
        remember_live_session_segment(cfg, notify_key, bucket)
        suppress_aggregate_bucket(cfg, bucket, reason="recording_segment_end_while_streaming", debounce=debounce)
        return
    deliver_aggregate_bucket(cfg, bucket, debounce=debounce)


def handle_end_bucket(cfg: Config, bucket: AggregateBucket) -> None:
    notify_key = build_bililive_notification_key(bucket)
    if notify_key and is_recording_segment_end_bucket(bucket):
        remember_live_session_segment(cfg, notify_key, bucket)

    if notify_key and is_true_bililive_end_bucket(bucket):
        if cancel_pending_start_after_end(cfg, notify_key, reason="cancelled_by_followup_true_end", end_bucket=bucket):
            suppress_aggregate_bucket(cfg, bucket, reason="followup_true_end_cancelled_pending_reconnect_start")
            return

    if not notify_key or cfg.notify_debounce_ms <= 0:
        if is_recording_segment_end_bucket(bucket):
            suppress_aggregate_bucket(cfg, bucket, reason="recording_segment_end_while_streaming")
        else:
            deliver_aggregate_bucket(cfg, bucket)
        return

    recent = get_recent_forwarded_end(notify_key)
    if recent is not None and is_end_candidate_bucket(bucket) and should_suppress_recent_forwarded_end_candidate(recent.score, bucket):
        suppress_aggregate_bucket(cfg, bucket, reason="tail_after_recent_forwarded_end")
        return

    now_ts = time.time()
    created = False
    with PENDING_END_LOCK:
        pending = PENDING_END_NOTIFICATIONS.get(notify_key)
        if pending is None or pending.expires_at <= now_ts:
            timer = threading.Timer(cfg.notify_debounce_ms / 1000, flush_pending_end_notification, args=(cfg, notify_key))
            timer.daemon = True
            pending = PendingEndNotification(
                key=notify_key,
                bucket=None,
                expires_at=now_ts + cfg.notify_debounce_ms / 1000,
                timer=timer,
                window_ms=cfg.notify_debounce_ms,
            )
            PENDING_END_NOTIFICATIONS[notify_key] = pending
            timer.start()
            created = True

        if "StreamEnded" in bucket.events:
            pending.stream_end_bucket = merge_aggregate_bucket(pending.stream_end_bucket, bucket) if pending.stream_end_bucket else bucket
            if pending.bucket is not None and pending.stream_end_bucket is not pending.bucket:
                merge_aggregate_bucket(pending.bucket, pending.stream_end_bucket)

        if is_end_candidate_bucket(bucket):
            candidate = bucket
            if pending.stream_end_bucket is not None and pending.stream_end_bucket is not candidate:
                merge_aggregate_bucket(candidate, pending.stream_end_bucket)
            previous = pending.bucket
            previous_score = build_end_bucket_score(previous) if previous is not None else None
            candidate_score = build_end_bucket_score(candidate)
            if previous is None or previous_score is None or candidate_score > previous_score:
                pending.bucket = candidate
                status = "held_pending_end_candidate" if previous is None else "replaced_pending_end_candidate"
            else:
                status = "suppressed_weaker_pending_end_candidate"
        elif "StreamEnded" in bucket.events:
            status = "held_stream_end_waiting_for_stats" if pending.bucket is None else "merged_stream_end_into_pending"
        else:
            status = "held_pending_end"

    if status == "suppressed_weaker_pending_end_candidate":
        suppress_aggregate_bucket(cfg, bucket, reason=status)
        return
    write_bililive_log(
        cfg,
        bucket,
        outcome="held",
        reason=status,
        text=build_bililive_message(bucket, cfg),
        debounce={"mode": "pending_end_window", "status": status, "key": notify_key, "window_ms": cfg.notify_debounce_ms, "created_pending": created},
    )


def handle_aggregate_notification(cfg: Config, bucket: AggregateBucket) -> None:
    if bucket.phase == "start":
        handle_start_bucket(cfg, bucket)
    else:
        handle_end_bucket(cfg, bucket)


def flush_aggregate_bucket(cfg: Config, key: str) -> None:
    with AGGREGATE_LOCK:
        bucket = AGGREGATE_BUCKETS.pop(key, None)
    if bucket is not None:
        handle_aggregate_notification(cfg, bucket)


def queue_bililive_event(
    cfg: Config,
    payload: dict[str, Any],
    *,
    request_id: str,
    request_meta: dict[str, Any],
    auth: dict[str, Any],
    payload_summary: str = "",
) -> dict[str, Any]:
    event_type = str(payload.get("EventType"))
    phase = event_phase(event_type)
    key = bucket_key(payload, phase)
    group_name = f"bililive_{phase}"
    should_start_timer = False
    should_flush_now = False
    window_ms = cfg.aggregate_window_ms

    with AGGREGATE_LOCK:
        bucket = AGGREGATE_BUCKETS.get(key)
        if bucket is None:
            bucket = AggregateBucket(
                key=key,
                phase=phase,
                group_name=group_name,
                group_config=default_group_config(phase),
                created_at=time.time(),
                request_path=request_meta.get("path", ""),
                remote_ip=request_meta.get("remote_ip", ""),
                auth=auth,
                target={"private": cfg.private, "group": cfg.group},
            )
            AGGREGATE_BUCKETS[key] = bucket
            should_start_timer = window_ms > 0
            should_flush_now = window_ms <= 0

        if request_id not in bucket.request_ids:
            bucket.request_ids.append(request_id)
        existing = bucket.events.get(event_type)
        should_replace = True
        if isinstance(existing, dict) and isinstance(existing.get("payload"), dict):
            should_replace = should_replace_aggregate_bucket_event(bucket, event_type, existing["payload"], payload)
        if should_replace:
            bucket.events[event_type] = {"request_id": request_id, "payload": payload, "ts": now_iso()}
        if payload_summary:
            bucket.payload_summaries.append(payload_summary)
        bucket.request_path = request_meta.get("path", "")
        bucket.remote_ip = request_meta.get("remote_ip", "")
        bucket.auth = auth
        bucket.target = {"private": cfg.private, "group": cfg.group}

        if should_start_timer:
            timer = threading.Timer(window_ms / 1000, flush_aggregate_bucket, args=(cfg, key))
            timer.daemon = True
            bucket.timer = timer
            timer.start()

    if should_flush_now:
        flush_aggregate_bucket(cfg, key)

    return {"queued": True, "phase": phase, "group_name": group_name, "group_key": key, "window_ms": window_ms, "event_type": event_type}


def handle_bililive_notification(
    cfg: Config,
    payload: dict[str, Any],
    *,
    request_id: str,
    request_meta: dict[str, Any],
    auth: dict[str, Any],
    payload_summary: str = "",
) -> HandlerResult:
    meta = queue_bililive_event(cfg, payload, request_id=request_id, request_meta=request_meta, auth=auth, payload_summary=payload_summary)
    return HandlerResult(200, {"ok": True, "route": "bililive", "request_id": request_id, "aggregate": meta})
