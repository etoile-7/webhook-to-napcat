# webhook-to-napcat

把 HTTP Webhook 转发到 NapCat（OneBot v11）的小服务。

这版只保留三条链路：

- BililiveRecorder 通知：识别直播录制事件，聚合开播/下播通知。
- ito 内部通知：按 `通知系统Webhook规范.md` 转发 `summary` 和附件。
- 未知通知：默认原样转发；如果正文里有 base64，会先保存为附件，正文只保留附件摘要。

通用规则配置和文案配置已经删除。项目不再通过外部规则文件拼接消息，也不再保留普通通知的特殊匹配逻辑。

## 配置

常用环境变量：

| 环境变量 | 说明 |
|---|---|
| `LISTEN_HOST` | 监听地址，默认 `0.0.0.0` |
| `LISTEN_PORT` | 监听端口，默认 `8787` |
| `WEBHOOK_PATH` | Webhook 路径，默认 `/webhook` |
| `WEBHOOK_SECRET` | 可选共享密钥，可放在 `X-Webhook-Secret` 请求头或 `secret` 查询参数 |
| `NAPCAT_BASE_URL` | NapCat HTTP API 地址 |
| `NAPCAT_TOKEN` | 可选 NapCat 访问令牌 |
| `NAPCAT_TOKEN_MODE` | `header` 或 `query` |
| `NAPCAT_PRIVATE_QQ` | 默认 QQ 私聊目标 |
| `NAPCAT_GROUP_QQ` | 默认 QQ 群目标 |
| `NAPCAT_TIMEOUT` | NapCat 请求超时，默认 `10` |
| `NAPCAT_RETRIES` | NapCat 请求重试次数，默认 `5` |
| `QQ_CHUNK_SIZE` | QQ 文本拆分长度，默认 `280` |
| `WEBHOOK_OUTBOUND_TEXT_MAX_CHARS` | 单条入站通知最多转发的文本长度，默认 `5000` |
| `WEBHOOK_LOG_DIR` | JSONL 日志目录，默认 `/logs` |
| `WEBHOOK_MEDIA_DIR` | base64 附件在服务内的保存目录，默认 `/app/media` |
| `WEBHOOK_PUBLIC_MEDIA_DIR` | 写入消息和日志里的媒体路径前缀，默认 `/opt/WebhookToNapcat/media` |

BililiveRecorder 相关配置：

| 环境变量 | 说明 |
|---|---|
| `WEBHOOK_AGGREGATE_WINDOW_MS` | 开播/下播事件聚合窗口，默认 `3000` |
| `WEBHOOK_NOTIFY_DEBOUNCE_MS` | 下播等待窗口，默认 `15000` |
| `WEBHOOK_LIVE_SESSION_SEGMENT_TTL_MS` | 录制分段统计缓存时间，默认 18 小时 |
| `WEBHOOK_POST_END_START_CONFIRM_MS` | 下播后疑似重连开播确认窗口，默认 `45000` |
| `BILILIVE_XML_BASE_DIR` | XML 弹幕统计文件根目录，留空则不读取 XML |
| `BILILIVE_XML_STRIP_PREFIXES` | 从录制相对路径里剥离的前缀，多个用英文逗号分隔 |
| `BILILIVE_GIFT_PRICE_TABLE` | 礼物价格 Markdown 表路径 |

ito 内部通知相关配置：

| 环境变量 | 说明 |
|---|---|
| `WEBHOOK_INTERNAL_DEDUPE_TTL_SECONDS` | `notification_id` 内存去重时间，默认 24 小时 |

## Docker Compose

直接编辑 `docker-compose.yml` 里的环境变量，然后启动：

```bash
docker compose pull
docker compose up -d
```

查看日志：

```bash
docker compose logs -f
```

日志默认写入：

```text
./logs
```

base64 附件默认写入：

```text
./media
```

## 通知处理

### BililiveRecorder

当 JSON 里有 `EventType` 和 `EventData`，且事件属于下面这些类型时，会进入 BililiveRecorder 链路：

```text
StreamStarted
SessionStarted
FileOpening
FileClosed
SessionEnded
StreamEnded
```

现在文案是内置固定格式：

- 开播只认 `StreamStarted`，`SessionStarted` / `FileOpening` 只参与聚合判断。
- 下播会在等待窗口里合并 `StreamEnded` 和带统计的 `FileClosed` / `SessionEnded`。
- 直播中录制切段不会被当成真正下播。
- 真实下播会合并当前直播周期内的录制分段时长、大小和可读取的 XML 统计。

### ito 内部通知

当 JSON 里 `program_id` 等于 `ito` 时，会优先进入内部通知链路。

请求正文需要包含这 7 个顶层字段：

```text
notification_id
program_id
program_name
targets
summary
sent_at
attachments
```

转发规则很简单：

- 用 `notification_id` 做短期去重。
- `targets` 里的 `user` 转 QQ 私聊，`group` 转 QQ 群。
- 只把 `summary` 当正文发给用户。
- 如果有 `attachments`，会在 `summary` 发送后先保存附件，再作为 QQ 文件发送。
- 附件发送失败不会影响 `summary` 的发送结果，只会记录到日志。

### 未知通知

不属于 BililiveRecorder，也不是 `program_id=ito` 的请求，会进入未知通知链路。

- JSON 会按原始结构转成多行文本发送。
- 纯文本会直接发送。
- base64 字段会先保存为附件，正文里只显示保存摘要。
- 图片附件会尽量作为 QQ 图片发送。

## 本地运行

```bash
python3 -m pip install -e .
webhook-to-napcat
```

测试：

```bash
python3 -m pytest
```

健康检查：

```bash
curl http://127.0.0.1:8787/health
```

发送一个未知通知测试：

```bash
curl -X POST 'http://127.0.0.1:8787/webhook' \
  -H 'Content-Type: application/json' \
  -d '{"event":"test","status":"ok"}'
```
