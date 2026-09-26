# webhook-to-napcat

把 HTTP Webhook 转发到 NapCat（OneBot v11）的小服务。

这版只保留两条链路：

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
| `WEBHOOK_PUBLIC_MEDIA_DIR` | 传给 NapCat 的媒体路径前缀，默认 `/opt/WebhookToNapcat/media` |

ito 内部通知的投递回执保存在 `WEBHOOK_MEDIA_DIR/delivery/receipts.sqlite`。
请持久挂载整个媒体目录。回执不会按时间自动过期；删除该目录会丢失去重依据。
内部通知逐目标、逐文本分段记录结果，不使用 `NAPCAT_RETRIES` 进行盲目重发。
`WEBHOOK_OUTBOUND_TEXT_MAX_CHARS` 不截断内部通知的完整摘要。

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

当前服务没有额外设置请求体或 base64（Base64 编码）附件的硬上限。实际可处理大小主要受反向代理、容器内存、磁盘空间和 NapCat（OneBot v11）能力限制；建议只传小图片、小日志片段或小文件，大文件改用外部链接并写进正文。

## 通知处理

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

- 用 `notification_id` 和完整请求内容指纹持久去重；相同 ID 携带不同内容返回 409。
- `targets` 里的 `user` 转 QQ 私聊，`group` 转 QQ 群。
- 只把 `summary` 当正文发给用户。
- 如果有 `attachments`，会在 `summary` 发送后先保存附件；图片作为 QQ 图片发送，其他附件作为 QQ 文件发送。
- 附件发送失败不会影响 `summary` 的发送结果，只会记录到日志。

### 未知通知

不是 `program_id=ito` 的请求，会进入未知通知链路。

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

## 内部通知投递回执与人工核实

正文所有目标、所有分段均由 NapCat 确认后返回 `200/state=forwarded`。
已确认步骤不会随重试再次发送；明确失败返回 502，发送端可用原请求重试。
空目标返回 `200/state=accepted_no_targets`、`ok=false`，不代表送达。
附件失败单独计数，不撤销已成功正文，不触发正文重发。

下游超时或进程在发送中退出，无法判断 QQ 是否已收到，返回
`409/state=uncertain`。先核对 QQ/NapCat 记录，再用本地工具记录核实结果：

```bash
python -m webhook_to_napcat.receipts --media-dir /app/media --notification-id 'ito:example'
python -m webhook_to_napcat.receipts --media-dir /app/media --notification-id 'ito:example' --step 'text:private:123:0' --outcome delivered --evidence '已核对QQ消息记录'
```

仅在确认未发送时使用 `--outcome not-delivered`。工具不发消息，只保存核实结果，
随后由发送端重试同一通知。无法核实时保持 uncertain；不要删除回执绕过去重。
`delivered` 表示外部发送已确认，不表示用户已阅读。操作记录持久保存到同一数据库。
详见 `通知系统Webhook规范.md` 的响应约定。
