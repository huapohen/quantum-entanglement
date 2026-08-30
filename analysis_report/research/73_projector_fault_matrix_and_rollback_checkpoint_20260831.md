# Projector fault matrix 与 rollback checkpoint（2026-08-31）

## 本节点结论

PostgreSQL message projector 现在拥有两条独立的提交边界证据：

1. **提交前失败**：在 Serializable page transaction 完成 head、snapshot、checkpoint 写入后，
   于 COMMIT 前注入一次性错误；事务回滚，materialized Reader 观察到空 rows、空 head（revision 0），
   下一次从原始 cursor 重放整页并得到完整结果。
2. **COMMIT ACK 丢失**：底层 COMMIT 成功但调用方收到 synthetic acknowledgement loss；下一次运行
   从已提交 checkpoint exact replay，处理 0 个重复事件且不产生第二行，随后追加事件并通过双 runner
   CAS 竞争与 pool restart readback。

两条路径都保持 bounded EventStore replay 为业务 primary；materialized reader 和 shadow equality
仍是 opt-in candidate，不打开生产 cutover 或 outbound。

## 代码与提交

- `b1b1160`：`TestPostgresMessageProjectorEndToEnd` 增加提交前失败/整页 rollback/重试恢复矩阵。
- `e49d7ae`：更新 production readiness，明确 ACK-loss 不再列为未覆盖项，仍保留真实进程 crash、长期
  shadow telemetry、生产 applied-schema/backup proof 等关闭边界。
- `8b78c26`：SQLite migration-7 backup restore 后的 compatibility rollback guard；非空结果图拒绝
  downgrade，并验证 row count、foreign-key 与 integrity check 不变。
- `004d78f`：ScopedPureWorkerLifecycle checkpoint，包含 SIGKILL 后 expiry recovery 证据。
- `cb471d1`：真实 projector child-process SIGKILL 前/后提交边界矩阵；完整证据见
  [`research/74_projector_sigkill_process_matrix_20260831.md`](74_projector_sigkill_process_matrix_20260831.md)。

## 验证

```text
cd apps/im-api
go test ./internal/platform/postgres/improjection -count=1     PASS
go test ./internal/platform/postgres/improjection -run TestPostgresMessageProjectorEndToEnd -count=1 -v
  PASS（隔离 PostgreSQL 18.6，临时 loopback 实例）

WANWORK_TEST_POSTGRES_ADMIN_URL='<isolated-local-admin-url>' \
  go test ./internal/platform/postgres/migrations \
  ./internal/platform/postgres/runtimepool \
  ./internal/platform/postgres/eventstore \
  ./internal/platform/postgres/improjection \
  ./internal/platform/postgres/imstore -count=1
  PASS（5 包 PostgreSQL 18.6 integration matrix）

PYTHONPATH=src .venv/bin/pytest -q tests/test_result_compatibility_rollback.py  1 passed
.venv/bin/ruff check tests/test_result_compatibility_rollback.py                 All checks passed
git diff --check                                                         PASS
go test ./... && go vet ./...                                      PASS（Go 全模块）
```

真实 PG18 端到端命令仍保留在 `research/69` 与 `research/71`；本节点已在隔离 PostgreSQL 18.6
临时 loopback 实例复跑 projector fault matrix 及 migrations/runtimepool/eventstore/improjection/
imstore 五包 integration matrix。提交前失败、ACK-loss 与 child-process SIGKILL 测试均为 opt-in integration，不能
用 SQLite 或单元测试替代。凭据、端口和完整连接串不进入证据文件。

## 未关闭边界

- 生产级 applied-schema、权限、备份、RPO/RTO、HA 与 materialized primary cutover；
- rollback/partial-write 的生产故障注入与恢复 runbook；
- shadow equality 长期 telemetry、mismatch 告警与 backfill orchestration；
- 可回滚 cutover receipt、真实 Clerk/JWKS、Task/Artifact/Needs You durable
  projection、worker/provider bridge、action receipt 与 `effect_unknown` reconcile。

## 安全边界

本节点没有连接真实 IM provider，没有启用 outbound，没有访问飞书/企微，也没有在证据、日志或提交
中记录完整 API Key。所有 provider 与模型调用仍使用 fake/显式本地配置。
