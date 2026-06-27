from __future__ import annotations

import re
from typing import Any

from .config import Config
from .bililive_model import AggregateBucket, BILILIVE_SESSION_STATS_KEY
from .bililive_xml import compute_xml_live_stats, derive_xml_path, empty_xml_stats, format_bytes_human, format_duration_human, get_file_name
from .utils import get_field_value, safe_bool, safe_float, safe_int, truncate_middle


def get_bucket_field_value(bucket: AggregateBucket, field: str) -> Any:
    ordered_types = [str(item) for item in bucket.event_order]
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
    normalized = value.strip().replace("T", " ")
    match = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", normalized)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return normalized.replace("Z", "")


def get_bucket_display_time(bucket: AggregateBucket, mode: str | None = None) -> str | None:
    values: list[str] = []
    ordered_types = [str(item) for item in bucket.event_order]
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


def should_replace_aggregate_bucket_event(bucket: AggregateBucket, event_type: str, existing_payload: dict[str, Any], new_payload: dict[str, Any]) -> bool:
    if bucket.group_name == "bililive_end" and event_type == "FileClosed":
        existing_duration = safe_float(get_field_value(existing_payload, "EventData.Duration")) or 0.0
        new_duration = safe_float(get_field_value(new_payload, "EventData.Duration")) or 0.0
        existing_size = safe_int(get_field_value(existing_payload, "EventData.FileSize")) or -1
        new_size = safe_int(get_field_value(new_payload, "EventData.FileSize")) or -1
        return (int(round(new_duration)), new_size) >= (int(round(existing_duration)), existing_size)
    return True
