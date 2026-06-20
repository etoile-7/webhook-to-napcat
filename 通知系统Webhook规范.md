# 通知系统 Webhook 规范

这份规范用于统一不同程序发出的 webhook（网络回调）通知结构，也可以作为消息转发程序的解析依据。

这套通知系统最终目标是“给人看”。转发程序只负责把 `summary`（摘要）里的内容发送给用户，不理解业务字段，也不从业务字段里二次拼接文本。

因此这里先约定一件事：通知正文只保留 7 个顶层字段。所有需要传达给用户的信息，都必须写进 `summary`（摘要）。

## 传输约定

- 请求方法：`POST`
- 内容类型：`application/json; charset=utf-8`
- 请求正文：UTF-8 编码 JSON（结构化数据）
- 时间字段：结构化字段统一使用 UTC（协调世界时）的 ISO 8601（时间格式），例如 `2026-06-10T13:30:00Z`
- 展示时间：`summary`（摘要）里可以使用面向用户的本地时间，例如 `2026-06-10T21:30:00+08:00`
- 字段命名：统一使用 snake_case（下划线命名）
- 空值策略：7 个顶层字段必须都存在；没有转发目标时 `targets`（转发目标）写空数组；没有附件时 `attachments`（附件列表）写空数组

发送端应当把一次通知视为“最多一次送达”。如果接收端返回非 2xx（成功响应）或网络失败，发送端可以重试；接收端需要用 `notification_id`（通知唯一标识）做去重。

## 顶层字段

所有通知的 JSON（结构化数据）正文必须包含且只应包含以下 7 个顶层字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `notification_id` | string | 是 | 单条通知唯一标识，用于转发去重 |
| `program_id` | string | 是 | 发送程序的内部标识，用于识别内部通知，值固定为 “ito” |
| `program_name` | string | 是 | 发送 webhook（网络回调）的程序名称，给用户和日志阅读使用 |
| `targets` | array | 是 | 转发目标列表 |
| `summary` | string | 是 | 要转发给用户的完整通知文本 |
| `sent_at` | string | 是 | 通知发送时间，统一使用 UTC（协调世界时） |
| `attachments` | array | 是 | 附件列表，没有附件时为空数组 |

不要把业务字段作为顶层字段发送，例如 `status`（状态）、`event_id`（事件标识）、`reason`（原因）、`task`（任务信息）、`order`（订单信息）、`resource`（资源信息）、`result`（结果信息）、`extra`（额外信息）等。

如果这些信息需要让用户看到，就写进 `summary`（摘要）。如果不需要让用户看到，就不要发送。

## Summary

`summary`（摘要）是通知的主体，也是转发程序唯一会转发给用户的文本内容。

发送端必须把一切需要用户知道的信息写进 `summary`（摘要），包括但不限于：

- 当前状态
- 事件、任务或业务对象标识
- 标题、名称、来源
- 成功结果，例如编号、链接、产物路径
- 失败原因、跳过原因、挂起原因
- 关键统计，例如数量、时长、大小、耗时
- 用户下一步需要知道的处理结论
- 面向用户展示的时间

`summary`（摘要）建议使用多行文本，每行表达一个明确事实。不要依赖转发程序解析 JSON（结构化数据）里的其他字段来补全文案。

推荐格式：

```text
ExampleProgram
状态：处理完成
事件：event-123
对象：resource-456
标题：每日任务处理
结果：生成 3 个文件
链接：https://example.com/results/123
耗时：2分18秒
时间：2026-06-10T21:30:00+08:00
```

失败、跳过、挂起等需要解释的通知，应在 `summary`（摘要）中加入原因：

```text
原因：外部服务连续请求失败
```

## Notification ID

`notification_id`（通知唯一标识）用于防止 webhook（网络回调）重试造成重复刷屏。

发送端应当保证同一条通知重试时使用同一个 `notification_id`（通知唯一标识）。不同通知不能复用同一个 `notification_id`（通知唯一标识）。

推荐组成方式：

```text
program_id:业务对象:通知动作:必要的细分标识
```

示例：

```text
example:resource-456:completed:main
```

如果发送端只能在发送时生成唯一值，也可以追加发送时间，但要注意：同一次通知重试时不能重新生成不同的值。

## 转发目标

`targets`（转发目标）是数组，每个对象至少包含：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | 目标类型，例如 `user`（单人）或 `group`（群组） |
| `id` | string | 是 | 目标 ID |
| `name` | string | 否 | 便于日志阅读的显示名 |

示例：

```json
[
  {"type": "user", "id": "123456"},
  {"type": "group", "id": "88886666"}
]
```

转发程序应当忽略不认识的 `type`（目标类型），并记录日志。发送端不应把空 `id`（目标标识）的目标写入列表。

## 附件

`attachments`（附件列表）统一放在顶层数组。它只用于传递文件，不承载通知正文。

每个附件对象至少包含：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | 附件用途，例如 `image`（图片）、`log`（日志）、`file`（文件） |
| `file_name` | string | 是 | 文件名 |
| `mime_type` | string | 是 | 媒体类型，例如 `image/png` |
| `base64` | string | 是 | 文件内容的 Base64（Base64 编码）字符串 |

可选字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `size_bytes` | number | 附件原始大小 |
| `sha256` | string | 附件哈希，用于校验 |
| `caption` | string | 附件说明文字 |

附件规则：

- 小图片、小日志片段或小文件可以直接放入 `base64`（Base64 编码）
- 大文件不建议直接走通知 webhook（网络回调），应改用外部链接，并把链接写进 `summary`（摘要）
- 附件说明可以写在 `caption`（附件说明）里，但用户必须知道的内容仍要写进 `summary`（摘要）
- 如果附件发送失败，转发程序仍应发送 `summary`（摘要），并在日志里记录附件失败原因

## 转发程序处理规则

转发程序只依赖 7 个顶层字段，按下面顺序处理：

1. 校验 JSON（结构化数据）是否能解析；失败则记录原始请求摘要，不转发附件。
2. 校验 `notification_id`、`program_id`、`program_name`、`targets`、`summary`、`sent_at`、`attachments` 是否存在。
3. 用 `notification_id`（通知唯一标识）做短期去重，避免 webhook（网络回调）重试导致重复刷屏。
4. 对 `targets`（转发目标）逐个发送；目标类型如何发送由具体转发程序决定。
5. 发送 `summary`（摘要）文本。
6. 再处理 `attachments`（附件列表）；不支持的附件类型可以只记录日志。
7. 任一目标发送失败时，记录失败目标、失败原因和 `notification_id`（通知唯一标识），不要影响其他目标。

转发程序不应该理解业务，也不应该从额外字段里拼接用户消息。它只转发 `summary`（摘要）和可处理的 `attachments`（附件）。

## 标准示例

```json
{
  "notification_id": "example:resource-456:completed:main",
  "program_id": "example",
  "program_name": "ExampleProgram",
  "targets": [
    {"type": "user", "id": "user-001"}
  ],
  "summary": "ExampleProgram\n状态：处理完成\n事件：event-123\n对象：resource-456\n标题：每日任务处理\n结果：生成 3 个文件\n链接：https://example.com/results/123\n耗时：2分18秒\n时间：2026-06-10T21:30:00+08:00",
  "sent_at": "2026-06-10T13:30:00Z",
  "attachments": [
    {
      "type": "image",
      "file_name": "result.png",
      "mime_type": "image/png",
      "base64": "..."
    }
  ]
}
```
