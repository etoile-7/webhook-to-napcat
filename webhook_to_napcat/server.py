#!/usr/bin/env python3
"""Listen for webhook POSTs and forward them to QQ via NapCat (OneBot v11 HTTP API)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


@dataclass
class Config:
    listen_host: str
    listen_port: int
    path: str
    secret: str
    napcat_base_url: str
    napcat_token: str
    napcat_token_mode: str
    private: int | None
    group: int | None
    timeout: float
    retries: int
    chunk_size: int
    title_prefix: str
    include_headers: bool
    rules_path: str


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float, retries: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = None
    for i in range(retries + 1):
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
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < retries:
                time.sleep(0.4 * (2**i))
    raise RuntimeError(f"POST failed after retries: {last}")


def build_napcat_request(cfg: Config) -> tuple[str, dict[str, str], str]:
    base_url = cfg.napcat_base_url.rstrip("/")
    endpoint = "/send_private_msg" if cfg.private is not None else "/send_group_msg"
    headers: dict[str, str] = {}
    token_q = ""

    if cfg.napcat_token:
        if cfg.napcat_token_mode == "header":
            headers["Authorization"] = "Bearer " + cfg.napcat_token
        else:
            token_q = ("&" if "?" in base_url else "?") + "access_token=" + urllib.parse.quote(cfg.napcat_token)

    return base_url + endpoint + token_q, headers, endpoint


def split_for_qq(text: str, max_len: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return ["(empty)"]
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if len(block) <= max_len:
            chunks.append(block)
            continue
        for line in block.splitlines():
            line = line.rstrip()
            if not line:
                continue
            if len(line) <= max_len:
                chunks.append(line)
                continue
            start = 0
            while start < len(line):
                chunks.append(line[start : start + max_len])
                start += max_len

    if not chunks:
        return [text[i : i + max_len] for i in range(0, len(text), max_len)]
    return chunks


def send_text_to_napcat(cfg: Config, text: str) -> list[dict[str, Any]]:
    url, headers, _ = build_napcat_request(cfg)
    target_payload = {"user_id": cfg.private} if cfg.private is not None else {"group_id": cfg.group}

    results = []
    for chunk in split_for_qq(text, cfg.chunk_size):
        payload = dict(target_payload)
        payload["message"] = [{"type": "text", "data": {"text": chunk}}]
        results.append(post_json(url, payload, headers=headers, timeout=cfg.timeout, retries=cfg.retries))
    return results


def maybe_parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def parse_body(content_type: str, raw: bytes) -> Any:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype == "application/json":
        return maybe_parse_json(raw)
    if ctype == "application/x-www-form-urlencoded":
        parsed = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {k: v if len(v) != 1 else v[0] for k, v in parsed.items()}
    if ctype == "text/plain":
        return raw.decode("utf-8", errors="replace")

    parsed_json = maybe_parse_json(raw)
    if parsed_json is not None:
        return parsed_json
    return raw.decode("utf-8", errors="replace")


def summarize_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        preferred_keys = [
            "title",
            "event",
            "type",
            "action",
            "status",
            "repo",
            "repository",
            "project",
            "message",
            "text",
            "content",
            "sender",
            "source",
        ]
        lines = []
        for key in preferred_keys:
            if key in payload:
                value = payload[key]
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                lines.append(f"{key}: {value}")
        if lines:
            lines.append("")
            lines.append("payload:")
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
            return "\n".join(lines)
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if isinstance(payload, list):
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return str(payload)


def load_rules(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        eprint(f"[rules] failed to load {path}: {exc}")
        return {}


def get_field_value(payload: Any, field: str) -> Any:
    current = payload
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def rule_matches(rule: dict[str, Any], handler: BaseHTTPRequestHandler, payload: Any) -> bool:
    match = rule.get("match")
    if not isinstance(match, dict):
        return True

    has_keys = match.get("has_keys")
    if has_keys is not None:
        if not isinstance(payload, dict):
            return False
        for key in has_keys:
            if key not in payload:
                return False

    path_contains = match.get("path_contains")
    if path_contains and path_contains not in handler.path:
        return False

    header_equals = match.get("header_equals")
    if isinstance(header_equals, dict):
        for key, expected in header_equals.items():
            if handler.headers.get(key, "") != str(expected):
                return False

    field_equals = match.get("field_equals")
    if isinstance(field_equals, dict):
        for key, expected in field_equals.items():
            if get_field_value(payload, key) != expected:
                return False

    return True


def render_rule_output(rule: dict[str, Any], payload: Any) -> str | None:
    output = rule.get("output")
    if not isinstance(output, dict):
        return None

    kind = output.get("type", "summary")
    if kind == "field":
        field = str(output.get("field", "")).strip()
        value = get_field_value(payload, field) if field else None
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None:
            return str(value)
        return None

    if kind == "template":
        template = str(output.get("template", "")).strip()
        if not template:
            return None
        if isinstance(payload, dict):
            flat = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in payload.items()}
            try:
                return template.format(**flat).strip()
            except Exception:  # noqa: BLE001
                return template
        return template

    if kind == "summary":
        return summarize_payload(payload).strip()

    return None


def apply_rules(cfg: Config, handler: BaseHTTPRequestHandler, payload: Any) -> str | None:
    rules_doc = load_rules(cfg.rules_path)
    rules = rules_doc.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if rule_matches(rule, handler, payload):
                rendered = render_rule_output(rule, payload)
                if rendered:
                    return rendered

    default_rule = rules_doc.get("default")
    if isinstance(default_rule, dict):
        rendered = render_rule_output({"output": default_rule}, payload)
        if rendered:
            return rendered

    return None


def build_forward_text(cfg: Config, handler: BaseHTTPRequestHandler, payload: Any) -> str:
    rules_text = apply_rules(cfg, handler, payload)
    if rules_text:
        return rules_text

    title = f"{cfg.title_prefix} 收到新 webhook"
    lines = [title]
    lines.append(f"路径: {handler.path}")
    lines.append(f"来源IP: {handler.client_address[0]}")
    if cfg.include_headers:
        event = handler.headers.get("X-GitHub-Event") or handler.headers.get("X-Gitlab-Event") or handler.headers.get("X-Event-Key")
        ua = handler.headers.get("User-Agent")
        if event:
            lines.append(f"事件: {event}")
        if ua:
            lines.append(f"UA: {ua}")
    lines.append("")
    lines.append(summarize_payload(payload))
    return "\n".join(lines).strip()


def verify_secret(cfg: Config, handler: BaseHTTPRequestHandler) -> bool:
    if not cfg.secret:
        return True
    query = urllib.parse.urlparse(handler.path).query
    params = urllib.parse.parse_qs(query)
    from_query = params.get("secret", [""])[0]
    from_header = handler.headers.get("X-Webhook-Secret", "")
    return cfg.secret in {from_query, from_header}


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "WebhookToNapCat/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        eprint("[http]", self.address_string(), "-", fmt % args)

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    def _send_json(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", self.cfg.path.rstrip("/") + "/health", "/health"}:
            self._send_json(
                200,
                {
                    "ok": True,
                    "listen": f"{self.cfg.listen_host}:{self.cfg.listen_port}",
                    "webhook_path": self.cfg.path,
                    "target": {"private": self.cfg.private, "group": self.cfg.group},
                },
            )
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.rstrip("/") != self.cfg.path.rstrip("/"):
            self._send_json(404, {"ok": False, "error": "path not matched"})
            return
        if not verify_secret(self.cfg, self):
            self._send_json(401, {"ok": False, "error": "invalid secret"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        payload = parse_body(self.headers.get("Content-Type", ""), raw)
        text = build_forward_text(self.cfg, self, payload)

        try:
            results = send_text_to_napcat(self.cfg, text)
            self._send_json(200, {"ok": True, "chunks": len(results), "napcat": results})
        except Exception as exc:  # noqa: BLE001
            eprint(f"[forward-error] {exc}")
            self._send_json(502, {"ok": False, "error": str(exc)})


def parse_args() -> Config:
    ap = argparse.ArgumentParser(description="Listen for webhook messages and forward them to QQ via NapCat")
    ap.add_argument("--listen-host", default=os.getenv("LISTEN_HOST", "0.0.0.0"))
    ap.add_argument("--listen-port", type=int, default=int(os.getenv("LISTEN_PORT", "8787")))
    ap.add_argument("--path", default=os.getenv("WEBHOOK_PATH", "/webhook"))
    ap.add_argument("--secret", default=os.getenv("WEBHOOK_SECRET", ""))

    ap.add_argument("--napcat-base-url", default=os.getenv("NAPCAT_BASE_URL", "http://127.0.0.1:3001"))
    ap.add_argument("--napcat-token", default=os.getenv("NAPCAT_TOKEN", ""))
    ap.add_argument("--napcat-token-mode", choices=["header", "query"], default=os.getenv("NAPCAT_TOKEN_MODE", "header"))

    private_env = os.getenv("NAPCAT_PRIVATE_QQ")
    group_env = os.getenv("NAPCAT_GROUP_QQ")
    ap.add_argument("--private", type=int, default=None, help="QQ 私聊 user_id；也可通过环境变量 NAPCAT_PRIVATE_QQ 提供")
    ap.add_argument("--group", type=int, default=None, help="QQ 群 group_id；也可通过环境变量 NAPCAT_GROUP_QQ 提供")

    ap.add_argument("--timeout", type=float, default=float(os.getenv("NAPCAT_TIMEOUT", "10")))
    ap.add_argument("--retries", type=int, default=int(os.getenv("NAPCAT_RETRIES", "5")))
    ap.add_argument("--chunk-size", type=int, default=int(os.getenv("QQ_CHUNK_SIZE", "280")))
    ap.add_argument("--title-prefix", default=os.getenv("TITLE_PREFIX", "📨"))
    ap.add_argument("--include-headers", action="store_true", default=os.getenv("INCLUDE_HEADERS", "1") not in {"0", "false", "False"})
    ap.add_argument("--rules-path", default=os.getenv("WEBHOOK_RULES_PATH", "/app/rules.json"), help="规则文件路径（JSON）；用于按配置决定不同 webhook 的转发内容")
    args = ap.parse_args()

    private_target = args.private if args.private is not None else (int(private_env) if private_env else None)
    group_target = args.group if args.group is not None else (int(group_env) if group_env else None)

    if private_target is not None and group_target is not None:
        ap.error("请只设置一个目标：--private / NAPCAT_PRIVATE_QQ 与 --group / NAPCAT_GROUP_QQ 二选一，不要同时填写")
    if private_target is None and group_target is None:
        ap.error("必须设置一个目标：请提供 --private 或 --group，或设置环境变量 NAPCAT_PRIVATE_QQ / NAPCAT_GROUP_QQ（私聊和群聊二选一）")

    path = args.path.strip() or "/webhook"
    if not path.startswith("/"):
        path = "/" + path

    return Config(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        path=path,
        secret=args.secret,
        napcat_base_url=args.napcat_base_url,
        napcat_token=args.napcat_token,
        napcat_token_mode=args.napcat_token_mode,
        private=private_target,
        group=group_target,
        timeout=args.timeout,
        retries=args.retries,
        chunk_size=max(50, args.chunk_size),
        title_prefix=args.title_prefix,
        include_headers=args.include_headers,
        rules_path=args.rules_path,
    )


def main() -> int:
    cfg = parse_args()
    server = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), WebhookHandler)
    server.cfg = cfg  # type: ignore[attr-defined]

    print(
        json.dumps(
            {
                "status": "listening",
                "listen": f"{cfg.listen_host}:{cfg.listen_port}",
                "path": cfg.path,
                "target": {"private": cfg.private, "group": cfg.group},
                "napcat_base_url": cfg.napcat_base_url,
                "rules_path": cfg.rules_path,
            },
            ensure_ascii=False,
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0
