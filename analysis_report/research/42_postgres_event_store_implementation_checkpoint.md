# v0版 PostgreSQL Durable EventStore 实现检查点（2026-08-29）

## 结论

本阶段把 W1 的 `VolatileMemoryStore` 合同 fake、W1 的本地 crash-recoverable JSONL store，推进到
W2 PostgreSQL durable event spine 的第一个可运行切片。`events.EventStore` 现在有一个只接受已认证
`runtimepool.Pool` 的 PostgreSQL adapter；在本地 PostgreSQL 18.6 上已实证追加、重试、分页、租户隔离、
重启读回、运行时禁止直写和并发单 winner。

这不是“已连接真实融云/生产 IM”的声明。当前实现关闭的是 durable event source 的基础合同，不是
message projection、inbox/outbox、provider delivery、备份恢复或 Agent result authority 的全部生产门禁。
`Characteristics.TamperEvident` 仍明确为 `false`；cursor 的 digest 只用于防止 scope/内容意外漂移，
不是授权签名，也不能代替 Clerk session 或 action-time PEP。

Notion 本阶段保持 `local_pending`。本地 `analysis_report`、Git commit 和测试输出是当前事实来源，
阶段全部收口后再批量上传并逐页回读；没有向飞书、企微或任何群聊发送消息。

## 实现边界

代码位于 `apps/im-api/internal/platform/postgres/eventstore/`：

- `Store` 的构造函数只接受 `*runtimepool.Pool`，不能注入 owner/migrator/raw `pgxpool`；运行时 pool
  在 acquire、session、role、search_path、application_name、RLS authority 上持续做 exact attestation。
- `AppendBatch` 在一个 `SERIALIZABLE` transaction 内完成 tenant binding、exact event-ID replay probe、
  digest 计算、逐事件调用 `wanwork_im.write_event`、raw-row readback 和 coordinate/digest 校验；任何
  失败都在 commit 前回滚，避免 batch 半写入。
- 每个接受的 event 拥有 store 生成的 stream `sequence`、tenant/workspace 范围内的
  `global_position` 和数据库 `recorded_at`。caller 不能提供这些 store-owned facts。
- inline payload 按既有 canonical JSON 合同保存；reference payload 只保存受验证的 storage、reference
  ID、byte length 和 digest，不把外部 blob 内容假装成数据库事实。
- read path 只使用 `REPEATABLE READ READ ONLY` transaction，并在事务内绑定 tenant。stream/global
  page 使用 scope-bound opaque cursor，严格拒绝非 canonical base64、重复 JSON key、未知字段、错误
  digest、错误 tenant/workspace/stream/kind、零位置和未来位置。
- durable row readback 会重新构造 `events.EventToAppend`，验证 payload digest、append digest、事件合同、
  sequence/global position 和 recorded-at；损坏行 fail-closed 为内部 integrity failure，不返回未经验证的
  event。

## Migration 与 retry identity

`0006_event_store` 已在前一提交发布表、RLS 和 `SECURITY DEFINER` 的 `write_event`。本阶段没有改写已
发布 migration，而是新增 `0007_event_retry_identity`：

1. 将 `event_log` 主键从 `(tenant_id, event_id)` 迁移为 `(tenant_id, workspace_id, event_id)`，与现有
   EventStore 合同一致：同一 tenant 的不同 workspace 可以复用 event ID，同一 workspace 的不同 stream
   仍不能复用。
2. 新增 `(tenant_id, workspace_id, stream_id, idempotency_key)` 的条件唯一索引，只约束非空 key；因此
   idempotency key 的 scope 与合同 fake 一致，跨 stream 重用不会误判为同一 retry identity。
3. 新增 migration postcondition，精确检查主键定义和条件唯一索引；旧 migration checksum 保持不变，
   已应用 0006 的环境可向前应用 0007。

同时升级了 migration catalog、未来版本测试、runtime authority fixture、cutover plan fixture 和
golden digest，避免 schema version/catalog digest 停留在 6 或旧计划金标准。

## 错误与并发语义

- exact retry 必须在同一 tenant/workspace scope 内找到完整事件集合，并且 event content、payload、
  append shape、expected sequence 全部一致；subset、superset、reorder、正文/actor/key drift、partial
  overlap 都是 `ErrIdempotencyConflict`。
- expected revision 不匹配由 fixed `write_event` 返回 `false`，映射为 `ErrRevisionConflict`；并发
  `SERIALIZABLE` 冲突可能映射为 `ErrStoreUnavailable`，调用方应按可重试故障处理。
