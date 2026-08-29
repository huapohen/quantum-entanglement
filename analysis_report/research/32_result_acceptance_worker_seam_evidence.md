# E3 Result Authority：AcceptedV2 与 heartbeat acceptance seam 证据

> 证据日期：2026-08-29（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 当前 worker-seam 提交：`7bed2b6`；result-only projection 候选：`69fbcb6`；projection process
> binding follow-up：`a014bc5`；identity-binding hardening：`a1eb218`
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`；本地任务完成后批量上传并逐页回读

## 结论

本阶段把“结果已写入”和“调用方收到 fresh COMMIT ACK”明确分成两个分类：

1. `SQLiteEventStore.accept_scoped_invocation_result_v2(request, claimed)` 在显式开启
   migration 7 的候选 store 内执行完整结果图事务。只有本次调用完成全部 DML、owner
   readback 且正常收到 `COMMIT` ACK 时，才返回当前进程内不可复制、不可 pickle 的
   `ScopedInvocationResultAcceptedV2`。
2. 已存在的完整结果图、重放、重开或恢复路径只返回 capability-free
   `ScopedInvocationResultObservedV2`，不会把 readback 重新分类为 Accepted。
3. COMMIT 后 ACK 丢失会 poison 当前 store；关闭并重开后可以读回完整结果图，公开 API
   的重放结果仍为 Observed。确认 rollback 的 COMMIT 失败不会留下结果前缀。
4. `HeartbeatPureWorkerSupervisor` 在 handler 返回后如配置 acceptance callback，会继续
   保持 heartbeat fencing，直到 callback 得到 Accepted/Observed 或进入 lease-loss、取消、
   超时/异常的 fail-closed 分支。`run_and_accept()` 只接受 exact
   `ScopedInvocationResultAcceptanceRequestV2`，并将 supervisor 自己快照的 exact start
   claim 传给 acceptor，避免 handler 替换 invocation 或 lease。

这些是候选组合 seam，不是生产 worker promotion。migration 7 默认关闭；`HeartbeatPureWorkerGate`
仍以 argument-free disabled error 拒绝产品 dispatch；没有任何真实模型、浏览器、MCP、connector、
飞书、企微、语雀、Notion 或 outbound 调用。

## 代码与提交映射

| 提交 | 变化 | 事实边界 |
|---|---|---|
| `1766eec` | process-bound `AcceptedV2` 类型和 private mint guard | receipt 本身仍是 capability-free |
| `b505dfd` | store-owned atomic result acceptance API | migration 7 opt-in；fresh ACK 才 Accepted |
| `5a4491c` | supervisor acceptance callback 与 `run_and_accept()` | heartbeat 持续到 acceptance；product gate 仍关闭 |
| `7bed2b6` | public API ACK-loss/reopen/replay test | lost ACK 后 reopen/readback 只能 Observed |

## 验证命令与结果

在干净 worktree 中执行：

```bash
./.venv/bin/ruff check \
  src/quantum_entanglement/invocation_worker.py \
  tests/test_invocation_worker.py \
  tests/test_result_acceptance_api.py

PYTHONPATH=src ./.venv/bin/python -m pytest -q \
  tests/test_invocation_worker.py \
  tests/test_result_acceptance_api.py \
  tests/test_result_acceptance_preparation.py \
  tests/test_result_acceptance_durable_prerequisites.py
```

结果：Ruff 通过；四个 focused suites 全部通过。覆盖的关键断言包括：

- 首 heartbeat 失败时 handler 不会启动；续租失败、超时、取消和 late value 全部 fail closed；
- acceptance callback 运行期间 heartbeat 仍执行；callback 返回非 exact 类型时不会泄漏 raw
  handler value；
- `run_and_accept()` 拒绝普通字符串、subclass 或其他伪造 request，且 acceptor 不会被调用；
- fresh result acceptance 返回 exact `AcceptedV2`，其 receipt 不含明文 lease，且 wrapper 不可
  copy/deepcopy/pickle；
- exact replay 返回 `ObservedV2`；
- public API 注入 COMMIT ACK-loss 后 store 被隔离，重开读回相同 receipt，API replay 仍为
  `ObservedV2`；
- DML fault、确认 rollback、partial/drift/orphan、Artifact/事件/job/attempt 绑定和
  foreign-key/integrity readback 继续保持原有 fail-closed 断言。

## 仍未关闭的门禁

- candidate acceptance 还没有接入 `HeartbeatPureWorkerGate.dispatch` 的生产 composition；
- 业务 read-model projection 候选已把 result/terminal event 投影为
  `task_result_projection_v1` 的 tenant/workspace/invocation 作用域最小视图，但尚未通过可信
  RequestContext、对外 API、跨 tenant property、process/kill/双连接和生产 composition 门禁；
- process-kill、双连接竞争、clean-host restore/replay 和长时 heartbeat 的完整 E2E 仍待补齐；
- publication/outbox 仍刻意为零，真实 IM、飞书、企微和任何外部副作用保持关闭；
- compatibility/rollback、部署、容量、SLO、RPO/RTO 和 Gate A–E 仍未通过。

## 远端文档策略

本文件先进入本地 Markdown、Git commit 和远端分支备份。按照用户已确认的速度优先决策，开发
期间不逐阶段写入 Notion；等本地阶段性任务全部完成，再批量同步正文、证据与截图，逐页回读
并更新 `analysis_report/notion_sync_manifest.json`。本文件未完成那次批量回读前，不声明 Notion
同步闭环。
