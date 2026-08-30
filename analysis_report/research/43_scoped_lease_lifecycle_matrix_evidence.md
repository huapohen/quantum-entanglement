# Scoped lease 生命周期门禁：现有进程/投影证据复核

> 复核日期：2026-08-30（Asia/Shanghai）  
> 复核分支：`scoped_lease_process_matrix`  
> 基线：`4fd6588`（`feat: add scoped lease heartbeat and expiry recovery`）  
> Notion 状态：`local_pending`；本地阶段完成后再批量同步并逐页回读

## 结论

本次复核确认，当前仓库已经有可复用的 projector 崩溃恢复和回滚语义测试；这些测试不应被
重复复制到 scoped lease 矩阵中。它们覆盖的是 projection-owned lease，而不是
`invocation_jobs`/`invocation_attempts` 的 scoped worker lease，因此只能作为底层 SQLite
进程级故障模型的旁证，不能替代 scoped lease 的 full-system 门禁。

## 已有可复用证据

### 双连接竞争与 owner fencing

`tests/test_result_projection.py::ResultProjectionTests.test_two_projection_connections_fence_competing_owner`
使用两个独立 `SQLiteResultProjectionStore` 连接并行运行 projector。第一个连接持有 lease 并
阻塞在 source，第二个连接立即收到 `ProjectionLeaseConflictError`；释放后第一个连接完成。
这证明 projection lease 的 owner fencing 是连接级原子的。

### projector 进程 SIGKILL 后恢复

`tests/test_result_projection.py::ResultProjectionTests.test_sigkill_after_lease_claim_is_recovered_by_new_owner`
启动一个全新 Python 进程，在 projector claim lease 后让 source 永久阻塞，再杀死该进程。等待
lease 到期后，新的 owner 成功取得 lease、重放全部事件并得到 `COMPLETED` projection。
该测试是跨进程 SIGKILL→lease expiry→reclaim 的可复用最小模型。

### 事务回滚/半成品清理

结果接受链路另有进程杀死与事务回滚矩阵：

- `tests/test_result_acceptance_process_kill_matrix.py` 覆盖 artifact、manifest、request、binding、
  receipt 和 terminal CAS 边界；每个边界重开后验证无半成品且原始 claim 可重新接受。
- `tests/test_result_acceptance_process_commit_kill.py` 覆盖 `COMMIT` 已落盘但 ACK 返回前被杀死，
  重开后只能得到 `ObservedV2`，不会重复签发 receipt。
- `tests/test_result_acceptance_process_recovery.py` 覆盖 artifact 写入阶段被杀死后的回滚与 fresh
  retry。

这些测试可复用相同的“独立进程、持久 SQLite、重开后只读验证”装具，但其业务表和 authority
语义不同，不能直接声称 scoped worker lease 已验证。

## scoped lease 当前已证明与未证明

当前 `tests/test_scoped_lease_lifecycle.py` 已证明同进程内：

1. heartbeat 在 job/attempt 两行上保持同一 owner、epoch、token digest，并且不重写 immutable
   start event；
2. 在 `lease_expires_at == now` 的边界 heartbeat 失败；
3. recovery 将过期的单次 scoped attempt 原子地 fence 为 `EXPIRED`，job 为 `FAILED`，清除 owner、
   token digest、deadline 和 heartbeat；
4. recovery 完成后旧 claim 不能再次 heartbeat。

仍缺少的 full-system 门禁是：

- 两个独立连接/进程同时 heartbeat 与 recovery 的竞争；
- worker 在 heartbeat 或 recovery 中途 SIGKILL 的结果；
- stale heartbeat、旧 epoch/token 和 recovery 的交错；
- graceful drain 与 lease expiry 的优先级和可观测结果；
- admission→claim→heartbeat→worker kill 的统一跨进程矩阵。

因此本复核不打开生产 worker，也不改变 `HeartbeatPureWorkerGate.dispatch()` 的 default-off
状态。真实模型、connector、IM、飞书、企微、语雀和 Notion outbound 仍保持关闭。

## 可复现命令

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_result_projection.py \
  tests/test_result_acceptance_process_kill_matrix.py \
  tests/test_result_acceptance_process_commit_kill.py \
  tests/test_result_acceptance_process_recovery.py \
  tests/test_scoped_lease_lifecycle.py
```

本文件是证据索引和边界声明；后续实现应在独立测试文件中补齐上述 scoped lease 跨进程矩阵，
并为每个故障边界单独提交、单独回归，再更新本文件的命令和结果。

