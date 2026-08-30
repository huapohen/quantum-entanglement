# Projector 子进程 SIGKILL 提交边界矩阵（2026-08-31）

## 结论

`cb471d1` 为 PostgreSQL message projector 增加了真实子进程故障测试，补齐了此前 fault
matrix 中唯一尚未有独立进程证据的两个提交边界：

| 边界 | 子进程动作 | 新进程观察 | 结论 |
| --- | --- | --- | --- |
| COMMIT 前 | page transaction 已写入 snapshot/head/checkpoint，但在 `transaction.Commit` 前写入 marker，随后由父进程发送真实 `SIGKILL` | 新 projector 从原始 cursor 重放 1 个事件；Reader 只看到 1 行、`projection_revision=1` | 未留下 prefix；整页 rollback 后可恢复 |
| COMMIT 后、调用方 ACK 前 | 子进程先成功执行 `transaction.Commit`，再写入 marker，随后由父进程发送真实 `SIGKILL` | 新 projector 从已提交 checkpoint 读取，`Processed=0`、position 保持 2；Reader 看到两行且无重复 | 已提交图完整可见；重跑是 exact no-op |

这不是对生产进程增加 kill/debug 开关：故障钩子只存在于 opt-in Go test binary 的 helper
test 中，生产 `Projector` API 和运行时配置没有变化。

## 实现要点

- 父测试先通过与正常端到端测试相同的隔离 authority graph 建库、应用 migrations 1–12、
  建立 runtime-only pool 并追加事件。
- 子进程使用 `os.Args[0]` 启动当前测试二进制，以环境变量传递临时 DSN、脱敏的 authority
  manifest、tenant/workspace scope 和临时 marker 路径；manifest 不含凭据。
- `Projector.commit` 仅在 helper 进程内注入：
  - `pre`：marker `fsync` 后保持阻塞，父进程确认边界后 `Process.Kill()`；
  - `post`：先执行真实 `Commit`，再 marker `fsync` 并保持阻塞，父进程确认边界后 kill。
- 父进程等待子进程退出，并断言 `wait status` 确实为 `SIGKILL`，而不是普通测试失败或
  正常退出。
- 每个边界后都通过新的 projector run 和 materialized Reader readback 检查 rows、head、
  checkpoint position、projection revision 与消息文本；第二个边界额外验证重复运行不
  产生第二行。

## 复现命令

测试是 opt-in 的，未提供管理连接时自动 skip：

```bash
cd apps/im-api
WANWORK_TEST_POSTGRES_ADMIN_URL='<isolated-local-admin-url>' \
  go test ./internal/platform/postgres/improjection \
  -run '^TestPostgresMessageProjectorSIGKILLBoundaries$' -count=1 -v
```

本机隔离 PostgreSQL 18.6 实跑结果：

```text
=== RUN   TestPostgresMessageProjectorSIGKILLBoundaries
=== RUN   TestPostgresMessageProjectorSIGKILLHelper
=== RUN   TestPostgresMessageProjectorSIGKILLHelper
--- PASS: TestPostgresMessageProjectorSIGKILLBoundaries (0.26s)
PASS
```

同包无管理连接时：

```text
--- SKIP: TestPostgresMessageProjectorSIGKILLBoundaries (WANWORK_TEST_POSTGRES_ADMIN_URL is not set)
```

完整回归仍按影响面执行；本节点提交后至少复跑了 `improjection` 包的 focused test、
`git diff --check`，并确认 helper 不会在默认无环境的测试运行中启动。

## 故障语义与剩余边界

这两条证据证明 PostgreSQL 事务边界在真实进程死亡下保持“旧图或完整新图”，但不等于生产
切换批准。以下仍需独立完成：

1. 生产级 applied-schema、权限、备份、RPO/RTO 与 HA 证明；
2. 更底层的 partial-write fault injection（包括 owner function 返回异常、连接断开、网络
   分片/超时）及可执行 rollback/reconcile runbook；
3. shadow equality 长期 telemetry、告警与 backfill orchestration；
4. materialized Reader primary cutover、旧 reader drain 与可回滚 receipt；
5. Task/Artifact/Needs You durable projection、production worker/provider bridge、
   `effect_unknown` reconcile、真实 Clerk/JWKS 与 Gate A–E 晋级。

真实 IM provider、飞书、企微和所有 outbound 仍未连接或启用。

