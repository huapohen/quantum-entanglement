# E3 Result Authority：COMMIT 后 ACK 前 SIGKILL 证据

> 证据日期：2026-08-29（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 证据提交：`8720586`  
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`；本地阶段完成后批量上传并逐页回读

## 结论

本阶段覆盖一个与“提交前回滚”完全不同的进程边界：child 在 SQLite `COMMIT` 成功返回后、
`accept_scoped_invocation_result_v2()` 能向调用方返回 `AcceptedV2` 之前，被真实 `SIGKILL`。

重开数据库后：

- 完整 result graph 保留，receipt、result/terminal event、Artifact 与 job/attempt terminal
  CAS 均可读回；
- `read_scoped_invocation_result_observed_v2()` 返回 exact `ObservedV2`；
- 使用相同 request/claim 重放公开 API 仍返回 `ObservedV2`，receipt 与首次观察完全一致；
- durable receipt 只有 1 行，job/attempt 状态均为 `succeeded`。

因此当前语义正确区分“已提交但 ACK 未知”和“本次 fresh COMMIT ACK 已确认”：进程重启或
重放绝不会把已提交图重新升级成 `AcceptedV2`，也不会重复写入结果 authority。

## 测试实现

测试文件：[`tests/test_result_acceptance_process_commit_kill.py`](../../tests/test_result_acceptance_process_commit_kill.py)

父进程建立合法 scoped start claim 并把 exact request 快照写入 JSON。child 在新 `spawn`
解释器中重建 request/claim，把 `_execute_transaction_control` 替换为测试 hook；hook 先调用
真实 `COMMIT`，仅在 statement 恰为 `COMMIT` 时发送 `os.kill(os.getpid(), SIGKILL)`。hook 不
执行 Python rollback/close，避免把真实 ACK-loss 窗口改写成普通异常路径。

父进程等待 child 返回 `-SIGKILL` 后，以新的 `SQLiteEventStore` connection 执行观察和 API
重放，断言 receipt identity、投影数据和终态状态一致。测试使用的 token、identity 和 Artifact
均为合成值，没有模型、浏览器、MCP、connector、网络、飞书、企微、语雀或 Notion 调用。

## 可复现命令与结果

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_result_acceptance_process_commit_kill.py
.venv/bin/ruff check tests/test_result_acceptance_process_commit_kill.py
git diff --check
```

结果：测试通过；Ruff 与 diff-check 通过。

## 故障语义矩阵增量

| 位置 | 持久结果 | 对外分类 |
| --- | --- | --- |
| DML/Artifact/event/CAS 前，`COMMIT` 前 SIGKILL | 全图回滚、无 authority 前缀 | 无 Accepted；受上层 retry policy 约束 |
| `COMMIT` 已成功，返回 ACK 前 SIGKILL | 完整图保留 | 重开/重放只能 `ObservedV2` |
| driver 报告 COMMIT ACK-loss（未实际 kill） | 既有图保留，当前 store poison | 关闭并重开后 `ObservedV2` |

这条证据仍只覆盖 result acceptance owner transaction，不覆盖 connector 外部副作用、跨主机
文件系统故障、driver 崩溃恢复、全系统 dispatch/heartbeat/lease-expiry kill matrix、兼容回滚、
容量/SLO、可信认证 composition 或生产 worker promotion。`HeartbeatPureWorkerGate` 及真实
IM/外部 outbound 继续 default-off。

## 本地优先同步策略

本文先写入本地 Markdown、Git commit 和远端分支备份。Notion 保持 `local_pending`，待本地阶段
全部完成后批量同步正文、证据和截图，并逐页回读更新
`analysis_report/notion_sync_manifest.json`；回读完成前不声明 Notion 同步闭环。
