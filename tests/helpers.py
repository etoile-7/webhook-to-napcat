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
    )
