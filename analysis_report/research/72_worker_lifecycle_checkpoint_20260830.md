# v0 版 worker lifecycle checkpoint（2026-08-30）

> 分支：`mainline_continue_quantum_entanglement`  
> 记录节点：`d3f2831`（本证据提交之后的代码仍未改变 worker lifecycle）  
> Notion 状态：`local_pending`

## 当前结论

本 checkpoint 只复核进程内 PURE/fake worker 的生命周期，不打开产品 dispatch。当前
`HeartbeatPureWorkerGate.dispatch_enabled` 仍为 `False`；`ScopedPureWorkerLifecycle` 仍是
私有 rehearsal composition，负责停止 admission、传播取消、执行有界 drain，并通过
`SQLiteEventStore.relinquish_scoped_invocation_start_v3()` 释放未成功结果的 lease。真实模型、
connector、MCP、浏览器、IM、飞书、企微以及任何 outbound 均不在该路径中。

生产合同与门禁的权威说明仍在
[`HEARTBEAT_SUPERVISED_PURE_WORKER.md`](../../docs/production/HEARTBEAT_SUPERVISED_PURE_WORKER.md)：
生命周期状态为 `ACCEPTING -> DRAINING -> CLOSED`，只有 exact `AcceptedV2` 或 `ObservedV2`
才会被视为成功，其余状态（stale、timeout、cancel、handler failure 和 acceptance failure）
都不得成功，并进入 store-owned relinquish/reconcile 路径。

## 本地验证

在仓库根目录执行：

```bash
PYTHONPATH=src python -m pytest -q \
  tests/test_invocation_worker_lifecycle.py \
  tests/test_invocation_worker_lifecycle_process.py
```

结果：**7 项通过**（包括 SIGKILL 后 expiry recovery）。本次验证未调用模型、网络、IM
或任何外部消息通道。

覆盖的关键边界：

1. 首 heartbeat 成功后 handler 才能开始，heartbeat 会持续到 acceptance；
2. graceful close 先停止新 admission，再取消 active run，并在 bounded drain 后关闭；
3. 不合作 handler 在 drain deadline 后只被 hard-cancel，不能进入结果 acceptance；
4. heartbeat 丢失、超时、取消和 relinquish 异常都不会产生成功结果；
5. 进程在纯 handler 中被 SIGKILL 后，重开 store 只能按过期 lease recovery，不能接受结果；
6. `CLOSED`/`DRAINING` 状态拒绝新的 run，重复 close 保持幂等。

## 仍然不是生产启用证明

该 checkpoint 不能替代 full-system lease expiry/stale-worker/drain、完整 process-kill 与双进程
result race、可信认证与 tenant scope、handler allowlist/sandbox、service lifecycle、容量/SLO、
兼容回滚和真实 IM/provider 证据。产品 promotion gate 继续关闭，后续阶段需在同一分支逐门禁
完成并保持每项独立 commit。
