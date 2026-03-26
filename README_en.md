# webhook-to-napcat

[中文说明](./README.md) | English

Receive HTTP webhooks and forward them to QQ through NapCat (OneBot v11).

A ready-to-run tiny project for turning server events, CI notifications, app callbacks, or custom webhooks into QQ messages.

## 30-second deploy

```bash
git clone https://github.com/etoile-7/webhook-to-napcat.git
cd webhook-to-napcat
# edit docker-compose.yml and set NAPCAT_BASE_URL plus your QQ target
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

Just edit the `environment` section in `docker-compose.yml`:

```yaml
environment:
  LISTEN_HOST: "0.0.0.0"
  LISTEN_PORT: "8787"
  WEBHOOK_PATH: "/webhook"
  WEBHOOK_SECRET: ""
  NAPCAT_BASE_URL: "http://host.docker.internal:3001"
  NAPCAT_TOKEN: ""
  NAPCAT_TOKEN_MODE: "header"
  NAPCAT_PRIVATE_QQ: "YOUR_QQ_NUMBER"
  NAPCAT_GROUP_QQ: ""
```

At minimum, change these:

- `NAPCAT_BASE_URL`
- `NAPCAT_PRIVATE_QQ` or `NAPCAT_GROUP_QQ`

Note: these target fields now work directly through the `docker-compose.yml` environment block; no extra CLI target arguments are required.

Then start it:

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

### Docker run

If you do not want to edit compose, you can run it directly with one command:

```bash
docker run -d \
  --name webhook-to-napcat \
  --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  -p 8787:8787 \
  -e LISTEN_HOST=0.0.0.0 \
  -e LISTEN_PORT=8787 \
  -e WEBHOOK_PATH=/webhook \
  -e NAPCAT_BASE_URL=http://host.docker.internal:3001 \
  -e NAPCAT_PRIVATE_QQ=YOUR_QQ_NUMBER \
  ghcr.io/etoile-7/webhook-to-napcat:latest
```

For group delivery, replace:

- `NAPCAT_PRIVATE_QQ=...`

with:

- `NAPCAT_GROUP_QQ=YOUR_GROUP_ID`

## Configuration

Configuration is mainly written directly in `docker-compose.yml` under `environment`, or passed with `docker run -e ...`.

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

## Connecting to NapCat

This project sends QQ messages through NapCat's OneBot v11 HTTP API. The connection method is HTTP, not WebSocket.

The key settings are:

```yaml
NAPCAT_BASE_URL: "http://host.docker.internal:3001"
NAPCAT_TOKEN: ""
NAPCAT_TOKEN_MODE: "header"
```

### What these fields mean

- `NAPCAT_BASE_URL`: the NapCat HTTP API endpoint
- `NAPCAT_TOKEN`: fill this if your NapCat instance requires authentication; otherwise leave it empty
- `NAPCAT_TOKEN_MODE`: authentication transport mode, either `header` or `query`

### Common connection scenarios

#### Scenario 1: NapCat runs on the host, this project runs in Docker

Recommended:

```yaml
NAPCAT_BASE_URL: "http://host.docker.internal:3001"
```

The compose file already includes:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

So in most Linux Docker environments, the container can reach NapCat on the host directly.

#### Scenario 2: NapCat does not use port 3001

Change it to the actual HTTP API port:

```yaml
NAPCAT_BASE_URL: "http://host.docker.internal:YOUR_PORT"
```

#### Scenario 3: `host.docker.internal` does not work

Use the real host IP instead, for example:

```yaml
NAPCAT_BASE_URL: "http://192.168.1.77:3001"
```

### If NapCat uses a token

```yaml
NAPCAT_TOKEN: "your_token_here"
NAPCAT_TOKEN_MODE: "header"
```

If your NapCat instance expects token auth via query string, change it to:

```yaml
NAPCAT_TOKEN_MODE: "query"
```

### How to verify the NapCat connection

First check container logs:

```bash
docker compose logs -f
```

Then send a test webhook:

```bash
curl -X POST 'http://127.0.0.1:8787/webhook' \
  -H 'Content-Type: application/json' \
  -d '{"event":"test","status":"ok"}'
```

If the setup is correct, your QQ target should receive a message. If the NapCat URL or token is wrong, logs will usually show connection failures, 401/403 errors, or timeouts.

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

Note: if the webhook JSON payload contains a `content` field (for example SMS / verification-code forwarding), the service will send only the `content` text and skip path, UA, and full payload details.

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
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── LICENSE
├── README.md
├── README_en.md
└── pyproject.toml
```

## License

MIT
