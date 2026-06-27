from __future__ import annotations

import tempfile

from webhook_to_napcat.config import Config


def make_config(
    *,
    listen_host: str = "127.0.0.1",
    listen_port: int = 8787,
    path: str = "/webhook",
    secret: str = "",
    napcat_base_url: str = "http://127.0.0.1:3001",
    napcat_token: str = "",
    napcat_token_mode: str = "header",
    private: int | None = 1,
    group: int | None = None,
    timeout: float = 1.0,
    retries: int = 0,
    chunk_size: int = 280,
    log_dir: str = "",
    media_dir: str | None = None,
    public_media_dir: str | None = None,
    outbound_text_max_chars: int = 5000,
    aggregate_window_ms: int = 3000,
    notify_debounce_ms: int = 15000,
    live_session_segment_ttl_ms: int = 18 * 60 * 60 * 1000,
    post_end_start_confirm_ms: int = 10000,
    internal_dedupe_ttl_seconds: int = 86400,
    bililive_xml_base_dir: str = "",
    bililive_xml_strip_prefixes: tuple[str, ...] = (),
    bililive_gift_price_table: str = "",
    bililive_targets: dict[str, tuple[str | dict[str, int], ...]] | None = None,
) -> Config:
    media_root = media_dir if media_dir is not None else tempfile.gettempdir()
    public_root = public_media_dir if public_media_dir is not None else media_root
    return Config(
        listen_host=listen_host,
        listen_port=listen_port,
        path=path,
        secret=secret,
        napcat_base_url=napcat_base_url,
        napcat_token=napcat_token,
        napcat_token_mode=napcat_token_mode,
        private=private,
        group=group,
        timeout=timeout,
        retries=retries,
        chunk_size=chunk_size,
        log_dir=log_dir,
        media_dir=media_root,
        public_media_dir=public_root,
        outbound_text_max_chars=outbound_text_max_chars,
        aggregate_window_ms=aggregate_window_ms,
        notify_debounce_ms=notify_debounce_ms,
        live_session_segment_ttl_ms=live_session_segment_ttl_ms,
        post_end_start_confirm_ms=post_end_start_confirm_ms,
        internal_dedupe_ttl_seconds=internal_dedupe_ttl_seconds,
        bililive_xml_base_dir=bililive_xml_base_dir,
        bililive_xml_strip_prefixes=bililive_xml_strip_prefixes,
        bililive_gift_price_table=bililive_gift_price_table,
        bililive_targets=bililive_targets or {},
    )
