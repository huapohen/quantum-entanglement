# v0版事件投影契约检查点（2026-08-29）

## 结论

本阶段在 `events.EventStore` 之上加入了框架中立的事件投影 replay contract。它把“从 durable
event log 追平一个派生读模型”的最小安全语义固定下来：投影按 tenant/workspace/projection 三元
组合作为精确命名空间，从 opaque global cursor 恢复，按页调用应用方 handler，只有整页成功后才用
compare-and-swap（CAS）提交 checkpoint。handler 失败或进程在 apply 与 commit 之间崩溃时，事件会被
再次投递，因此合同是 at-least-once；handler 必须以 `event_id` 做幂等键。

这不是 durable checkpoint 的最终实现，也没有宣称已经具备 inbox/outbox、dead-letter、provider
delivery 或生产 worker 门禁。当前代码是可由 PostgreSQL、SQLite 或测试 fake 实现的 port；下一阶段
才会把 checkpoint/inbox/outbox 落到 PostgreSQL，并补 crash/reconcile 证据。

Notion 本阶段保持 `local_pending`。本地报告、代码、测试和 Git 先收口，之后批量上传并逐页回读。
没有向飞书、企微、任何群聊或外部 provider 发消息。

## 代码位置与 API

代码位于 `apps/im-api/internal/events/projection.go`，测试位于
`apps/im-api/internal/events/projection_test.go`。

### 精确 scope

```go
type ProjectionScope struct {
    TenantID    string
    WorkspaceID *string // nil 是 root workspace，不是通配符
    ProjectionID string
}
```

`TenantID`、可选 `WorkspaceID` 和 `ProjectionID` 均必须通过既有 identifier 合同。不同投影 ID
不能互相推进 checkpoint；root workspace 与命名 workspace 也严格区分。

### checkpoint

```go
type ProjectionCheckpoint struct {
    Scope       ProjectionScope
    Position    uint64
    Cursor      Cursor
    LastEventID string
}
```

零 checkpoint 表示尚未消费任何事件，三项进度字段必须同时为空/为零。非零 checkpoint 必须同时
包含 opaque cursor 和合法 `LastEventID`。checkpoint store 的接口要求原子比较完整旧值后写入新值：

```go
LoadProjectionCheckpoint(ctx, scope) (ProjectionCheckpoint, error)
CommitProjectionCheckpoint(ctx, previous, next) error
```

存储层可以用 SQL `UPDATE ... WHERE`、版本号或等价 CAS 实现；冲突统一暴露为
`ErrProjectionCheckpointConflict`，未知/不可用的后端错误映射为 `ErrProjectionStoreUnavailable`。

## replay 与提交语义

`NewProjector` 接收一个 `EventStore`、checkpoint store、handler 和 page size（0 使用默认 64，最大
不超过事件 store 的 256）。`Run` 的流程是：

1. 检查 context、scope 和 typed-nil 依赖；读取 scope 对应 checkpoint。
2. 用 checkpoint cursor 调用 `ReadGlobalPage`，只读取同一 tenant/workspace 的 global event。
3. 校验 page 非空时 cursor 必须前进，事件必须匹配 scope、global position 严格递增且大于旧 position。
4. 顺序调用 handler。任意一个 handler 返回错误，整个 page 不提交 checkpoint。
5. 整页成功后生成 `Position`、`Next cursor`、`LastEventID`，校验后 CAS 提交。
6. `HasMore=false` 时返回 caught-up；空页必须返回原 cursor 且不能伪造 `HasMore=true`。

因此 page size 是重复投递窗口的显式运维参数：`1` 提供最小重复窗口，较大的 page 提高吞吐但在
crash/lost-ACK 后可能重放整页。任何外部副作用（发消息、调用 provider、写对象存储）不应直接放在
这个同步 commit 路径；应由幂等 inbox/outbox 或独立 action receipt 平面承接。

## 测试证据

新增纯内存合同测试覆盖：

- page size 为 1 的多页 backfill、global position/last event/cursor 和 commit 次数；已追平 rerun
  不重复处理；
- page 中第二个事件 handler 失败时 checkpoint 保持初始值，修复后 rerun 会再次收到第一个事件，
  明确 at-least-once 语义；
- checkpoint CAS conflict 原样返回，其他 checkpoint store 故障映射为 unavailable；
- tenant scope、零 position+非空 cursor、非零 position+缺 cursor/last event 等损坏 checkpoint
  在读取 event 之前 fail-closed；
- context cancellation、非法 scope、超出最大 page size 和 nil 依赖拒绝。

本轮命令（均通过）：

```text
cd /Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_wanwork_quantum_entanglement/apps/im-api
go test ./internal/events
go test ./...
go test -race ./...
go vet ./...
```

需要 PostgreSQL 的集成测试仍按既有本机临时 PostgreSQL 18.6 命令执行；本阶段没有把 volatile fake
误标为 durable，也没有访问 Notion、语雀、融云、Clerk、飞书或企微。

## 仍然打开的生产门禁

- PostgreSQL durable checkpoint 表、CAS、租户 RLS、重启恢复、commit-unknown reconcile；
- inbox 去重、outbox/action receipt、dead-letter、重试退避、消费 lag/容量/保留策略；
- page apply 与 checkpoint commit 的 kill-9/断电测试，以及 handler 幂等 golden tests；
- provider callback authenticity、Clerk session/membership PEP、result authority reserved fence；
- 备份恢复、跨区复制、schema expand/contract、观测指标和生产压测。

下一阶段顺序：先实现 PostgreSQL checkpoint/inbox/outbox 的 schema 与 repository，再以同一 projection
contract 接入 durable adapter；完成 raw-row、并发、重启和 commit-unknown 证据后，才评估 provider
delivery 与 IM worker promotion。真实 provider 出网和生产 worker 继续关闭。

## Git 与同步状态

本阶段代码提交已推送：

| Commit | 内容 |
|---|---|
| `5e9894a` | `feat(events): add provider-neutral projection contract` |

远端 ref：`origin/dev_wanwork_quantum_entanglement`。本报告及同步台账随后作为独立文档提交；Notion
目标页建议为 `research-43` 与 `current-implementation`，状态均为 `local_pending`。
