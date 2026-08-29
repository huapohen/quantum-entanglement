# v0版 PostgreSQL durable projection checkpoint 实现检查点（2026-08-29）

## 结论

本阶段把 `events.ProjectionCheckpointStore` 从框架中立 port 落到了 PostgreSQL 18.6。新增
`event_projection_checkpoints` 表和 runtime-only `write_projection_checkpoint` 函数，投影可以在
进程重启后恢复 global position/cursor/last event ID，并以完整旧 checkpoint 做 CAS，避免两个 consumer
静默覆盖彼此的进度。

这是 durable checkpoint 的可运行切片，不是完整消费平台：inbox 去重、outbox/action receipt、dead
letter、commit-unknown reconcile、跨区备份和 worker promotion 仍是 NO-GO。真实融云、Clerk、IM
provider 出网仍关闭。

Notion 本阶段 `local_pending`。本地报告、代码、测试和 Git 是事实来源，阶段收口后再批量写入并回读；
没有向飞书、企微或任何群聊发消息。

## Schema 0008

`apps/im-api/internal/platform/postgres/migrations/sql/0008_event_projection_checkpoint.up.sql` 新增：

- `event_projection_checkpoints` 主键 `(tenant_id, workspace_id, projection_id)`；root workspace
  在数据库中编码为空字符串，调用层的 `nil` 与命名 workspace 严格区分；
- `global_position` 允许零表示初始 checkpoint，非零时 cursor 和 last event ID 必须同时非空；
- cursor 最大 4096 字节，event/projection ID 最大 256 字节，position 不超过 PostgreSQL bigint；
- tenant 外键、`FORCE ROW LEVEL SECURITY`、精确 `wanwork.tenant_id` policy；
- `write_projection_checkpoint` 为 `SECURITY DEFINER`、`STRICT`、`SET search_path TO pg_catalog`，
  只返回写入成功布尔值，函数自身检查 tenant GUC、坐标形状、单调性，并以旧三元组做条件 update；
  初次写入使用 `ON CONFLICT DO NOTHING`，竞争者得到 false。

0008 down 只删除函数和表。迁移 runner 永不自动执行 DownSQL；回滚需要独立审批、备份和兼容数据证明。

## Authority 与运行时边界

- migration catalog 由 7 升至 8，event-store v6/v7 postcondition 按当前 schema 版本区分：schema 已到 v7
  时不再把 v6 digest 当作合法，避免 ledger/schema 漂移后重复执行 0007 DDL；
- authority manifest 增加 checkpoint 表和函数，runtime role 只有该表的 `SELECT` 与函数 `EXECUTE`，
  没有直接 `INSERT/UPDATE`；`SELECT ... FOR UPDATE` 也不被 runtime adapter 使用（该操作需要 UPDATE
  权限），CAS 写面完全收敛到 SECURITY DEFINER 函数；
- migration/owner helper、runtime access manifest、对象/权限计数、catalog、cutover/plan/preflight
  golden digest 均已更新；新函数定义 digest 固定并由 postcondition 验证；
- eventstore adapter 只接受已经 attested 的 `*runtimepool.Pool`，任何 raw pgxpool/owner/migrator
  连接不能注入。

## Adapter 行为

代码位于 `apps/im-api/internal/platform/postgres/eventstore/checkpoint_store.go`：

1. Load 走 `REPEATABLE READ READ ONLY`，绑定 tenant GUC，精确查询 tenant/workspace/projection；无行
   返回 scope-bound 零 checkpoint；损坏坐标 fail-closed。
2. Commit 先验证 scope、position、cursor、last event ID 和 bigint 边界，再用 `SERIALIZABLE` 事务读取
   当前值；旧值不一致返回 `ErrProjectionCheckpointConflict`。
3. 事务调用固定 `write_projection_checkpoint`，false 统一视为 CAS 冲突；数据库异常映射为
   `ErrProjectionStoreUnavailable`，context cancellation 保留 context error。
4. checkpoint 不直接拥有事件业务副作用；projection handler 的 at-least-once 幂等义务仍由上层
   `Projector` 承担。

同时修正 EventStore public error contract：坏行 materialization、写后 readback 缺失等 adapter 私有
sentinel 不再透传，统一映射为公开 `events.ErrStoreUnavailable`；SQLSTATE `22003` 映射到已有
`events.ErrStoreCapacity`，避免把明确容量耗尽误当成无限 transient retry。

## 测试证据

纯 Go 测试覆盖 adapter scope/坐标校验、nil runtime pool、内部错误脱敏、capacity/context 映射；
PostgreSQL 18.6 集成覆盖：

- fresh 0001–0008 migrations、schema digest、exact authority manifest；
- runtime role 读取初始 checkpoint、函数写入、stale CAS 冲突；
- 关闭 runtime pool 后重新打开，checkpoint position/cursor/last event ID 完整读回；
- runtime 直接写 `event_log` 仍为 SQLSTATE 42501，checkpoint 表同样不授予直接写权限；
- migration owner repeat Apply、access manifest、authority/cutover/preflight golden 与完整迁移包。

命令（均通过，集成命令使用本机临时 PostgreSQL 18.6）：

```text
cd /Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_wanwork_quantum_entanglement/apps/im-api
go test ./...
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55483/postgres?sslmode=disable' \
  go test ./internal/platform/postgres/migrations -count=1
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55483/postgres?sslmode=disable' \
  go test ./internal/platform/postgres/eventstore -run TestPostgresEventStoreAgainstPostgres -count=1
```

本阶段 `go test -race ./...` 与 `go vet ./...` 继续作为收口门禁；测试未访问 Notion、语雀、飞书、企微、
融云或 Clerk。

## Git 版本

| Commit | 内容 |
|---|---|
| `82f2790` | `feat(postgres): add durable projection checkpoint migration` |
| `4d6f76c` | `feat(postgres): add durable projection checkpoint adapter` |
| `9482790` | `fix(postgres): map event store integrity errors publicly` |

远端 ref：`origin/dev_wanwork_quantum_entanglement`。本报告和下一份同步包会作为独立文档提交；Notion
目标页为 `research-44`、`research-43` 和 `current-implementation`，状态保持 `local_pending`。

## 下一步与明确边界

1. 给 checkpoint 增加并发竞争、commit-unknown/reconcile 和损坏行集成测试；
2. 在同一 migration/authority 模型下实现 inbox 去重（event ID + projection scope）和 outbox/action
   receipt，明确 provider 外部副作用的 exactly-once 不可宣称边界；
3. 实现 dead-letter、lag/retention/capacity 指标和备份恢复/kill-9 演练；
4. 只有 projection/inbox/outbox/result authority 全部有 durable 证据后，才打开原生 IM worker 或真实
   provider delivery。
