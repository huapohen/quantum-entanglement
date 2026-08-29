# E3 Result Authority：Heartbeat supervisor → store acceptance seam 证据

> 证据日期：2026-08-29（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 证据提交：`3081c58`  
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`；本地阶段完成后批量上传并逐页回读

## 结论

新增正向集成测试把三个已完成但此前分开的 seam 连接起来：

1. `HeartbeatPureWorkerGate.prepare_scoped_v3()` 只接受 exact scoped first-claim authority；
2. `HeartbeatPureWorkerSupervisor.run_and_accept()` 在 handler 返回和结果接受期间维持心跳；
3. handler 返回 exact `ScopedInvocationResultAcceptanceRequestV2` 后，supervisor 将自身快照的
   claim 交给 store-owned `accept_scoped_invocation_result_v2()`。

真实 opt-in SQLite store 返回 `AcceptedV2`，supervisor 最终 outcome 为 `ACCEPTED`，且至少执行
一次首 heartbeat。handler 只获得无 store/lease/connector 的 `PureWorkerContext`，不能替换
invocation 或 lease。该测试证明候选 seam 的类型和 authority 绑定没有断裂，但**没有启用**
`HeartbeatPureWorkerGate.dispatch()` 产品入口，也不代表允许模型、插件、浏览器、MCP、connector
或任何外部副作用。

## 测试文件与关键断言

测试文件：[`tests/test_result_acceptance_worker_integration.py`](../../tests/test_result_acceptance_worker_integration.py)

- 使用合成 `scoped_request()` 与 durable start claim；
- 配置有限的 lease/heartbeat/handler/drain 时间；
- heartbeat callback 只返回 `True`，不触碰外部系统；
- handler 断言 context 未取消并返回预先构造的 exact acceptance request；
- acceptor 断言 request 是 exact 对象、claim 类型精确，再把当前时间推进到有效窗口调用真实
  store acceptance API；
- 断言 `PureWorkerOutcome.ACCEPTED`、结果非空且心跳至少一次；
- store 在 `finally` 中关闭，未泄漏连接或锁。

## 可复现命令与结果

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_result_acceptance_worker_integration.py
.venv/bin/ruff check tests/test_result_acceptance_worker_integration.py
git diff --check
```

结果：测试通过；Ruff 和 diff-check 通过。

## 仍然关闭的 promotion 条件

该正向 seam 不足以打开产品 worker。以下条件仍需在同一 promotion commit 中具备：

- 可信认证/RequestContext 与全 repository tenant/workspace scope；
- handler revision 的闭合 allowlist、spawn/exec-before-secret-load 和资源沙箱；
- heartbeat/lease expiry、取消、graceful drain、stale-worker 与双进程中途 kill 的完整矩阵；
- receipt-bound restart/reconcile、restore-forward 和上一版本兼容/回滚；
- service lifecycle、API、observability、容量/SLO 和 clean-host release evidence；
- 明确的 fake/pure-only composition gate，并保证不存在真实 outbound 入口。

因此 `HeartbeatPureWorkerGate.dispatch_enabled` 仍为 `False`，`dispatch()` 继续从 argument-free
disabled frame fail closed。结果 authority、projection、backup、模型试用页和原生 IM 离线合同
之间也没有自动连接；外部 IM/飞书/企微消息仍禁止发送。

## 本地优先同步策略

本文先写入本地 Markdown、Git commit 和远端分支备份。Notion 保持 `local_pending`，待本地阶段
全部完成后批量同步正文、证据和截图，并逐页回读更新
`analysis_report/notion_sync_manifest.json`；回读完成前不声明 Notion 同步闭环。
