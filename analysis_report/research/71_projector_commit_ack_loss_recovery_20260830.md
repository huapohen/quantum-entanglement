# Projector COMMIT ACK-loss recovery evidence（2026-08-30）

## 场景

在新增第 4 条消息事件后，测试注入一次性 commit hook：底层 PostgreSQL `COMMIT` 成功，但调用方
收到 synthetic acknowledgement loss。Projector 首次返回 `ErrStoreUnavailable`，不会伪造成功结果。

随后恢复正常 commit hook，从同一 runtime pool 重新运行 projector：checkpoint 已经是 position 4，
运行处理 0 个事件；materialized Reader 仍返回完整消息集合。之后再追加第 5 条消息并同时启动两个
runner，CAS 竞争完成且最终三条消息一致。关闭并重新打开 runtime pool 后，checkpoint position 5 和
三条消息仍可读。

## 代码约束

- `Projector` 使用内部 commit seam，生产默认实现直接调用 `pgx.Tx.Commit`；只在测试中注入 ACK-loss；
- 失败路径不把未知提交结果降级成“未提交”，而是要求下一次运行通过 checkpoint/row exact replay
  重新观察；
- migration-12 writer 的 exact existing-row/head 语义保证重复事件不会产生第二行或 revision 漂移；
- CAS 冲突只允许成功观察已提交状态或返回可重试错误，不能覆盖其他 runner 的 head。

## 验证命令

```bash
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55490/postgres?sslmode=disable' \
  go test ./internal/platform/postgres/improjection \
  -run TestPostgresMessageProjectorEndToEnd -count=1 -v
```

结果：`PASS`。这是隔离本机 PostgreSQL 18 evidence，不是目标生产集群的 RPO/RTO、HA 或 network
failure 证明。

## 未关闭边界

真实 SIGKILL 在提交前后的进程级矩阵、partial-write/rollback fault injection、shadow equality 长期
telemetry、生产 applied-schema/backup proof 和 materialized primary cutover 仍需单独 Gate。

代码提交：`d1af1a4`（已推送）；与 `research/70` 的 shadow seam 一起保持 primary replay default。
