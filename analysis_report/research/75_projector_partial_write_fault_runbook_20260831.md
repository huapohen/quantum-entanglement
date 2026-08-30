# Projector partial-write fault injection 与恢复 runbook（2026-08-31）

## 目的

验证 message projector 在“同一页前面的 row/head 已写入，但后续 snapshot 写入失败”时不会
发布半页。故障必须表现为数据库事务异常，而不是仅由 Go 层在 COMMIT 前返回一个 synthetic
error；恢复时必须从未推进的 projection cursor 重放完整页面。

## 已交付证据

- 提交：`595f034 test(im): inject projector partial write rollback`。
- 测试：`TestPostgresMessageProjectorPartialWriteRollback`，仅在设置
  `WANWORK_TEST_POSTGRES_ADMIN_URL` 时运行。
- 注入方式：测试使用 owner connection 临时创建 `BEFORE INSERT OR UPDATE` trigger；当第二个
  message snapshot 的 `message_id` 命中测试值时，trigger 抛出 PostgreSQL `P0001` 异常。
  第一个 snapshot 已经由 owner-only `write_message_projection` 成功写入，但异常仍发生在同一
  Serializable page transaction 内。
- 观察方式：runtime-only projector 收到 `ErrStoreUnavailable`；materialized Reader 随后读到
  0 行、`projection_revision=0`；删除 trigger/function 后重新运行，`Processed=2`、checkpoint
  position=2，Reader 读到两行且顺序和文本完整。

## 复现

```bash
cd apps/im-api
WANWORK_TEST_POSTGRES_ADMIN_URL='<isolated-local-admin-url>' \
  go test ./internal/platform/postgres/improjection \
  -run '^TestPostgresMessageProjectorPartialWriteRollback$' -count=1 -v
```

预期输出：

```text
=== RUN   TestPostgresMessageProjectorPartialWriteRollback
--- PASS: TestPostgresMessageProjectorPartialWriteRollback (...s)
PASS
```

该测试自动在临时隔离数据库中应用 migration 1–12，并在 cleanup 中删除测试 trigger/function；
不会修改生产数据库，也不会连接真实 IM provider。

## 线上故障处置顺序（草案）

1. **冻结晋级**：保持 materialized primary 和 shadow promotion 关闭；bounded EventStore replay
   继续作为业务读取路径。不要手工修改 checkpoint、head 或 snapshot rows。
2. **判定边界**：记录 tenant/workspace/projection scope、projector attempt、最后已知
   checkpoint position/cursor，并从只读连接检查 rows/head 的一致性；禁止把单条 row 存在误判为
   已提交页。
3. **隔离写入**：停止故障 projector owner，保留数据库和日志证据；若连接/事务 outcome 不明确，
   将实例标记为 reconcile-only，不让调用方返回成功。
4. **恢复新 owner**：使用新的 runtime pool/进程，先通过 authority/access manifest readiness，
   再从 durable checkpoint 读取 cursor。只让 projector 按原 page size 重放，禁止从业务 row
   反推 cursor。
5. **校验结果**：对 checkpoint、每个 conversation head、snapshot 数量/coordinates、reader
   cursor 与 shadow replay 做逐 scope 对账；任何 digest、scope、sequence、revision 或 row
   geometry 漂移都 fail closed。
6. **解除隔离**：只有旧图或完整新图二选一、重跑达到 exact no-op、backfill/shadow 指标稳定后，
   才能提交单独的 cutover receipt；该 receipt 仍需人工审批。

## 注入边界与限制

当前实现证明了数据库异常发生在页内首个写入之后的 rollback 语义，但没有宣称覆盖所有生产
故障：

- owner function 内部 OOM、主机断电、磁盘满、WAL/存储损坏、网络代理分片、连接池驱逐与
  PostgreSQL failover 仍需在专用 staging 做演练；
- trigger 注入不应进入生产 schema，真实生产故障注入必须使用隔离副本、可回滚变更和变更审批；
- applied-schema digest、RPO/RTO、备份恢复、HA/failover 与长期 shadow telemetry 仍是独立
  release gates；
- 真实 IM、飞书、企微和 outbound 始终关闭。

