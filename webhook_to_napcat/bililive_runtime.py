from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any
from uuid import uuid4

from .bililive_context import (
    build_aggregate_context,
    build_bililive_notification_key,
    build_end_bucket_score,
    build_start_bucket_score,
    get_xml_live_stats,
    get_bucket_field_value,
    is_end_candidate_bucket,
    is_recording_segment_end_bucket,
    is_true_bililive_end_bucket,
    is_true_bililive_start_bucket,
    should_replace_aggregate_bucket_event,
    should_suppress_recent_forwarded_end_candidate,
)
from .bililive_message import build_bililive_message
from .bililive_model import (
    AggregateBucket,
    BILILIVE_SESSION_STATS_KEY,
    LiveSessionSegmentAccumulator,
    PendingEndNotification,
    PendingStartAfterEndNotification,
    RecentForwardedEnd,
    RecentForwardedStart,
    bucket_key,
    default_event_order,
    event_phase,
)
from .bililive_xml import PRICE_TABLE_CACHE, build_guard_increment_line, empty_xml_stats, format_count_k, format_money, get_file_name
from .config import Config
from .internal import HandlerResult
from .logs import append_error_log, append_message_log, eprint
from .media import sanitize_for_log
from .napcat import DeliveryReport, resolve_named_targets, send_text
from .utils import get_field_value, now_iso, safe_float, safe_int


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


def reset_bililive_state() -> None:
    with AGGREGATE_LOCK:
        AGGREGATE_BUCKETS.clear()

    with PENDING_END_LOCK:
        pending_ends = list(PENDING_END_NOTIFICATIONS.values())
        PENDING_END_NOTIFICATIONS.clear()
    for pending in pending_ends:
        if pending.timer is not None:
            pending.timer.cancel()

    with PENDING_START_AFTER_END_LOCK:
        pending_starts = list(PENDING_START_AFTER_END_NOTIFICATIONS.values())
        PENDING_START_AFTER_END_NOTIFICATIONS.clear()
    for pending in pending_starts:
        if pending.timer is not None:
            pending.timer.cancel()

    with RECENT_START_LOCK:
        RECENT_FORWARDED_STARTS.clear()
    with RECENT_END_LOCK:
        RECENT_FORWARDED_ENDS.clear()
    with LIVE_SESSION_SEGMENT_LOCK:
        LIVE_SESSION_SEGMENTS.clear()
    PRICE_TABLE_CACHE.clear()


def get_recent_start_suppress_window_ms(cfg: Config) -> int:
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
            expires_at=time.time() + get_recent_start_suppress_window_ms(cfg) / 1000,
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

    if not xml_exists:
        merged.update({"xml_path": " | ".join(xml_paths), "xml_paths": xml_paths})
        return merged

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
    target.request_path = source.request_path
    target.remote_ip = source.remote_ip
    target.auth = source.auth
    target.computed.clear()
    return target


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


def resolve_bililive_targets(cfg: Config, bucket: AggregateBucket):
    room_id = get_bucket_field_value(bucket, "EventData.RoomId")
    specs = cfg.bililive_targets.get(str(room_id).strip()) if room_id not in {None, ""} else None
    return resolve_named_targets(cfg, specs)


def deliver_aggregate_bucket(cfg: Config, bucket: AggregateBucket, debounce: dict[str, Any] | None = None) -> None:
    notify_key = build_bililive_notification_key(bucket)
    if bucket.group_name == "bililive_end" and is_true_bililive_end_bucket(bucket):
        apply_live_session_segments_to_bucket(notify_key, bucket, cfg)
    text = build_bililive_message(bucket, cfg)
    targets = resolve_bililive_targets(cfg, bucket)
    if not targets:
        write_bililive_log(cfg, bucket, outcome="failed", reason="no Bililive NapCat targets configured", text=text, debounce=debounce)
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


def cancel_pending_start_after_end(cfg: Config, notify_key: str, *, reason: str) -> bool:
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
        if recent_start is not None:
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
        if cancel_pending_start_after_end(cfg, notify_key, reason="cancelled_by_followup_true_end"):
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
                event_order=default_event_order(phase),
                request_path=request_meta.get("path", ""),
                remote_ip=request_meta.get("remote_ip", ""),
                auth=auth,
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
        bucket.request_path = request_meta.get("path", "")
        bucket.remote_ip = request_meta.get("remote_ip", "")
        bucket.auth = auth

        if should_start_timer:
            timer = threading.Timer(window_ms / 1000, flush_aggregate_bucket, args=(cfg, key))
            timer.daemon = True
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
) -> HandlerResult:
    meta = queue_bililive_event(cfg, payload, request_id=request_id, request_meta=request_meta, auth=auth)
    return HandlerResult(200, {"ok": True, "route": "bililive", "request_id": request_id, "aggregate": meta})
