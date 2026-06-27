from __future__ import annotations

from .bililive_context import build_aggregate_context
from .bililive_model import AggregateBucket
from .config import Config


def _has_number(context: dict, key: str) -> bool:
    value = context.get(key)
    return isinstance(value, (int, float)) and value > 0


def _present(value) -> bool:
    return value not in {None, ""}


def _append_end_stats(lines: list[str], context: dict) -> None:
    duration = context.get("duration") if context.get("duration_seconds") is not None else None
    file_size = context.get("file_size") if context.get("file_size_bytes") is not None else None
    if _present(duration) and _present(file_size):
        lines.append(f"时长：{duration}｜大小：{file_size}")
    elif _present(duration):
        lines.append(f"时长：{duration}")
    elif _present(file_size):
        lines.append(f"大小：{file_size}")

    interaction = context.get("interaction_count_display") if context.get("interaction_count_value") is not None else None
    bullets = context.get("bullet_count_display") if context.get("bullet_count_value") is not None else None
    if _present(interaction) and _present(bullets):
        lines.append(f"互动人数：{interaction}｜弹幕：{bullets}")
    elif _present(interaction):
        lines.append(f"互动人数：{interaction}")
    elif _present(bullets):
        lines.append(f"弹幕：{bullets}")

    guard_line = context.get("guard_increment_line")
    if _present(guard_line):
        lines.append(str(guard_line))

    sc_count = context.get("sc_count") if _has_number(context, "sc_count_value") else None
    sc_total = context.get("sc_total") if _has_number(context, "sc_total_value") else None
    if _present(sc_count) and _present(sc_total):
        lines.append(f"SC数量 ： {sc_count}｜ 金额：¥{sc_total}")
    elif _present(sc_count):
        lines.append(f"SC数量 ： {sc_count}")
    elif _present(sc_total):
        lines.append(f"SC金额：¥{sc_total}")

    total_revenue = context.get("total_revenue") if _has_number(context, "total_revenue_value") else None
    if _present(total_revenue):
        label = context.get("total_revenue_label") or "总营收"
        lines.append(f"{label}：¥{total_revenue}")
    if _present(context.get("gift_unknown_line")):
        lines.append(str(context["gift_unknown_line"]))


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
        if _present(context.get("title")):
            lines.append(f"标题：{context['title']}")
        _append_end_stats(lines, context)
        if _present(context.get("time")):
            lines.append(f"时间：{context['time']}")
        return "\n".join(lines)
    for label, value in candidates:
        if _present(value):
            lines.append(f"{label}：{value}")
    return "\n".join(lines)
