# v0版 PostgreSQL digest-bound native IM inbox 实现检查点（2026-08-29）

## 结论

本阶段把原生 IM 入站去重从“合同和内存 fake”推进到 PostgreSQL durable admission。新增
`events.InboxStore` provider-neutral port、`native_im_inbox` migration 0009，以及只允许
attested runtime pool 调用的 `admit_native_im_inbox` `SECURITY DEFINER` 函数。

同一 `(tenant, workspace, provider, channel, eventId)` 的相同 `eventDigest` 和 payload digest
只产生一条 durable 记录，重送返回 `replayed` 并递增 delivery count；同 event ID 的 digest 或
payload 漂移返回 `ErrInboxDigestConflict`，不会进入后续路由。root workspace 用空字符串数据库
哨兵编码，调用层的 `nil` 与命名 workspace 严格区分。

这只是 verified envelope 的 durable admission 切片，不是融云接入完成：provider 签名/nonce/租户
mapping、Clerk trusted request context、message projection、mention bridge、outbox/action
receipt、dead-letter、真实 provider 读写和生产 worker 仍为 NO-GO。commit-unknown 已有独立的
隔离/readback 代码路径，但还没有和 event admission bridge、outbox 或操作台闭合。

Notion 本阶段保持 `local_pending`。本地代码、测试、报告和 Git 是事实来源，阶段全部收口后再批量
写入 Notion 并回读；没有操作语雀，也没有向飞书、企微、群聊、机器人或 webhook 发消息。

## 1. 合同层

`apps/im-api/internal/events/inbox.go` 定义：

- `InboxScope`：`tenantId`、可选 `workspaceId`、`provider`、`channelId` 的精确 transport namespace；
- `InboxEnvelope`：`eventId`、provider-neutral `eventDigest`、`verificationId` 和已 canonical 的
  `events.Payload`；
- `InboxStore.Admit` / `Load`：写入前验证、同 digest 重放、digest 冲突和按 scope 读取；
- `MemoryInboxStore`：明确为 volatile contract fake，不声明持久化或生产能力。

验证边界：

1. scope、event ID、verification ID 和 provider/channel 均拒绝空值、控制字符、越界文本；
2. event digest 和 payload digest 必须是小写 `sha256:` 64-hex；
3. payload 复用现有严格 inline-object/reference codec，不能把任意 JSON、secret、临时 URL 或凭据
   直接写进 inbox；
4. memory fake 在 mutex 内完成单 winner admission，返回记录为深拷贝，调用者不能在入库后修改事实；
5. 同 scope + event ID 的不同 digest/payload 只返回冲突，不覆盖原记录；跨 workspace 读取返回
   `ErrInboxNotFound`，不把 `nil` 当 wildcard。

## 2. PostgreSQL 0009

`apps/im-api/internal/platform/postgres/migrations/sql/0009_native_im_inbox.up.sql` 新增：

- 主键 `(tenant_id, workspace_id, provider, channel_id, event_id)`；
- `event_digest`、`payload_digest`、verification ID、payload canonical storage fields；
- `first_received_at` / `last_received_at` 由数据库 `clock_timestamp()` 产生，调用者不能伪造；
- `delivery_count`、时间顺序、payload kind/shape、provider/event/channel 长度和 digest checks；
- tenant 外键、`ENABLE ROW LEVEL SECURITY` 与 `FORCE ROW LEVEL SECURITY`；
- 精确 `wanwork.tenant_id` policy，禁止跨 tenant 读写。

`admit_native_im_inbox`：

- `STRICT`、`SECURITY DEFINER`、`PARALLEL UNSAFE`、`SET search_path TO pg_catalog`；
- 首先比较 transaction-local tenant GUC 与显式 tenant 参数，不一致以 SQLSTATE 42501 失败；
- `INSERT ... ON CONFLICT DO NOTHING` 保障并发入站只有一个 inserted winner；
- 首次写入返回 `inserted`；已有记录只有 event/payload digest 完全一致才更新最后观察时间和计数并
  返回 `replayed`；任何漂移返回 `conflict`；
- PUBLIC 没有 EXECUTE，runtime 只获得登记函数的 EXECUTE 和 inbox 表 SELECT；直接 INSERT/UPDATE/
  DELETE 仍被拒绝。

Migration catalog、schema digest、function definition digest、authority object/privilege manifest、
runtime read table 和 cutover/preflight golden 已同步到版本 9。runner 继续逐版本累计验证旧
postcondition，显式 DownSQL 仍不由 runner 自动执行。

