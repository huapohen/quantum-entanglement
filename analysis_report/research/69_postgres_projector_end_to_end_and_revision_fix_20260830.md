# PostgreSQL message projector 端到端闭环与 revision 修复（2026-08-30）

## 结论

本节点完成了真实 PostgreSQL 18 临时库上的 runtime-only 闭环验证：

`EventStore append → Serializable Projector.Run → migration-12 owner function →
message_projection_heads/message_snapshots → materialized Reader.ReadPage`。

验证同时覆盖分页跨页、非消息事件推进 stream watermark、编辑 reducer、精确重跑、新事件后的
双实例并发竞争，以及关闭/重新打开 runtime pool 后的 checkpoint 与 reader readback。没有启用真实
IM provider、外发或公网监听。

## 端到端场景

测试写入同一 `cnv_room` 流的三个事件：

1. `message.created`：`msg_1 = before`；
2. `conversation.updated`：不改变消息，但必须推进流序号/head；
3. `message.edited`：`msg_1 = after`。

Projector 使用 page size 2，实际跨两个 Serializable transaction 完成 3 个事件。Reader 随后
只返回一个 `edited/after` 快照，`ProjectionRevision=3`。追加第 4 个 `message.created` 后，两个
runner 以 page size 1 并发竞争；一个 CAS 获胜，另一个要么观察已提交 checkpoint、要么得到可重试
冲突，最终两条消息和 `ProjectionRevision=4` 均可读。关闭并重新打开 runtime pool 后，projector
处理 0 个事件、checkpoint position=4，reader 仍返回完整两条消息。

## 真实缺陷与修复

此前 `loadConversationState` 与 `Reader` 把每条消息的 `projection_revision` 错误要求为等于
conversation head revision。合法的非消息事件会推进 head，但不会改写消息行，因此该条件会在
跨页恢复时把合法状态报为 integrity failure。

现改为：

- 消息行 `0 < projection_revision <= head.current_revision`；
- `last_event_sequence <= head.current_sequence`、`last_event_position <= head.current_global_position`；
- projection/head 自身仍要求正数、单调、同一 tenant/workspace/conversation。

这保持了对未来/跨 scope/负数/空坐标的 fail-closed，同时允许只推进 watermark 的事件。

## 可复现命令

```bash
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55489/postgres?sslmode=disable' \
  go test ./internal/platform/postgres/improjection \
  -run TestPostgresMessageProjectorEndToEnd -count=1 -v
```

记录结果：`PASS`（含并发竞争与 restart readback）。完整迁移、runtime authority、eventstore、
imstore 矩阵也已在同一隔离 PG18 实例通过。

## 边界

本节点只证明本机临时 PostgreSQL 的代码路径和权限 fixture；不等同目标生产集群的 schema digest、
备份、RPO/RTO、HA、真实认证、Task/Artifact/Needs You projection 或 IM provider readiness。
materialized reader 仍未切为默认 primary，shadow comparator 仍未接入 runtime composition。

代码提交：`1e8fc38`（已推送 `origin/mainline_continue_quantum_entanglement`）。
