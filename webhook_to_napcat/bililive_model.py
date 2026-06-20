from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .utils import get_field_value


START_EVENTS = {"StreamStarted", "SessionStarted", "FileOpening"}
END_EVENTS = {"FileClosed", "SessionEnded", "StreamEnded"}
BILILIVE_EVENTS = START_EVENTS | END_EVENTS
BILILIVE_SESSION_STATS_KEY = "__bililive_live_session_merged_stats"

@dataclass
class AggregateBucket:
    key: str
    phase: str
    group_name: str
    event_order: list[str]
    request_path: str
    remote_ip: str
    auth: dict[str, Any]
    request_ids: list[str] = field(default_factory=list)
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    computed: dict[str, Any] = field(default_factory=dict)


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


def default_event_order(phase: str) -> list[str]:
    if phase == "start":
        return ["StreamStarted", "SessionStarted", "FileOpening"]
    return ["FileClosed", "SessionEnded", "StreamEnded"]


def bucket_key(payload: dict[str, Any], phase: str) -> str:
    room_id = get_field_value(payload, "EventData.RoomId")
    name = get_field_value(payload, "EventData.Name")
    return f"bililive:{phase}:{room_id or '_'}:{name or '_'}"
