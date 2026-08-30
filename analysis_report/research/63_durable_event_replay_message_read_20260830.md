# PostgreSQL durable event-replay 消息读取节点（2026-08-30）

## 结论

提交 `f64ee99` 把 PostgreSQL durable EventStore 与认证消息读取 seam 接入同一生产 composition。
当前实现从 durable conversation stream 有界重放消息事件，提供可以实际装配的读取路径；它不是
最终的 materialized message heads/snapshots，也不声称完成高吞吐投影。

## 已实现

- `message.created`、`message.edited`、`message.recalled` 通过严格 reducer 重放；
- 非消息事件不进入消息 reducer，但仍计入 stream version；
- 单次读取最多重放 4,096 个事件，超过上限返回 dependency-unavailable，不做无界扫描；
- EventStore 分页固定每页 256，空页但 `hasMore`、游标不前进、sequence 回退均失败关闭；
- 消息分页游标绑定 tenant、workspace presence/value、conversation 和 exact stream version；
- 流在两页之间变化时，旧游标返回 revision conflict，客户端必须重新开始；
- malformed/cross-scope cursor 拒绝；空 stream 返回合法空页，projection revision 为 0；
- route 重新核对 repository 返回的 conversation revision，拒绝非零但不匹配的伪造页；
- PostgreSQL `cmd/im-api` composition 同时注入 EventStore 与 MessageReadRepository；
- 全路径只读，不启动 provider、outbound、飞书、企微或真实 IM。

## 验证

```text
go test ./internal/improjection -run EventReplayMessageReader -count=1  -> pass
go test ./...                                                           -> pass
go vet ./...                                                            -> pass
./scripts/verify_web_first.sh                                           -> pass
git diff --check                                                        -> pass
```

专项覆盖稳定两页、cursor drift、malformed cursor、空 stream 和 nil dependency。前一节点
`a3889e2` 已完成全语言阶段封板；本提交只执行 Go 影响面门禁。

## 尚未完成

1. PostgreSQL materialized message heads/snapshots；
2. projection write 与 checkpoint 同事务提交；
3. authority snapshot 与 EventStore/replay snapshot 的同事务绑定；
4. 超过 4,096 事件的后台 projector/compaction；
5. real Clerk/JWKS 与 production credential rotation；
6. crash/restore/rollback compatibility matrix。

因此该节点把“503 未装配”推进到“durable source 可读且有界失败关闭”，但 Gate A–E 仍保持关闭。
