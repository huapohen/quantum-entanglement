# 认证会话事件读取合同（2026-08-30）

> 实现提交：`1f262f0`
>
> 分支：`mainline_continue_quantum_entanglement`
>
> 性质：只读 contract seam；不等于真实 IM、生产授权或 Gate A–E 通过

## 目标

在已完成的 trusted tenant context 与 conversation read seam 之上，固定第一版“读取会话事件”的
边界，避免客户端直接猜测 provider channel、把历史消息当成 authority，或用 cursor 重放越过租户/ACL。

入口：

```text
GET /api/v1/tenants/:tenantId/conversations/:conversationId/events
```

事件 stream ID 采用平台 `ConversationID`（例如 `cnv_room`），而不是融云/其他 provider 的 group ID。
这使 provider mapping 只能作为边缘输入，不能成为平台读取授权事实。

## 读取执行链

```text
Bearer verifier
  -> trusted tenant context middleware
  -> path tenant == trusted tenant
  -> bounded limit/opaque after parsing
  -> action-time identity resolve in UoW read snapshot
  -> current conversation + active membership + read permission
  -> EventStore stream page
  -> tenant/workspace/stream/sequence/dedupe/payload validation
  -> safe event page + authority revision snapshot
```

`EventStore` 未被 composition 注入时返回 `50301 dependency unavailable`，绝不把“没有存储”伪装成
空成功页。当前 `cmd/im-api` 的 PostgreSQL runtime 仍未注入它，这个故意的硬停止保护了生产边界。

## 请求和响应合同

| 项目 | 约束 |
|---|---|
| `tenantId` / `conversationId` | 必须是平台 ID；path tenant 必须等于可信 tenant |
| `limit` | 默认 50，`1..256`；空白、非十进制、0、超界均 `42201` |
| `after` | EventStore 签发的 opaque cursor；缺省为空；错域/损坏 cursor 为 `42201` |
| scope | Event 的 tenant、workspace、stream 必须与授权 conversation 完全一致 |
| 顺序 | 页内 sequence 严格递增，global position 非零；页大小不能超过 limit |
| 去重 | 页内 event ID 不得重复；`dedupeKey` 明确等于 event ID，仅表示观察键 |
| payload | inline 保留 canonical JSON object；reference 只返回 storage/referenceId/byteLength |
| authority snapshot | 返回 conversation/membership/access revision，供客户端检测授权版本漂移 |

响应 data 的主要字段：

```json
{
  "tenantId": "ten_alpha",
  "conversationId": "cnv_room",
  "events": [{
    "eventId": "evt_message_1",
    "streamId": "cnv_room",
    "eventType": "message.created",
    "sequence": 1,
    "globalPosition": 1,
    "dedupeKey": "evt_message_1",
    "payloadKind": "inline",
    "payload": {"conversationId": "cnv_room", "text": "hello"}
  }],
  "nextCursor": "<opaque>",
  "hasMore": false,
  "snapshot": {
    "conversationRevision": 7,
    "membershipRevision": 8,
    "accessRevision": 9,
    "afterCursor": "",
    "nextCursor": "<opaque>"
  }
}
```

`nextCursor` 不可由客户端拼接；客户端必须原样带回同一 tenant/conversation endpoint。cursor 是事件
存储页的 resume token，不是能力令牌，不延长 membership，也不授权写入或 Agent 调度。

## 负向矩阵

已覆盖的专项断言：

1. 有效 bearer、tenant、conversation 和 read access 返回一页事件；
2. 第二页使用原样 cursor，返回稳定空尾页且不重复第一条；
3. 损坏 cursor 返回 validation code；
4. 空 permission 即使 EventStore 可用也返回 forbidden；
5. EventStore 缺失返回 dependency-unavailable；
6. event tenant、workspace、stream 漂移、sequence 回退、global position 为零、页内重复 ID 或
   非法 payload 均 fail closed；
7. event page 的 event ID 只作为 dedupe observation，不会被解释为 command idempotency 或接受凭证。

## 验证证据

```text
cd apps/im-api
go test ./internal/app          # PASS
go test ./...                  # PASS
go vet ./...                   # PASS
git diff --check               # PASS
```

测试位于 `internal/app/event_read_test.go`；使用 `VolatileMemoryStore` 仅作为零网络合同 fake，
没有真实 IM、飞书、企微、模型或 outbound 调用。

## 当前未闭合边界

- EventStore 查询与 authority 读取目前是两个 port；生产实现必须把事件读取纳入同一 repeatable-read
  UoW snapshot，或提供等价的绑定 snapshot token，才能抵抗 ACL 在两次读取之间的 TOCTOU；
- 尚无 message projection（`message.created/edited/recalled` 到 `MessageSnapshot`）、conversation
  list、read state、mention/thread、Task/Artifact/Needs You 或 write command；
- PostgreSQL event store 本身已有低层分页能力，但尚未接到 `RuntimeDependencies` 的生产 composition；
- 真实 Clerk/JWKS、provider profile/transport、cursor 兼容窗口、备份/恢复和多进程 crash evidence
  仍未完成；
- Gate A–E、真实 IM、任何 outbound 和 Agent dispatch 继续关闭。

下一步应先实现同一 snapshot 的 durable message projection/read repository，再补 message cursor 与
inbox-to-message dedupe；任何写入前必须沿用 command identity、atomic UoW、receipt/readback 和 crash
boundary。
