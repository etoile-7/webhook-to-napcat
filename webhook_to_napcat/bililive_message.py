from __future__ import annotations

from .bililive_context import build_aggregate_context
from .bililive_model import AggregateBucket
from .config import Config


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
