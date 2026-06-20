from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .logs import eprint
from .utils import safe_float, safe_int


PRICE_TABLE_CACHE: dict[str, dict[str, float]] = {}


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
