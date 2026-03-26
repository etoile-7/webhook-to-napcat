# webhook-to-napcat

[中文说明](./README.md) | English

Receive HTTP webhooks and forward them to QQ through NapCat (OneBot v11).

A ready-to-run tiny project for turning server events, CI notifications, app callbacks, or custom webhooks into QQ messages.

## 30-second deploy

```bash
git clone https://github.com/etoile-7/webhook-to-napcat.git
cd webhook-to-napcat
cp .env.example .env
# edit .env and set NAPCAT_PRIVATE_QQ or NAPCAT_GROUP_QQ
docker compose pull
docker compose up -d
```

Health check:

```bash
curl http://127.0.0.1:8787/health
```

Default image:

```text
ghcr.io/etoile-7/webhook-to-napcat:latest
```

## Features

- Configurable host / port / path
- Accepts `application/json`, `application/x-www-form-urlencoded`, and `text/plain`
- Forwards to QQ private chat or group chat
- Optional shared-secret verification
- QQ-friendly long-message splitting
- Retry with exponential backoff for NapCat requests
- Prebuilt GHCR image for direct deployment

## Deployment

### Docker Compose (recommended)

The included `docker-compose.yml` pulls the GHCR image by default, so local image builds are not required.

1. Copy the environment file:

```bash
cp .env.example .env
```

2. Edit `.env`

Set at least one target:

```env
NAPCAT_PRIVATE_QQ=YOUR_QQ_NUMBER
# or
# NAPCAT_GROUP_QQ=123456789
```

3. Pull and start:

```bash
docker compose pull
docker compose up -d
```

Check status:

```bash
docker compose ps
```

Check logs:

```bash
docker compose logs -f
```

### One-command deploy

```bash
./deploy.sh
```

It will:

- create `.env` from `.env.example` if missing
- pull the latest image
- start the service with Docker Compose
- print current status

## Configuration

These values are mainly provided via `.env`, and can also be overridden through environment variables.

| Environment variable | Description |
|---|---|
| `LISTEN_HOST` | Bind address, default `0.0.0.0` |
| `LISTEN_PORT` | Listen port, default `8787` |
| `WEBHOOK_PATH` | Webhook path, default `/webhook` |
| `WEBHOOK_SECRET` | Optional shared secret |
| `NAPCAT_BASE_URL` | NapCat HTTP base URL, default `http://host.docker.internal:3001` |
| `NAPCAT_TOKEN` | Optional NapCat access token |
| `NAPCAT_TOKEN_MODE` | `header` or `query` |
| `NAPCAT_PRIVATE_QQ` | Target QQ private user ID |
| `NAPCAT_GROUP_QQ` | Target QQ group ID |
| `NAPCAT_TIMEOUT` | Per-request timeout |
| `NAPCAT_RETRIES` | Retry count |
| `QQ_CHUNK_SIZE` | QQ single-message chunk size |
| `TITLE_PREFIX` | Title prefix for forwarded messages |
| `INCLUDE_HEADERS` | Whether to include selected request header info |

See `.env.example` for an example.

## Docker notes

The default `docker-compose.yml` uses the remote image:

```yaml
image: ghcr.io/etoile-7/webhook-to-napcat:latest
```

So normal deployments do not need `docker build` first.

If `host.docker.internal` does not resolve in your Linux Docker environment, either:

- replace it with your host IP, or
- keep the compose mapping: `host.docker.internal:host-gateway`

## Test webhook

```bash
curl -X POST 'http://127.0.0.1:8787/webhook' \
  -H 'Content-Type: application/json' \
  -d '{"event":"deploy","status":"ok","repo":"demo"}'
```

If forwarding works, your QQ target should receive a formatted message.

## Secret verification

You can protect the webhook with a shared secret using either:

- query string: `?secret=YOUR_SECRET`
- request header: `X-Webhook-Secret: YOUR_SECRET`

Test example:

```bash
curl -X POST 'http://127.0.0.1:8787/webhook?secret=my_shared_secret' \
  -H 'X-Webhook-Secret: my_shared_secret' \
  -H 'Content-Type: application/json' \
  -d '{"event":"deploy","status":"ok"}'
```

## Reverse proxy examples

### Nginx

Example config: `examples/nginx/webhook-to-napcat.conf`

```bash
sudo cp examples/nginx/webhook-to-napcat.conf /etc/nginx/sites-available/webhook-to-napcat
sudo ln -s /etc/nginx/sites-available/webhook-to-napcat /etc/nginx/sites-enabled/webhook-to-napcat
sudo nginx -t && sudo systemctl reload nginx
```

### Caddy

Example config: `examples/caddy/Caddyfile`

```bash
sudo cp examples/caddy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## GitHub Actions

This project includes `.github/workflows/docker-image.yml`.

By default it:

- builds Docker images on push / PR
- pushes images to GHCR on non-PR runs
- generates tags such as `latest`, branch name, tag version, and commit SHA

Default image name:

```text
ghcr.io/etoile-7/webhook-to-napcat
```

## Developer note

If you are modifying code, debugging the Dockerfile, or want a local dev image, you can still build manually:

```bash
docker build -t webhook-to-napcat:dev .
```

But this is no longer the default deployment path.

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
├── README_en.md
└── pyproject.toml
```

## License

MIT
