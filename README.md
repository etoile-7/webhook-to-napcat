# webhook-to-napcat

中文说明 | [English](./README_en.md)

通过 NapCat（OneBot v11）接收 HTTP Webhook 并转发到 QQ。

这是一个很轻量的 Python 小项目，适合把“服务器事件 / CI 通知 / 应用回调 / 自定义 Webhook”快速转成 QQ 消息。

## 功能特性

- 监听可配置的 host / port / path
- 支持接收 `application/json`、`application/x-www-form-urlencoded`、`text/plain`
- 支持转发到 QQ 私聊或群聊
- 支持可选共享密钥校验
- 自动按 QQ 聊天习惯拆分长消息
- NapCat 请求支持重试和指数退避
- 纯 Python 标准库实现，无运行时第三方依赖

## 安装方式

### 方式 1：直接本地运行

```bash
git clone https://github.com/etoile-7/webhook-to-napcat.git
cd webhook-to-napcat
python3 -m webhook_to_napcat --help
```

### 方式 2：可编辑安装

```bash
python3 -m pip install -e .
webhook-to-napcat --help
```

## 快速开始

### 转发到 QQ 私聊

```bash
python3 -m webhook_to_napcat \
  --listen-port 8787 \
  --path /webhook \
  --napcat-base-url http://127.0.0.1:3001 \
  --private YOUR_QQ_NUMBER
```

### 转发到 QQ 群聊

```bash
python3 -m webhook_to_napcat \
  --listen-port 8787 \
  --path /webhook \
  --napcat-base-url http://127.0.0.1:3001 \
  --group 123456789
```

### 测试 webhook

```bash
curl -X POST 'http://127.0.0.1:8787/webhook' \
  -H 'Content-Type: application/json' \
  -d '{"event":"deploy","status":"ok","repo":"demo"}'
```

如果转发成功，目标 QQ 会收到一条整理后的消息。

## 密钥校验

你可以给 webhook 设置共享密钥，支持两种传递方式：

- Query 参数：`?secret=YOUR_SECRET`
- 请求头：`X-Webhook-Secret: YOUR_SECRET`

示例：

```bash
python3 -m webhook_to_napcat \
  --listen-port 8787 \
  --path /webhook \
  --secret my_shared_secret \
  --napcat-base-url http://127.0.0.1:3001 \
  --private YOUR_QQ_NUMBER
```

测试：

```bash
curl -X POST 'http://127.0.0.1:8787/webhook?secret=my_shared_secret' \
  -H 'Content-Type: application/json' \
  -d '{"event":"deploy","status":"ok"}'
```

## 配置说明

这些参数都可以通过 CLI 传入，大多数也支持环境变量。

| CLI 参数 | 环境变量 | 说明 |
|---|---|---|
| `--listen-host` | `LISTEN_HOST` | 监听地址，默认 `0.0.0.0` |
| `--listen-port` | `LISTEN_PORT` | 监听端口，默认 `8787` |
| `--path` | `WEBHOOK_PATH` | Webhook 路径，默认 `/webhook` |
| `--secret` | `WEBHOOK_SECRET` | 可选共享密钥 |
| `--napcat-base-url` | `NAPCAT_BASE_URL` | NapCat HTTP 地址，默认 `http://127.0.0.1:3001` |
| `--napcat-token` | `NAPCAT_TOKEN` | 可选 NapCat 访问令牌 |
| `--napcat-token-mode` | `NAPCAT_TOKEN_MODE` | `header` 或 `query` |
| `--private` | `NAPCAT_PRIVATE_QQ` | 目标 QQ 私聊用户 ID |
| `--group` | `NAPCAT_GROUP_QQ` | 目标 QQ 群号 |
| `--timeout` | `NAPCAT_TIMEOUT` | 单次请求超时时间 |
| `--retries` | `NAPCAT_RETRIES` | 重试次数 |
| `--chunk-size` | `QQ_CHUNK_SIZE` | QQ 单条消息长度上限 |
| `--title-prefix` | `TITLE_PREFIX` | 转发消息标题前缀 |

## 环境变量示例

参见：`.env.example`

## Docker 部署

### 构建镜像

```bash
docker build -t webhook-to-napcat:latest .
```

### 直接运行容器

私聊示例：

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

群聊示例：

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

如果你在 Linux 上 Docker 里无法解析 `host.docker.internal`，可以：

- 改成宿主机实际 IP
- 或加上 `--add-host=host.docker.internal:host-gateway`

示例：

```bash
docker run -d \
  --name webhook-to-napcat \
  --add-host=host.docker.internal:host-gateway \
  -p 8787:8787 \
  -e NAPCAT_BASE_URL=http://host.docker.internal:3001 \
  -e NAPCAT_PRIVATE_QQ=YOUR_QQ_NUMBER \
  webhook-to-napcat:latest
```

健康检查：

```bash
curl http://127.0.0.1:8787/health
```

### Docker Compose

项目内已附带 `docker-compose.yml`。

1. 复制环境变量文件：

```bash
cp .env.example .env
```

2. 编辑 `.env`

至少设置一个目标：

```env
NAPCAT_PRIVATE_QQ=YOUR_QQ_NUMBER
# 或
# NAPCAT_GROUP_QQ=123456789
```

3. 启动：

```bash
docker compose up -d --build
```

### 一键部署

项目内自带部署脚本：

```bash
./deploy.sh
```

它会自动：

- 如果 `.env` 不存在，就从 `.env.example` 复制一份
- 构建镜像
- 用 Docker Compose 启动服务
- 输出当前运行状态

## 反向代理示例

### Nginx

配置文件示例：`examples/nginx/webhook-to-napcat.conf`

常见用法：

```bash
sudo cp examples/nginx/webhook-to-napcat.conf /etc/nginx/sites-available/webhook-to-napcat
sudo ln -s /etc/nginx/sites-available/webhook-to-napcat /etc/nginx/sites-enabled/webhook-to-napcat
sudo nginx -t && sudo systemctl reload nginx
```

### Caddy

配置文件示例：`examples/caddy/Caddyfile`

常见用法：

```bash
sudo cp examples/caddy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## GitHub Actions

项目内包含：`.github/workflows/docker-image.yml`

默认功能：

- push / PR 时自动构建 Docker 镜像
- 非 PR 构建时自动推送到 GHCR
- 自动生成 `latest`、分支名、tag 版本号、commit SHA 等标签

默认镜像名：

```text
ghcr.io/etoile-7/webhook-to-napcat
```

如果你需要，可以自行修改 workflow 里的镜像路径。

## systemd 服务示例

参见：`examples/systemd/webhook-to-napcat.service`

## 项目结构

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
