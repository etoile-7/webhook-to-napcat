# webhook-to-napcat

中文说明 | [English](./README_en.md)

通过 NapCat（OneBot v11）接收 HTTP Webhook 并转发到 QQ。

一个开箱即用的小项目：把服务器事件、CI 通知、应用回调或自定义 Webhook，快速变成 QQ 消息。

## 功能特性

- 监听可配置的 host / port / path
- 支持接收 `application/json`、`application/x-www-form-urlencoded`、`text/plain`
- 支持转发到 QQ 私聊或群聊
- 支持可选共享密钥校验
- 自动按 QQ 聊天习惯拆分长消息
- NapCat 请求支持重试和指数退避
- 提供 GHCR 镜像，可直接拉取部署

## 部署方式

### Docker Compose（推荐）

项目内已附带 `docker-compose.yml`，默认拉取 GHCR 镜像，不需要本地构建。

你只需要直接编辑 `docker-compose.yml` 里的 `environment`：

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
  WEBHOOK_LOG_DIR: "/logs"
```

```yaml
volumes:
  - "./rules.json:/app/rules.json:ro"
  - "/opt/WebhookToNapcat/logs:/logs"
```

至少改这两项：

- `NAPCAT_BASE_URL`
- `NAPCAT_PRIVATE_QQ` 或 `NAPCAT_GROUP_QQ`

然后启动：

```bash
docker compose pull
docker compose up -d
```

查看状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f
```

消息日志默认会额外落盘到宿主机：

```text
/opt/WebhookToNapcat/logs
```

容器内对应目录是：

```text
/logs
```

日志按分层写入：

- `requests-YYYY-MM.jsonl`：所有入站 webhook 请求（包括 path 不匹配、secret 校验失败）
- `messages-YYYY-MM.jsonl`：已接受并准备转发 / 已转发到 NapCat 的消息记录
- `errors-YYYY-MM.jsonl`：鉴权失败、路由失败、NapCat 转发失败等错误记录

### Docker run

如果你不想改 compose，也可以直接一条命令启动：

```bash
docker run -d \
  --name webhook-to-napcat \
  --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  -p 8787:8787 \
  -v /opt/WebhookToNapcat/logs:/logs \
  -e LISTEN_HOST=0.0.0.0 \
  -e LISTEN_PORT=8787 \
  -e WEBHOOK_PATH=/webhook \
  -e WEBHOOK_LOG_DIR=/logs \
  -e NAPCAT_BASE_URL=http://host.docker.internal:3001 \
  -e NAPCAT_PRIVATE_QQ=YOUR_QQ_NUMBER \
  ghcr.io/etoile-7/webhook-to-napcat:latest
```

如果发群，把：

- `NAPCAT_PRIVATE_QQ=...`

换成：

- `NAPCAT_GROUP_QQ=你的群号`

## 配置说明

配置主要写在 `docker-compose.yml` 的 `environment` 里，或者通过 `docker run -e ...` 直接传入。

| 环境变量 | 说明 |
|---|---|
| `LISTEN_HOST` | 监听地址，默认 `0.0.0.0` |
| `LISTEN_PORT` | 监听端口，默认 `8787` |
| `WEBHOOK_PATH` | Webhook 路径，默认 `/webhook` |
| `WEBHOOK_SECRET` | 可选共享密钥 |
| `NAPCAT_BASE_URL` | NapCat HTTP 地址，默认 `http://host.docker.internal:3001` |
| `NAPCAT_TOKEN` | 可选 NapCat 访问令牌 |
| `NAPCAT_TOKEN_MODE` | `header` 或 `query` |
| `NAPCAT_PRIVATE_QQ` | 目标 QQ 私聊用户 ID |
| `NAPCAT_GROUP_QQ` | 目标 QQ 群号 |
| `NAPCAT_TIMEOUT` | 单次请求超时时间 |
| `NAPCAT_RETRIES` | 重试次数 |
| `WEBHOOK_RULES_PATH` | 规则文件路径，默认 `/app/rules.json` |
| `WEBHOOK_LOG_DIR` | 消息日志目录，默认 `/logs`，按月分层写入 `requests-YYYY-MM.jsonl` / `messages-YYYY-MM.jsonl` / `errors-YYYY-MM.jsonl` |
| `WEBHOOK_AGGREGATE_WINDOW_MS` | BililiveRecorder 事件聚合窗口（毫秒），默认 `3000` |
| `WEBHOOK_NOTIFY_FILE_OPENING` | 是否单独发送 `FileOpening` 事件，默认 `0`（只参与聚合不单发） |
| `QQ_CHUNK_SIZE` | QQ 单条消息长度上限 |
| `TITLE_PREFIX` | 转发消息标题前缀 |
| `INCLUDE_HEADERS` | 是否附带部分请求头信息 |