- PostgreSQL unique violation 映射为 `ErrIdempotencyConflict`，条件唯一索引阻止同一 scope 下不同
  event 使用同一 idempotency key。
- 所有数据库异常均不透传 DSN、SQL credential、payload 原文或服务端敏感错误；context cancellation
  优先保留为 `context.Canceled`/`context.DeadlineExceeded`。

## 测试证据

### 纯 adapter 合同测试

`store_test.go` 覆盖：

- nil runtime pool、durability characteristics 与 typed-nil 行为；
- cursor round trip、scope binding、严格 JSON duplicate-key、padding、zero/future/overflow position；
- inline/reference payload parts、超大 reference、corrupted append digest、zero recorded-at；
- tenant/workspace/expected-version 的数据库 admission shape。

### PostgreSQL 18.6 集成矩阵

`store_integration_test.go` 使用临时数据库、独立 runtime role 和 RLS：

1. fresh migration 0001–0007、runtime pool exact authority 和 event-store function access；
2. 两事件 batch append、sequence/global position 连续性、exact replay、actor drift 和 stale revision；
3. 条件 idempotency index 冲突、同 tenant 不同 workspace 的 event ID scope、跨 tenant 隔离；
4. stream/global cursor 分页、空 workspace 与 alternate workspace 精确区分；
5. runtime role 直接 `INSERT event_log` 得到 SQLSTATE `42501`；
6. 关闭并重新打开 runtime pool 后，已提交 stream 可完整读回；
7. 两个并发 fresh append 只有一个 winner，另一方仅为 revision/transient retry outcome。

本轮实际验证命令及结果：

```text
cd /Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_wanwork_quantum_entanglement/apps/im-api
go test ./...                         # passed
go test -race ./...                   # passed
go vet ./...                          # passed
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55483/postgres?sslmode=disable' \
  go test ./internal/platform/postgres/migrations \
    -run 'TestApplyAgainstPostgres' -count=1 -timeout=15m  # passed
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55483/postgres?sslmode=disable' \
  go test ./internal/platform/postgres/runtimepool \
    -run TestRuntimePoolAgainstPostgres -count=1 -timeout=15m # passed
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55483/postgres?sslmode=disable' \
  go test ./internal/platform/postgres/eventstore \
    -run TestPostgresEventStoreAgainstPostgres -count=1 -timeout=15m -v # passed
```

全量 race/vet 未设置集成 URL 时会跳过需要外部 PostgreSQL 的测试；上面三条带 URL 的命令补上了
fresh migration、authority 和 durable adapter 的真实数据库证据。测试只使用本机临时数据库，未访问
融云、Clerk、Notion、飞书或企微。

## Git 版本与本地同步

本阶段按小变化拆成四个已推送提交，均在
`dev_wanwork_quantum_entanglement`，未合并其他 worktree：

| Commit | 内容 |
|---|---|
| `e6a6482` | PostgreSQL durable EventStore adapter、严格 cursor/row materialization 单测 |
| `9fb0448` | 0007 retry identity migration、postcondition、catalog/runtime fixture 适配 |
| `e6611b5` | PostgreSQL 18.6 EventStore 集成测试 |
| `84c2b82` | schema 7 后 authority cutover fixture 与 golden digest |

远端 ref：`origin/dev_wanwork_quantum_entanglement`。本报告写入后会再产生一个文档提交；该提交
仍会先推送 GitHub，Notion 镜像等阶段收口后再批量处理。

## 尚未关闭的生产门禁

以下事项仍然保留为明确的 NO-GO，不应因为 adapter 通过而提前接入生产 IM：

- event projector、message projection rebuild、inbox/outbox、consumer checkpoint、dead-letter 与
  provider commit-unknown/reconciliation；
- event envelope 的 tamper-evident/rotating key 设计、backup/restore、跨区复制和 kill-9/断电演练；
- Clerk JWKS/session revoke、tenant resolver、action-time membership/ACL PEP 和真实融云 callback
  authenticity；
- Agent invocation 的 reserved result event fence、artifact/result receipt 同事务 writer、Observed/
  Accepted recovery 与 worker promotion gate；
- metrics/tracing、schema rollout/rollback runbook、capacity/retention/partition 和生产 PostgreSQL
  connection/SLO 压测；
- 正式 React/Tailwind/Zustand Web/PWA 与移动/桌面多端同步。

下一阶段优先级是：先补 durable event projection/inbox-outbox 合同和 raw-row/envelope 双 verifier，再
进入 reserved result fence；在此之前继续保持真实 provider 出网和生产 worker 关闭。
