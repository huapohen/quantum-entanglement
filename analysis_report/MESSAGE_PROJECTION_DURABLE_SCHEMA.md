# v0版 durable message projection schema 合同

> 状态：schema 已注册到 PostgreSQL migration catalog（migration 11，`message_projection`），migration
> 12 已补齐 owner-only projector writer 候选并通过 catalog/RLS/postcondition/Go 代码门禁；本文档
> 仍不授权直接切换默认读取。当前读取 bridge 继续使用 bounded EventStore replay；materialized
> cutover 必须完成 applied-schema、双读比对、crash/restore 和 rollback 证据后才能开启。

## 1. 目标

为一个 tenant/workspace/conversation 建立可重建、可校验、可分页的消息投影。事件日志仍是事实源；
message rows 只是派生状态。任何 projection row、head 或 checkpoint 都不能单独授予读/写权限。

## 2. 表与键

### `wanwork_im.message_projection_heads`

```text
tenant_id              text       -- exact tenant FK/RLS key
workspace_id           text       -- '' represents no workspace
conversation_id        text       -- platform conversation identity
projection_id          text       -- frozen `messages-v1`
current_sequence       bigint     -- last applied stream sequence
current_global_position bigint    -- last applied event global position
current_revision       bigint     -- monotonically increasing projection generation
updated_at             timestamptz
PRIMARY KEY (tenant_id, workspace_id, conversation_id, projection_id)
```

### `wanwork_im.message_snapshots`

```text
tenant_id              text
workspace_id           text
conversation_id        text
message_id             text
client_message_id      text
sender_actor_id        text
message_type            text       -- text/system
status                  text       -- active/edited/recalled
text                    text
ext_info                text
created_at              timestamptz -- immutable UTC microseconds
revision                bigint
last_event_sequence     bigint
last_event_position     bigint
projection_revision     bigint
PRIMARY KEY (tenant_id, conversation_id, message_id)
```

Required unique/indexed access paths:

```text
(tenant_id, workspace_id, conversation_id, created_at, message_id)
(tenant_id, workspace_id, conversation_id, client_message_id)
(tenant_id, workspace_id, conversation_id, last_event_sequence)
```

`projection_revision` 是该消息行最后一次被消息 reducer 改写时的 generation；它不要求等于
当前 head generation。只推进 stream watermark 的非消息事件会使 head generation 前进而保留
旧行，因此恢复/读取校验使用 `0 < row.projection_revision <= head.current_revision`，并同时要求
消息行的事件坐标不超过 head 坐标。

All tables require exact tenant RLS, forced RLS, explicit runtime read grants, owner-only write functions,
and access-manifest/schema-digest registration. Workspace and conversation FKs must prevent cross-scope rows.

## 3. Apply transaction

The projector consumes one event-stream page in a single serializable transaction:

1. lock/read the exact head and verify `(tenant, workspace, conversation, projection_id)`;
2. read the next global event page after the projection cursor in the same Serializable transaction;
3. skip non-conversation streams, observe non-message conversation events, and apply only the three frozen
   message event types through the strict reducer;
4. call migration-12 owner functions to insert/update message snapshot and head with CAS/idempotent replay;
5. write `event_projection_checkpoints` in the same transaction;
6. commit; only then release the connection. Durable row/head restoration revalidates scope, revision,
   sequence and UTC values before replaying the next page.

Any duplicate event, sequence gap, scope drift, unknown payload, duplicate client identity, checkpoint conflict,
or partial write rolls back the entire transaction and leaves the projector lease in reconcile-required state.
There is no `trusted=true`, caller-supplied coordinates, or direct table mutation escape hatch.

## 4. Read cutover

The materialized reader must accept the same `MessageReadPageQuery` as the replay bridge. During shadow mode,
both readers run against the same authority revision and `CompareMessageReaders` compares ordered
`(message_id, revision, status, text, ext_info, created_at, sender, client_message_id)` tuples. A mismatch
is an integrity failure, never a best-effort merge. Each reader keeps its own opaque cursor; projection
generation and replay stream version are not compared as if they were the same coordinate. Cutover order:

```text
replay bridge -> shadow materialized read -> equality canary -> materialized primary -> replay fallback disabled
```

The cursor binds projection revision and page offset. A head revision change invalidates the cursor and requires
restart; a cursor is observation metadata, not an authorization capability.

## 5. Migration and rollback gates

- add a disabled migration candidate first; do not silently append to legacy bootstrap;
- update catalog checksum, schema digest, access manifest, backup topology and down guard together;
- empty and non-empty upgrade, restore, sparse upgrade and old-reader/new-reader matrix must pass;
- downgrade must preserve event log and refuse destructive removal when message rows/checkpoints exist;
- SIGKILL before/after commit must leave either the old head or the complete new head, never a prefix;
- only after all gates pass may `cmd/im-api` switch from replay reader to materialized reader.

## 6. Current status

The event-replay bridge is implemented and bounded. Migration 11 creates and protects the two materialized
tables; migration 12 and `platform/postgres/improjection.Projector` now provide an owner-function writer,
same-transaction global read and checkpoint CAS candidate. `research/69` covers a real PostgreSQL 18
runtime-only end-to-end projector/reader run, including pagination, non-message watermark, exact rerun and
concurrent rerun. Target production applied-schema attestation, shadow comparison wiring, crash/restore/
rollback evidence and production composition remain open. Gate A–E, real Clerk/JWKS, real IM provider and
outbound remain closed.