## 连接 NapCat

这个项目通过 NapCat 的 OneBot v11 HTTP API 发消息给 QQ，连接方式是 HTTP，不是 WebSocket。

最关键的配置是：

```yaml
NAPCAT_BASE_URL: "http://host.docker.internal:3001"
NAPCAT_TOKEN: ""
NAPCAT_TOKEN_MODE: "header"
```

### 这 3 个字段分别是什么

- `NAPCAT_BASE_URL`：NapCat HTTP API 地址
- `NAPCAT_TOKEN`：如果你的 NapCat 开了鉴权，就填这里；没开可留空
- `NAPCAT_TOKEN_MODE`：鉴权传递方式，支持 `header` 或 `query`

### 常见连接场景

#### 场景 1：NapCat 跑在宿主机，当前项目跑在 Docker 容器里

推荐：

```yaml
NAPCAT_BASE_URL: "http://host.docker.internal:3001"
```

项目的 compose 已经带了：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

所以大多数 Linux Docker 环境下都能直接访问宿主机上的 NapCat。

#### 场景 2：NapCat 不在 3001 端口

把端口改成你实际的 HTTP API 端口：

```yaml
NAPCAT_BASE_URL: "http://host.docker.internal:实际端口"
```

#### 场景 3：`host.docker.internal` 不可用

可以直接改成宿主机实际 IP，例如：

```yaml
NAPCAT_BASE_URL: "http://192.168.1.77:3001"
```

### 如果 NapCat 开了 token

```yaml
NAPCAT_TOKEN: "your_token_here"
NAPCAT_TOKEN_MODE: "header"
```

如果你的 NapCat 要求 query 方式传 token，则改成：

```yaml
NAPCAT_TOKEN_MODE: "query"
```

### NapCat 连接是否正常，怎么判断

启动后先看容器日志：

```bash
docker compose logs -f
```

然后发一个测试 webhook：

```bash
curl -X POST 'http://127.0.0.1:8787/webhook' \
  -H 'Content-Type: application/json' \
  -d '{"event":"test","status":"ok"}'
```

如果配置正确，目标 QQ 会收到消息；如果 NapCat 地址或 token 错了，日志里通常会看到连接失败、401、403 或超时。

## Docker 说明

当前项目的 `docker-compose.yml` 默认使用远程镜像：

```yaml
image: ghcr.io/etoile-7/webhook-to-napcat:latest
```

所以普通部署时，不需要先执行 `docker build`。

如果你在 Linux 上 Docker 里无法解析 `host.docker.internal`，可以：

- 改成宿主机实际 IP
- 或保留 compose 里的：`host.docker.internal:host-gateway`

## 聚合规则（模板驱动）

除了普通 `rules` 外，`rules.json` 还可以定义 `aggregate`，把“是否聚合 / 聚合键 / 聚合窗口 / suppress 哪些事件 / 命中哪条输出模板”都放进模板配置里。

一个最小示例：

