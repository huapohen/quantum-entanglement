# PostgreSQL materialized message projector writer（2026-08-30）

## 本阶段结论

本阶段把 message projection 从“仅有 inactive reader”推进到可被运行时调用的
PostgreSQL materialized projector 候选：migration 12 注册两个 owner-only
`SECURITY DEFINER` 写函数，projector 在同一 Serializable transaction 中读取 EventStore、
写入 message head/snapshot，并 CAS 写入 projection checkpoint。代码已通过 Go 全模块
`test ./...` 与 `go vet ./...`；尚未宣称生产完成。

## 变更摘要

- `write_message_projection`：严格校验 tenant/session setting、projection id、sequence/
  position/revision 单调关系与消息字段；对 head 和 snapshot 执行锁定、幂等 replay 与 CAS，
  其他竞争状态返回 false，由调用方映射为 conflict。
- `advance_message_projection_head`：对非 message 事件只推进 durable head，保持消息行不变。
- `ReadGlobalPageTx`：EventStore global page 使用调用方 transaction，保证事件、materialized
  rows 和 checkpoint 共享同一 PostgreSQL snapshot。
- `MessageProjection`：增加从已提交 snapshots 恢复、sequence watermark 观察和 detached
  snapshot 读取；`message.created/edited/recalled` 仍由 provider-neutral reducer 严格处理。
- `platform/postgres/improjection.Projector`：按有界 page drain；恢复 conversation state，
  逐事件 owner-function 写入，最后 CAS checkpoint。事务提交失败时整体回滚，重试允许 exact replay。

## 验证

```text
go test ./internal/platform/postgres/migrations ./internal/improjection \
  ./internal/platform/postgres/eventstore ./internal/platform/postgres/improjection \
  ./internal/platform/postgres/authoritycutover -count=1   PASS
go test ./...                                             PASS
go vet ./...                                              PASS
git diff --check                                          PASS
```

## 仍未关闭的门禁

该实现仍是 production composition 前的候选，需要真实 PostgreSQL applied-schema integration
evidence、crash/restore 与双 projector 竞争证据、shadow replay/materialized equality、
Task/Artifact/Needs You durable projection、真实 Clerk/JWKS、worker/provider bridge、action
receipt reconcile 以及 Gate A–E。默认业务读取仍不应在没有 cutover 证据时切换到 materialized reader。

## 安全边界

本阶段没有连接真实 IM provider，没有发送飞书/企微/群聊消息，没有启用 outbound，也没有记录
任何完整 API key。运行 projector 前必须由可信认证 composition 设置 tenant GUC，并由 migration
access manifest 授予最小 runtime 权限。
