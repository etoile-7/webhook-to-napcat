from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Config


BASE64_DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.*)$", re.S)
BASE64_FIELD_NAMES = {
    "base64",
    "base64_data",
    "base64_png",
    "png_base64",
    "image_base64",
    "cover_base64",
    "content_base64",
    "file_base64",
    "raw_base64",
    "blob",
}
MEDIA_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/json": ".json",
    "text/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/markdown": ".md",
    "text/html": ".html",
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/octet-stream": ".bin",
}


@dataclass(frozen=True)
class PersistedMedia:
    public_path: str
    internal_path: str
    file_name: str
    mime_type: str
    size_bytes: int
    sha256: str
    kind: str = "file"
    caption: str = ""

    @property
    def is_image(self) -> bool:
        return self.kind == "image" or self.mime_type.startswith("image/")

    def payload_summary(self) -> dict[str, Any]:
        return {
            "base64_omitted": True,
            "saved": True,
            "path": self.public_path,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }

    def log_summary(self) -> dict[str, Any]:
        data = self.payload_summary()
        data["internal_path"] = self.internal_path
        data["file_name"] = self.file_name
        data["type"] = self.kind
        if self.caption:
            data["caption"] = self.caption
        return data


def decode_base64_media(value: str) -> tuple[bytes | None, str | None]:
    text = value.strip()
    mime_type: str | None = None
    match = BASE64_DATA_URI_RE.match(text)
    if match:
        mime_type = match.group("mime").strip().lower() or None
        text = match.group("data").strip()
    elif text.startswith("base64://"):
        text = text[len("base64://") :].strip()

    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None, mime_type

    try:
        return base64.b64decode(compact, validate=True), mime_type
    except Exception:
        try:
            padded = compact + ("=" * (-len(compact) % 4))
            return base64.b64decode(padded, validate=False), mime_type
        except Exception:
            return None, mime_type


def looks_like_base64_blob(value: str) -> bool:
    text = value.strip()
    if text.startswith("base64://") or BASE64_DATA_URI_RE.match(text):
        return True
    if len(text) < 512:
        return False
    compact = re.sub(r"\s+", "", text)
    return len(compact) >= 512 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact) is not None


def summarize_base64_for_log(value: str, key: str | None = None) -> dict[str, Any]:
    data, uri_mime_type = decode_base64_media(value)
    compact = value.strip()
    match = BASE64_DATA_URI_RE.match(compact)
    if match:
        compact = match.group("data").strip()
    elif compact.startswith("base64://"):
        compact = compact[len("base64://") :].strip()
    compact = re.sub(r"\s+", "", compact)

    summary: dict[str, Any] = {
        "base64_omitted": True,
        "chars": len(value),
        "data_chars": len(compact),
    }
    if key:
        summary["field"] = key
    if uri_mime_type:
        summary["mime_type"] = uri_mime_type
    if data is not None:
        summary["decoded_bytes"] = len(data)
        summary["sha256"] = hashlib.sha256(data).hexdigest()
    return summary


def sanitize_for_log(value: Any, key: str | None = None) -> Any:
    if isinstance(value, PersistedMedia):
        return value.log_summary()
    if isinstance(value, str):
        key_name = (key or "").strip().lower()
        if key_name in BASE64_FIELD_NAMES or looks_like_base64_blob(value):
            return summarize_base64_for_log(value, key=key_name or None)
        return value
    if isinstance(value, list):
        return [sanitize_for_log(item, key=key) for item in value]
    if isinstance(value, dict):
        return {str(k): sanitize_for_log(v, key=str(k)) for k, v in value.items()}
    return value


def media_extension(mime_type: str | None, file_name: Any = None, default: str = ".bin") -> str:
    if isinstance(file_name, str) and file_name.strip():
        suffix = Path(file_name.strip()).suffix.lower()
        if suffix:
            return suffix
    return MEDIA_MIME_EXTENSIONS.get((mime_type or "").strip().lower(), default)


def detect_mime_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    return None


def file_to_base64_uri(path: str) -> str | None:
    try:
        data = Path(path).read_bytes()
    except Exception:
        return None
    if not data:
        return None
    return "base64://" + base64.b64encode(data).decode("ascii")


