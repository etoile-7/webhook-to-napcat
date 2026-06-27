from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Config
from .utils import split_text_for_qq


@dataclass(frozen=True)
class NapCatTarget:
    kind: str
    id: int

    def to_log(self) -> dict[str, int]:
        return {self.kind: self.id}


@dataclass
class DeliveryReport:
    results: list[dict[str, Any]]
    chunks: list[str]

    @property
    def attempted(self) -> int:
        return len(self.results)

    @property
    def successful(self) -> int:
        return sum(1 for result in self.results if result.get("ok") is True)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.get("ok") is False)

    @property
    def all_failed(self) -> bool:
        return self.attempted > 0 and self.successful == 0

    def to_log(self) -> dict[str, Any]:
        return {
            "chunks": self.chunks,
            "chunks_count": len(self.chunks),
            "delivery_count": self.attempted,
            "success_count": self.successful,
            "failure_count": self.failed,
            "napcat": self.results,
        }


def default_targets(cfg: Config) -> list[NapCatTarget]:
    targets: list[NapCatTarget] = []
    if cfg.private is not None:
        targets.append(NapCatTarget("private", int(cfg.private)))
    if cfg.group is not None:
        targets.append(NapCatTarget("group", int(cfg.group)))
    return dedupe_targets(targets)


def dedupe_targets(targets: list[NapCatTarget]) -> list[NapCatTarget]:
    seen: set[tuple[str, int]] = set()
    result: list[NapCatTarget] = []
    for target in targets:
        key = (target.kind, int(target.id))
        if key in seen:
            continue
        seen.add(key)
        result.append(target)
    return result


def parse_internal_targets(raw_targets: Any) -> tuple[list[NapCatTarget], list[dict[str, Any]]]:
    if not isinstance(raw_targets, list):
        return [], [{"target": raw_targets, "reason": "targets_not_array"}]

    targets: list[NapCatTarget] = []
    ignored: list[dict[str, Any]] = []
    for item in raw_targets:
        if not isinstance(item, dict):
            ignored.append({"target": item, "reason": "target_not_object"})
            continue
        target_type = str(item.get("type") or "").strip().lower()
        target_id = str(item.get("id") or "").strip()
        if not target_id:
            ignored.append({"target": item, "reason": "empty_id"})
            continue
        try:
            numeric_id = int(target_id)
        except Exception:
            ignored.append({"target": item, "reason": "id_not_numeric"})
            continue
        if target_type == "user":
            targets.append(NapCatTarget("private", numeric_id))
        elif target_type == "group":
            targets.append(NapCatTarget("group", numeric_id))
        else:
            ignored.append({"target": item, "reason": "unknown_type"})
    return dedupe_targets(targets), ignored


def resolve_named_targets(cfg: Config, specs: tuple[str | dict[str, int], ...] | None) -> list[NapCatTarget]:
    if specs is None:
        return default_targets(cfg)

    targets: list[NapCatTarget] = []
    for spec in specs:
        if spec == "default":
            targets.extend(default_targets(cfg))
        elif isinstance(spec, dict) and "private" in spec:
            targets.append(NapCatTarget("private", int(spec["private"])))
        elif isinstance(spec, dict) and "group" in spec:
            targets.append(NapCatTarget("group", int(spec["group"])))
    return dedupe_targets(targets)


def napcat_response_ok(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    retcode = response.get("retcode")
    status = str(response.get("status") or "").lower()
    if retcode == 0:
        return True
    return status == "ok" and retcode in {None, "", 0}


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float, retries: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries + 1):
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
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(0.4 * (2**attempt))
    raise RuntimeError(f"POST failed after retries: {last}")


def apply_napcat_auth(cfg: Config, url: str) -> tuple[str, dict[str, str]]:
    headers: dict[str, str] = {}
    if not cfg.napcat_token:
        return url, headers
    if cfg.napcat_token_mode == "header":
        headers["Authorization"] = "Bearer " + cfg.napcat_token
        return url, headers
    sep = "&" if "?" in url else "?"
    return url + sep + "access_token=" + urllib.parse.quote(cfg.napcat_token), headers


def build_napcat_request(
    cfg: Config,
    target: NapCatTarget,
    *,
    private_endpoint: str = "/send_private_msg",
    group_endpoint: str = "/send_group_msg",
) -> tuple[str, dict[str, str], dict[str, int]]:
    base_url = cfg.napcat_base_url.rstrip("/")
    if target.kind == "private":
        endpoint = private_endpoint
        target_payload = {"user_id": int(target.id)}
    else:
        endpoint = group_endpoint
        target_payload = {"group_id": int(target.id)}

    url = base_url + endpoint
    url, headers = apply_napcat_auth(cfg, url)
    return url, headers, target_payload


def send_segments(cfg: Config, segments: list[dict[str, Any]], targets: list[NapCatTarget]) -> DeliveryReport:
    results: list[dict[str, Any]] = []
    for target in dedupe_targets(targets):
        try:
            url, headers, target_payload = build_napcat_request(cfg, target)
            payload = dict(target_payload)
            payload["message"] = json.loads(json.dumps(segments, ensure_ascii=False))
            response = post_json(url, payload, headers=headers, timeout=cfg.timeout, retries=cfg.retries)
            ok = napcat_response_ok(response)
            results.append({"target": target.to_log(), "ok": ok, "response": response})
        except Exception as exc:
            results.append({"target": target.to_log(), "ok": False, "error": str(exc)})
    return DeliveryReport(results=results, chunks=[])


def send_text(cfg: Config, text: str, targets: list[NapCatTarget]) -> DeliveryReport:
    chunks = split_text_for_qq(text, cfg.chunk_size, outbound_limit=cfg.outbound_text_max_chars)
    results: list[dict[str, Any]] = []
    for target in dedupe_targets(targets):
        for chunk in chunks:
            segment = [{"type": "text", "data": {"text": chunk}}]
            report = send_segments(cfg, segment, [target])
            results.extend(report.results)
    return DeliveryReport(results=results, chunks=chunks)


def send_file(cfg: Config, file_path: str, file_name: str, targets: list[NapCatTarget]) -> DeliveryReport:
    results: list[dict[str, Any]] = []
    for target in dedupe_targets(targets):
        try:
            url, headers, target_payload = build_napcat_request(
                cfg,
                target,
                private_endpoint="/upload_private_file",
                group_endpoint="/upload_group_file",
            )
            payload = dict(target_payload)
            payload["file"] = file_path
            payload["name"] = file_name
            response = post_json(url, payload, headers=headers, timeout=cfg.timeout, retries=cfg.retries)
            ok = napcat_response_ok(response)
            results.append({"target": target.to_log(), "ok": ok, "response": response, "file": file_path, "name": file_name})
        except Exception as exc:
            results.append({"target": target.to_log(), "ok": False, "error": str(exc), "file": file_path, "name": file_name})
    return DeliveryReport(results=results, chunks=[])


def image_segment(file_path: str) -> dict[str, Any]:
    return {"type": "image", "data": {"file": file_path}}