## 3. Runtime adapter

`apps/im-api/internal/platform/postgres/eventstore/inbox_store.go` 的 `NativeIMInboxStore`：

1. 只接受 `*runtimepool.Pool`，不接受 raw pgxpool、owner connection 或 migrator connection；
2. `Admit` 先在进程内校验 provider-neutral envelope 和 PostgreSQL ID 约束，再进入 Read Committed
   transaction；同 scope/event 的唯一键冲突由数据库函数协调，避免正常并发重放被序列化冲突误判；
3. 在 transaction 内绑定 tenant GUC，调用固定函数，然后从同一事务 readback 完整 row；
4. readback 重新构造严格 `events.Payload` 和 envelope，验证 scope、digest、时间、计数和 storage
   shape，任何坏行 fail closed；
5. `Load` 使用 Repeatable Read + read-only transaction，严格绑定完整 scope，缺行只返回
   `ErrInboxNotFound`；
6. context cancellation 保留原始 context error；数据库故障统一为 `ErrInboxStoreUnavailable`，不
   泄露 SQL、连接、路径或内部 sentinel。

## 4. 证据

纯 Go：

```text
go test ./internal/events -count=1
go test ./internal/platform/postgres/eventstore -count=1
go test ./internal/platform/postgres/migrations -count=1
go test ./internal/platform/postgres/authoritycutover -count=1
```

PostgreSQL 18.6（本机临时数据库，仅集成证据，不是生产 promotion）：

```text
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55483/postgres?sslmode=disable' \
  go test ./internal/platform/postgres/migrations -count=1 -timeout=10m

WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55483/postgres?sslmode=disable' \
  go test ./internal/platform/postgres/eventstore \
    -run TestPostgresEventStoreAgainstPostgres -count=1 -timeout=10m
```

已覆盖：

- fresh/repeat `0001..0009`、ledger count、future ledger、migration concurrency、schema/function digest；
- exact authority object、runtime table SELECT/function EXECUTE 与 drift validator；
- inserted/replayed/conflict 三态、delivery count、first/last received readback；
- same tenant alternate scope、cross-tenant not-found、RLS 直接 runtime table write 拒绝；
- payload digest/storage corruption、invalid provider/scope、context/error redaction；
- authority cutover/preflight golden 随 schema version 9 更新。

## 5. 当前不变量和剩余门禁

已关闭：

- 入站 transport event ID 与业务消息 ID 的存储边界已经可以独立承载；
- digest-bound durable inbox admission 已有 PostgreSQL 表、RLS、function-only writer、runtime adapter
  和 restart/readback 证据；
- duplicate replay 不重写首次 verification ID、payload 或首次观察时间；
- digest drift 不会被当作幂等重送，也不会触发下游路由。

仍明确 NO-GO：

- `IMVerifiedInboundEnvelopeV1` strict codec、provider signature/nonce/replay-window verifier；
- Clerk claim → realm → tenant membership → Actor 的 trusted request context；
- durable event 与 inbox admission 的同一事务 bridge、message projection、mention routing、Agent
  child thread/Task/Attempt；
- PostgreSQL outbox、lease/fencing、provider action receipt、effect unknown/reconcile、DLQ、lag/retention
  和 backup/restore/kill-9 演练；
- 融云 SDK、Clerk、任何外部 provider endpoint 和生产 worker 出网。

下一步顺序：将现有 inbox commit-unknown/readback 证据接入 inbox 与 event admission bridge 的统一事务
边界，再实现 transactional outbox/action receipt 和 reconcile；完成这些 durable 门禁后才进入 W3 provider adapter
与 inbound-only sandbox。真实 outbound 仍需用户对具体 sandbox 动作单独授权。

## 6. Git 台账

| Commit | 内容 |
|---|---|
| `3b933c3` | provider-neutral digest-bound inbox contract 与 volatile fake |
| `0c9cbac` | PostgreSQL 0009 native IM inbox migration、RLS、function-only writer、authority 更新 |
| `5248a0a` | `NativeIMInboxStore` runtime adapter、unit/integration tests |
| `2c208e6` | commit-unknown 隔离、fresh readback reconcile、并发 admission 语义收口 |
| `0ac868a` | commit-unknown 阶段证据与本地 report-sync checkpoint |

远端：`origin/dev_wanwork_quantum_entanglement`；本报告和同步包会以独立 `local_pending` 文档提交。