def safe_path_component(value: Any, default: str = "asset") -> str:
    raw = str(value or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-_")
    return (cleaned[:80] if cleaned else default)


def save_media_bytes(
    cfg: Config,
    data: bytes,
    *,
    file_name: Any = None,
    mime_type: str | None = None,
    request_id: str = "",
    namespace: str = "webhook_media",
    path_hint: Any = "payload",
    kind: str = "file",
    caption: str = "",
) -> PersistedMedia:
    resolved_mime = (mime_type or detect_mime_type(data) or "application/octet-stream").strip().lower()
    if kind == "file" and resolved_mime.startswith("image/"):
        kind = "image"
    ext = media_extension(resolved_mime, file_name=file_name)
    stem_source = Path(str(file_name)).stem if file_name else request_id or uuid4().hex
    stem = safe_path_component(stem_source, default="media")
    date_part = datetime.now().strftime("%Y-%m-%d")
    rel_path = Path(safe_path_component(namespace, "webhook_media")) / date_part / safe_path_component(path_hint) / f"{stem}{ext}"
    save_path = Path(cfg.media_dir or "media") / rel_path
    public_path = Path(cfg.public_media_dir or cfg.media_dir or "media") / rel_path
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(data)
    return PersistedMedia(
        public_path=str(public_path),
        internal_path=str(save_path),
        file_name=Path(str(file_name or save_path.name)).name,
        mime_type=resolved_mime,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        kind=kind,
        caption=caption,
    )


def save_base64_media(
    cfg: Config,
    value: str,
    *,
    file_name: Any = None,
    mime_type: str | None = None,
    request_id: str = "",
    namespace: str = "webhook_media",
    path_hint: Any = "payload",
    kind: str = "file",
    caption: str = "",
) -> PersistedMedia | None:
    data, uri_mime_type = decode_base64_media(value)
    if data is None:
        return None
    return save_media_bytes(
        cfg,
        data,
        file_name=file_name,
        mime_type=uri_mime_type or mime_type,
        request_id=request_id,
        namespace=namespace,
        path_hint=path_hint,
        kind=kind,
        caption=caption,
    )


def should_extract_base64(key: str | None, value: str, *, parent_has_metadata: bool = False) -> bool:
    key_name = (key or "").strip().lower()
    if key_name in BASE64_FIELD_NAMES:
        return True
    if key_name == "data" and parent_has_metadata:
        return BASE64_DATA_URI_RE.match(value.strip()) is not None or looks_like_base64_blob(value)
    return looks_like_base64_blob(value)


def extract_payload_media(cfg: Config, payload: Any, request_id: str, *, namespace: str) -> tuple[Any, list[PersistedMedia]]:
    found: list[PersistedMedia] = []

    def metadata(obj: dict[str, Any]) -> tuple[str | None, str | None, str, str]:
        file_name = None
        for key in ("file_name", "filename", "original_filename", "original_file_name"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                file_name = value.strip()
                break
        mime_type = None
        for key in ("mime_type", "mimetype", "content_type", "content_type_hint"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                mime_type = value.strip()
                break
        kind = str(obj.get("type") or ("image" if (mime_type or "").startswith("image/") else "file")).strip() or "file"
        caption = str(obj.get("caption") or "").strip()
        return file_name, mime_type, kind, caption

    def walk(value: Any, key: str | None = None, path: tuple[str, ...] = (), parent: dict[str, Any] | None = None) -> Any:
        parent_has_metadata = False
        file_name = mime_type = caption = None
        kind = "file"
        if isinstance(parent, dict):
            file_name, mime_type, kind, caption = metadata(parent)
            parent_has_metadata = bool(file_name or mime_type)

        if isinstance(value, str) and should_extract_base64(key, value, parent_has_metadata=parent_has_metadata):
            key_name = (key or "").strip().lower()
            inferred_kind = kind
            inferred_mime = mime_type
            if not parent_has_metadata and any(marker in key_name for marker in ("image", "cover", "png", "jpg", "jpeg", "webp", "gif")):
                inferred_kind = "image"
                if "png" in key_name:
                    inferred_mime = inferred_mime or "image/png"
                elif "jpg" in key_name or "jpeg" in key_name:
                    inferred_mime = inferred_mime or "image/jpeg"
                elif "webp" in key_name:
                    inferred_mime = inferred_mime or "image/webp"
                elif "gif" in key_name:
                    inferred_mime = inferred_mime or "image/gif"
            saved = save_base64_media(
                cfg,
                value,
                file_name=file_name,
                mime_type=inferred_mime,
                request_id=request_id,
                namespace=namespace,
                path_hint=".".join(path) or key or "payload",
                kind=inferred_kind,
                caption=caption or "",
            )
            if saved is None:
                return {"base64_omitted": True, "saved": False, "error": "base64_decode_failed"}
            found.append(saved)
            return saved.payload_summary()

        if isinstance(value, dict):
            return {str(k): walk(v, str(k), path + (str(k),), value) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(item, key, path + (str(index),), None) for index, item in enumerate(value)]
        return value

    return walk(payload), found
