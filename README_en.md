# webhook-to-napcat

Receive webhook HTTP requests and forward them to QQ through NapCat (OneBot v11).

A tiny Python project for people who want "something happened on my server / CI / app" → "send it to QQ" with almost zero dependencies.

## Features

- Listen on a configurable host / port / path
- Accept `application/json`, `application/x-www-form-urlencoded`, and `text/plain`
- Forward to QQ private chat or group via NapCat HTTP API
- Optional shared-secret verification
- QQ-friendly long-message splitting
- Retry with exponential backoff on transient NapCat failures
- Pure Python stdlib, no third-party runtime dependencies

## Install

### Option 1: local run

```bash
git clone <your-repo-url>
cd webhook-to-napcat
python3 -m webhook_to_napcat --help
```

### Option 2: editable install

```bash
python3 -m pip install -e .
webhook-to-napcat --help
```

## Quick start

Forward webhook messages to a QQ private chat:

```bash
python3 -m webhook_to_napcat \
  --listen-port 8787 \
  --path /webhook \
  --napcat-base-url http://127.0.0.1:3001 \
  --private YOUR_QQ_NUMBER
```

Forward to a QQ group:

```bash
python3 -m webhook_to_napcat \
  --listen-port 8787 \
  --path /webhook \
  --napcat-base-url http://127.0.0.1:3001 \
  --group 123456789
```

Test it:

```bash
curl -X POST 'http://127.0.0.1:8787/webhook' \
  -H 'Content-Type: application/json' \
  -d '{"event":"deploy","status":"ok","repo":"demo"}'
```

If successful, the QQ target will receive a formatted message.

## Secret verification

You can set a shared secret and pass it either by query string or header:

- Query: `?secret=YOUR_SECRET`
- Header: `X-Webhook-Secret: YOUR_SECRET`

Example:

```bash
python3 -m webhook_to_napcat \
  --listen-port 8787 \
  --path /webhook \
  --secret my_shared_secret \
  --napcat-base-url http://127.0.0.1:3001 \
  --private YOUR_QQ_NUMBER
```

Test:

```bash
curl -X POST 'http://127.0.0.1:8787/webhook?secret=my_shared_secret' \
  -H 'Content-Type: application/json' \
  -d '{"event":"deploy","status":"ok"}'
```

## Configuration

All of these can be passed as CLI flags. Most also support environment variables.

| CLI flag | Env var | Description |
|---|---|---|
| `--listen-host` | `LISTEN_HOST` | Bind address, default `0.0.0.0` |
| `--listen-port` | `LISTEN_PORT` | Listen port, default `8787` |
| `--path` | `WEBHOOK_PATH` | Webhook path, default `/webhook` |
| `--secret` | `WEBHOOK_SECRET` | Optional shared secret |
| `--napcat-base-url` | `NAPCAT_BASE_URL` | NapCat HTTP base URL, default `http://127.0.0.1:3001` |
| `--napcat-token` | `NAPCAT_TOKEN` | Optional NapCat access token |
| `--napcat-token-mode` | `NAPCAT_TOKEN_MODE` | `header` or `query` |
| `--private` | `NAPCAT_PRIVATE_QQ` | Target QQ private chat user ID |
| `--group` | `NAPCAT_GROUP_QQ` | Target QQ group ID |
| `--timeout` | `NAPCAT_TIMEOUT` | Per-request timeout |
| `--retries` | `NAPCAT_RETRIES` | Retry count |
| `--chunk-size` | `QQ_CHUNK_SIZE` | QQ single-message chunk size |
| `--title-prefix` | `TITLE_PREFIX` | Title prefix in forwarded messages |

## Environment example

See `.env.example`.

## Docker

### Build image

```bash
docker build -t webhook-to-napcat:latest .
```

### Run container

Private chat example:

```bash
docker run -d \
  --name webhook-to-napcat \
  -p 8787:8787 \
  -e LISTEN_HOST=0.0.0.0 \
  -e LISTEN_PORT=8787 \
  -e WEBHOOK_PATH=/webhook \
  -e NAPCAT_BASE_URL=http://host.docker.internal:3001 \
  -e NAPCAT_PRIVATE_QQ=YOUR_QQ_NUMBER \
  webhook-to-napcat:latest
```

Group example:

```bash
docker run -d \
  --name webhook-to-napcat \
  -p 8787:8787 \
  -e LISTEN_HOST=0.0.0.0 \
  -e LISTEN_PORT=8787 \
  -e WEBHOOK_PATH=/webhook \
  -e NAPCAT_BASE_URL=http://host.docker.internal:3001 \
  -e NAPCAT_GROUP_QQ=123456789 \
  webhook-to-napcat:latest
```

If your Docker environment cannot resolve `host.docker.internal` on Linux, replace it with:
- the host LAN IP, or
- `--add-host=host.docker.internal:host-gateway`

Example:

```bash
docker run -d \
  --name webhook-to-napcat \
  --add-host=host.docker.internal:host-gateway \
  -p 8787:8787 \
  -e NAPCAT_BASE_URL=http://host.docker.internal:3001 \
  -e NAPCAT_PRIVATE_QQ=YOUR_QQ_NUMBER \
  webhook-to-napcat:latest
```

Health check test:

```bash
curl http://127.0.0.1:8787/health
```

### Docker Compose

This project also ships with `docker-compose.yml`.

1. Copy the example environment file:

```bash
cp .env.example .env
```

2. Edit `.env`

At minimum, set one target:

```env
NAPCAT_PRIVATE_QQ=YOUR_QQ_NUMBER
# or
# NAPCAT_GROUP_QQ=123456789
```

3. Start it:

```bash
docker compose up -d --build
```

### One-command deploy

A helper script is included:

```bash
./deploy.sh
```

It will:
- create `.env` from `.env.example` if missing
- build the image
- start the service with Docker Compose
- print current status

## Reverse proxy examples

### Nginx

See `examples/nginx/webhook-to-napcat.conf`.

Typical usage:

```bash
sudo cp examples/nginx/webhook-to-napcat.conf /etc/nginx/sites-available/webhook-to-napcat
sudo ln -s /etc/nginx/sites-available/webhook-to-napcat /etc/nginx/sites-enabled/webhook-to-napcat
sudo nginx -t && sudo systemctl reload nginx
```

### Caddy

See `examples/caddy/Caddyfile`.

Typical usage:

```bash
sudo cp examples/caddy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## GitHub Actions

This project includes `.github/workflows/docker-image.yml`.

What it does:
- builds the Docker image on push / PR
- pushes images to GHCR on non-PR builds
- generates tags like `latest`, branch name, tag version, and commit SHA

Expected image name:

```text
ghcr.io/<your-github-user-or-org>/webhook-to-napcat
```

If needed, change the image path in the workflow file.

## Example systemd service

See `examples/systemd/webhook-to-napcat.service`.

## Project layout

```text
webhook-to-napcat/
├── .github/
│   └── workflows/
├── examples/
│   ├── caddy/
│   ├── nginx/
│   └── systemd/
├── scripts/
│   └── webhook_to_napcat.py
├── webhook_to_napcat/
│   ├── __init__.py
│   ├── __main__.py
│   └── server.py
├── .dockerignore
├── .env.example
├── .gitignore
├── deploy.sh
├── docker-compose.yml
├── Dockerfile
├── LICENSE
├── README.md
└── pyproject.toml
```

## License

MIT
