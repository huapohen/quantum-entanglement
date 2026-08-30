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
- `improjection.CompareMessageReaders`：以各自 opaque cursor 完整分页，对 replay/materialized
  的 ordered message snapshots 做严格字段比较；不把 projection generation 与 stream version
  混比，也不将 mismatch 降级为 fallback。

## 验证

```text
go test ./internal/platform/postgres/migrations ./internal/improjection \
  ./internal/platform/postgres/eventstore ./internal/platform/postgres/improjection \
  ./internal/platform/postgres/authoritycutover -count=1   PASS
go test ./...                                             PASS
go vet ./...                                              PASS
git diff --check                                          PASS
```

新增 shadow comparator 的单测覆盖：相同消息行（不同 projection revision）通过、页面元数据
漂移失败、非空跨实现 cursor 拒绝。

随后审计到 migration 12 对旧 PostgreSQL integration fixture 的影响：fresh/repeat、并发 migrator、
conversation authority 与 identity fixture 原先仍断言 10 个 migration，已统一修正为 12；这样在
设置 `WANWORK_TEST_POSTGRES_ADMIN_URL` 的真实集成环境中不会因旧数量断言产生假失败。

最后修复 message ID 提取器的字段边界：created payload 由 reducer 负责 strict schema 校验，提取器
只读取已校验对象中的 `messageId`，允许同一合法对象的其他字段，并拒绝缺失 ID；避免首条
`message.created` 在进入 owner SQL function 前被错误拦截。

## 真实 PostgreSQL integration readback

使用本机隔离 PostgreSQL 18 测试实例（仅临时测试数据库，无业务数据）运行：

```text
WANWORK_TEST_POSTGRES_ADMIN_URL=<local-test-admin-dsn> \
go test ./internal/platform/postgres/migrations \
  -run TestApplyAgainstPostgres -count=1   PASS
```

覆盖结果包括 migration 1–12 fresh/repeat、migration-12 writer function definition digest、旧
migration postcondition、防 hostile search_path、checksum/future ledger、RLS/constraint/index/
grant/default 漂移、双 migrator serialization、锁超时和 panic quarantine。该证据证明 migration
12 在记录的 PostgreSQL 18 schema 上可应用且 postcondition 可回读；不等于生产集群批准或 projector
crash/restore 完成。

同一隔离实例上的 `TestRuntimePoolAgainstPostgres` 也已通过：runtime relation inventory 从 28
校准为 30，message projection 两张表已纳入最小 SELECT manifest，function inventory 从 8 校准为
10；reset role、search_path、tenant setting、transaction、advisory lock、LISTEN 污染和 access
drift 全部保持 fail-closed/recovery 语义。

## 仍未关闭的门禁

该实现仍是 production composition 前的候选，需要真实 PostgreSQL applied-schema integration
evidence、crash/restore 与双 projector 竞争证据、shadow replay/materialized equality、
Task/Artifact/Needs You durable projection、真实 Clerk/JWKS、worker/provider bridge、action
receipt reconcile 以及 Gate A–E。默认业务读取仍不应在没有 cutover 证据时切换到 materialized reader。

## 安全边界

本阶段没有连接真实 IM provider，没有发送飞书/企微/群聊消息，没有启用 outbound，也没有记录
任何完整 API key。运行 projector 前必须由可信认证 composition 设置 tenant GUC，并由 migration
access manifest 授予最小 runtime 权限。
