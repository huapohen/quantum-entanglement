# E3 continuation：scoped PURE worker lifecycle 与 lease race 证据

> 证据日期：2026-08-30（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 当前代码 HEAD：`859ae57`（实现节点为 `36cd0b4`，race 矩阵为 `025b5c7`）
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`；本地阶段结束后再批量同步并逐页回读

## 结论

本节点把此前分开的 scoped lease heartbeat、PURE/fake supervisor 和 result acceptance seam
组合成一个可关闭的进程内生命周期，并用双连接竞争测试验证其最小并发边界。它仍然是
default-off rehearsal：`HeartbeatPureWorkerGate.dispatch_enabled` 保持 `False`，没有打开
模型、插件、MCP、浏览器、connector、飞书、企微或任何外部网络效果。

实现分为三个独立提交：

| 提交 | 内容 | 可回滚点 |
|---|---|---|
| `36cd0b4` | `ScopedPureWorkerLifecycle`、store relinquish 时间因果修复、包导出与生命周期专项测试 | 删除该提交即可恢复到 supervisor-only seam |
| `a4196d3` | 排除 `RecoverySummary`，恢复 `store` 历史 wildcard API 表面 | 只影响兼容导出，不影响持久化语义 |
| `025b5c7` | 双连接 heartbeat/expiry 与 relinquish race 测试 | 只减少证据覆盖，不改变运行路径 |
| `3d8173f` | 生产合同与本证据链接文档 | 纯文档回滚 |

## 生命周期合同

`ScopedPureWorkerLifecycle` 暴露三个单调状态：

```text
ACCEPTING --close()--> DRAINING --bounded drain--> CLOSED
```

- admission 在 async lock 下登记；进入 `DRAINING` 后新 run 立即得到稳定的
  `PureWorkerLifecycleDrainingError`，`CLOSED` 后得到 `PureWorkerLifecycleClosedError`；
- 每个 run 都取得 claim、manifest、configuration 的 exact snapshot，并只把
  `PureWorkerContext` 交给 handler；context 不含 store、lease、connector、credential 或授权对象；
- `close(timeout_seconds)` 先停止 admission，再发出进程内取消信号，等待有界 drain；超时后
  hard-cancel 未完成的 supervisor task；重复 close 是幂等的；
- 结果只有 exact `AcceptedV2` 或 `ObservedV2` 才算成功；其他 outcome 在 `finally` 中调用
  store-owned `relinquish_scoped_invocation_start_v3()`；
- relinquish 在一个 SQLite transaction 内把 attempt 置为 `expired`、job 置为 `failed`，清空
  job owner/token/deadline/heartbeat，不追加 event/outbox，不创建 retry 或外部副作用；
- 若 clock 与最后 heartbeat 同一微秒，finished timestamp 向前推进一个可表示微秒 tick，保持
  `finished_at > heartbeat_at` 的持久因果不变量。

## 测试证据

可复现命令：

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_scoped_lease_lifecycle.py \
  tests/test_invocation_worker_lifecycle.py \
  tests/test_result_acceptance_worker_integration.py
.venv/bin/ruff check \
  src/quantum_entanglement/invocation_worker_lifecycle.py \
  src/quantum_entanglement/store.py \
  tests/test_scoped_lease_lifecycle.py \
  tests/test_invocation_worker_lifecycle.py
```

结果：**13 项通过**；Ruff 通过。

覆盖内容：

1. 首 heartbeat 后 handler 才能运行，且 heartbeat 贯穿 result acceptance；
2. exact result request + store acceptance 返回 `ACCEPTED` 后 job 为 `SUCCEEDED`；
3. graceful close 停止新 admission，取消 handler，有界 drain，最终 relinquish 为
   `FAILED/EXPIRED`；
4. 不合作 handler 在 bounded drain 后被 hard-cancel，仍不会进入 result acceptance；
5. supervisor task 自身取消时不会遗留 live handler；
6. heartbeat 返回 `False` 时 handler 被取消，结果被丢弃，active lease 被 relinquish；
7. relinquish 抛出 store error 时 active-run bookkeeping 仍释放，不会阻塞后续 close；
8. heartbeat/expiry 在两个 SQLite connection 上竞争时，最终只可能是：
   - heartbeat 先提交：job/attempt 仍 `RUNNING` 且 deadline 延长，expiry recovery 为 0；或
   - expiry 先提交：job 为 `FAILED`、attempt 为 `EXPIRED`，heartbeat 返回 `False`；
9. 两个 connection 同时 relinquish 时恰好一个返回 `True`、另一个返回 `False`，持久状态只有
   一个完整 terminal fence；
10. scoped start event 的 stream version 不因 heartbeat/relinquish 改写；
11. 重复 relinquish、失效 claim、非法 limit 和非法输入均 fail closed。

此前的结果 acceptance、SIGKILL、双进程 claim、backup/restore 证据仍见
[`41_result_acceptance_worker_integration_evidence.md`](./41_result_acceptance_worker_integration_evidence.md)、
[`38_result_acceptance_process_kill_matrix_evidence.md`](./38_result_acceptance_process_kill_matrix_evidence.md)
和 [`40_mainline_full_regression_latest.md`](./40_mainline_full_regression_latest.md)。本节点没有
替代那些矩阵，也没有声称完成全系统 crash-at-every-boundary 或生产 composition。

## 仍未关闭的门禁

- 全系统 process-kill/two-process result acceptance 与 compatibility/rollback 证据；
- 可信认证入口和所有 repository 的 tenant/workspace predicate；
- handler revision 闭合 allowlist、spawn/exec-before-secret-load、资源沙箱；
- service composition、API、observability、容量/SLO、clean-host release；
- 真实 IM provider contract、测试 scope/profile/mapper 与 production exchange；
- action receipt、connector ACK 与 `succeeded | rejected | effect_unknown` 外部副作用状态机。

因此本证据只能支持“进程内 PURE/fake rehearsal 的生命周期和 lease 竞争边界已覆盖”，不能支持
“worker 已可生产启用”或“可以接入真实 IM”。
