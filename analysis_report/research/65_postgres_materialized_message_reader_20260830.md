# PostgreSQL materialized message reader adapter（2026-08-30）

## 状态

提交：`fde9bb6 feat(im): add inactive postgres materialized message reader`；schema migration：`e5acef0`（migration 11）。

这是可切换的只读 adapter 候选，当前不接入默认 runtime。`message_projection_heads` 与
`message_snapshots` 已由 migration 11 注册并受 RLS/access-manifest 约束，但 projector writer、
checkpoint 同事务和 shadow equality 尚未闭合；默认路径仍是 `f64ee99` 的 bounded event replay。

## 已实现的合同

- 只接受 attested `runtimepool.Pool`，不能注入 owner/migrator/raw pool；
- `RepeatableRead + ReadOnly` 事务内绑定 exact tenant setting；
- 先读取 `messages-v1` projection head，再读取 tenant/workspace/conversation 范围内 snapshots；
- projection revision、sequence、last event position 和 row projection revision 必须一致且为正；
- 每行重新构造 `MessageSnapshot`，严格校验平台/客户消息 ID、Actor tenant、类型、状态、文本、UTC 时间和 revision；
- cursor 绑定 tenant、workspace presence/value、conversation、projection revision、createdAt、messageID；
- 旧 projection revision、跨租户/工作区/会话 cursor、非法 base64/时间/ID 均拒绝；
- `(created_at, message_id)` 稳定排序和 LIMIT+1 分页；没有 fallback、写入或 caller coordinates。

## 与最终 cutover 的关系

该 adapter 解决的是 materialized rows 的严格读取，不包含：

1. migration 11 已完成，但仍需真实 applied-schema digest/access-manifest integration evidence；
2. event projector 的 row CAS；
3. head 与 `event_projection_checkpoints` 同事务提交；
4. replay/materialized shadow equality canary；
5. crash/restore/rollback 和 old/new binary compatibility。

上述条件全部通过前，禁止把该 adapter 接入默认 composition，也不能把它称为 production IM readiness。

## 验证

```text
go test ./internal/platform/postgres/improjection ./internal/improjection ./internal/app  -> pass
go vet ./internal/platform/postgres/improjection                                  -> pass
git diff --check                                                                  -> pass
```