```json
{
  "aggregate": {
    "enabled": true,
    "window_ms": 3000,
    "groups": [
      {
        "name": "bililive_start",
        "phase": "start",
        "match": {
          "field_in": {
            "EventType": ["StreamStarted", "SessionStarted", "FileOpening"]
          }
        },
        "key_fields": ["EventData.RoomId", "EventData.Name"],
        "event_order": ["StreamStarted", "SessionStarted", "FileOpening"],
        "suppress_event_types": ["FileOpening"],
        "outputs": [
          {
            "match": { "event_types_all": ["StreamStarted", "SessionStarted"] },
            "output": {
              "type": "template",
              "template": "🟢［{name}］开播啦！\\n标题：{title}\\n分区：{area}\\n房间：{room_id}\\n时间：{time}"
            }
          }
        ]
      }
    ]
  }
}
```

目前聚合模板可用的上下文字段包括：

- `name`
- `title`
- `room_id`
- `short_id`
- `session_id`
- `area_parent`
- `area_child`
- `area`
- `file_path`
- `file_name`
- `duration`
- `file_size`
- `time`
- `phase`
- `group_name`
- `event_types`
- `request_count`
- `event_count`

聚合组里常用字段：

- `match`：哪些 webhook 进入这个聚合组（支持 `field_equals` / `field_in` / `has_keys` / `field_exists`）
- `key_fields`：按哪些字段决定“是不是同一组消息”
- `window_ms`：这个组自己的聚合窗口
- `event_order`：聚合上下文取值时的事件优先级
- `suppress_event_types`：只参与聚合但不单独体现为通知的事件
- `outputs`：按已收集到的 `event_types_*` 规则选择最终模板

`outputs[*].match` 目前支持：

- `event_types_all`
- `event_types_any`
- `event_types_none`
- `field_equals`
- `field_in`

这样以后改“开播啦 / 下播啦 / 时间放哪 / 哪几类事件合并 / 哪些事件 suppress”，都主要改本地 `rules.json`，不用再改 Python 源码。

## 测试 webhook

```bash
curl -X POST 'http://127.0.0.1:8787/webhook' \
  -H 'Content-Type: application/json' \
  -d '{"event":"deploy","status":"ok","repo":"demo"}'
```

如果转发成功，目标 QQ 会收到一条整理后的消息。

补充：如果 webhook 的 JSON payload 里包含 `content` 字段（例如短信/验证码转发场景），程序会优先只发送 `content` 的内容，不再附带路径、UA 和完整 payload。

## 密钥校验

你可以给 webhook 设置共享密钥，支持两种传递方式：

- Query 参数：`?secret=YOUR_SECRET`
- 请求头：`X-Webhook-Secret: YOUR_SECRET`

测试：

```bash
curl -X POST 'http://127.0.0.1:8787/webhook?secret=my_shared_secret' \
  -H 'X-Webhook-Secret: my_shared_secret' \
  -H 'Content-Type: application/json' \
  -d '{"event":"deploy","status":"ok"}'
```

## 反向代理示例

### Nginx

配置文件示例：`examples/nginx/webhook-to-napcat.conf`

```bash
sudo cp examples/nginx/webhook-to-napcat.conf /etc/nginx/sites-available/webhook-to-napcat
sudo ln -s /etc/nginx/sites-available/webhook-to-napcat /etc/nginx/sites-enabled/webhook-to-napcat
sudo nginx -t && sudo systemctl reload nginx
```

### Caddy

配置文件示例：`examples/caddy/Caddyfile`

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

## 开发者说明

如果你是在修改代码、调试 Dockerfile 或想自己本地构建开发镜像，可以手动执行：

```bash
docker build -t webhook-to-napcat:dev .
```

但这不是普通部署的默认方式。

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
tc/nginx/sites-available/webhook-to-napcat
sudo ln -s /etc/nginx/sites-available/webhook-to-napcat /etc/nginx/sites-enabled/webhook-to-napcat
sudo nginx -t && sudo systemctl reload nginx
```

### Caddy

配置文件示例：`examples/caddy/Caddyfile`

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

## 开发者说明

如果你是在修改代码、调试 Dockerfile 或想自己本地构建开发镜像，可以手动执行：

```bash
docker build -t webhook-to-napcat:dev .
```

但这不是普通部署的默认方式。

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
