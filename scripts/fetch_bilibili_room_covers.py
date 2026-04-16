#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def fetch_text(url: str, headers: dict[str, str] | None = None, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")



def fetch_binary(url: str, headers: dict[str, str] | None = None, timeout: float = 20.0) -> tuple[bytes, str | None]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get_content_type()



def extract_room_info(html: str) -> dict[str, Any]:
    match = re.search(r"__NEPTUNE_IS_MY_WAIFU__\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except Exception:
        return {}
    room_info = (((data.get("roomInfoRes") or {}).get("data") or {}).get("room_info") or {})
    return room_info if isinstance(room_info, dict) else {}



def normalize_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url



def guess_ext(url: str, content_type: str | None) -> str:
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    if content_type:
        return EXT_BY_CONTENT_TYPE.get(content_type.lower(), ".jpg")
    return ".jpg"



def parse_room_arg(raw: str) -> tuple[str, str | None]:
    raw = raw.strip()
    if not raw:
        raise ValueError("empty room spec")
    if ":" in raw:
        room_id, name = raw.split(":", 1)
        return room_id.strip(), name.strip() or None
    return raw, None



def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch Bilibili live room covers and build /opt cover index.json")
    ap.add_argument("--room", action="append", required=True, help="Room spec: <room_id> or <room_id>:<display_name>; repeatable")
    ap.add_argument("--output-dir", default="/opt/WebhookToNapcat/cover")
    ap.add_argument("--index-name", default="index.json")
    args = ap.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {}

    for raw_room in args.room:
        room_id, display_name = parse_room_arg(raw_room)
        page_url = f"https://live.bilibili.com/{urllib.parse.quote(room_id)}"
        headers = {
            "User-Agent": UA,
            "Referer": page_url,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }
        html = fetch_text(page_url, headers=headers)
        room_info = extract_room_info(html)
        cover_url = normalize_url(str(room_info.get("cover") or room_info.get("user_cover") or "").strip())
        if not cover_url:
            raise RuntimeError(f"room {room_id}: cover url not found")

        data, content_type = fetch_binary(cover_url, headers={"User-Agent": UA, "Referer": page_url})
        if not data:
            raise RuntimeError(f"room {room_id}: downloaded cover is empty")

        ext = guess_ext(cover_url, content_type)
        target_path = output_dir / f"{room_id}{ext}"
        target_path.write_bytes(data)

        index[str(room_id)] = {
            "room_id": str(room_id),
            "name": display_name or room_info.get("uname") or room_info.get("title") or str(room_id),
            "url": cover_url,
            "path": str(target_path),
            "content_type": content_type,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
        print(f"saved room {room_id} -> {target_path}")

    index_path = output_dir / args.index_name
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote index -> {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
