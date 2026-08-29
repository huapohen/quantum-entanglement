# E3 Result Authority：结果接受提交前 SIGKILL 矩阵证据

> 证据日期：2026-08-29（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 证据提交：`eafe13a`  
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`；本地阶段完成后批量上传并逐页回读

## 结论

本阶段把单一 Artifact 写入点扩展为结果接受事务的提交前故障矩阵。测试在全新 `spawn` 解释器
中对每个指定边界执行真实 `SIGKILL`，而不是抛出 Python 异常模拟：

```text
artifact
event:1
event:2
insert:manifest
insert:request
insert:result event binding
insert:terminal event binding
insert:receipt
insert:Artifact binding 0
update:job terminal CAS
update:attempt terminal CAS
```

每个边界均满足以下结果：

1. child 进程退出码为 `-SIGKILL`；
2. 重开 SQLite 后 `artifact_blobs`、`artifact_versions`、
   `invocation_result_manifests`、`invocation_result_requests`、
   `invocation_result_event_bindings`、`invocation_result_receipts`、
   `invocation_result_artifacts` 七张 authority 表均为 0 行；
3. 原始 start stream 的版本仍等于 acceptance request 的 expected version；
4. 使用同一份合法 request/claim 重试，得到唯一精确类型 `AcceptedV2`，receipt 行数为 1。

这证明当前 SQLite owner transaction 对所有已列出的提交前 DML 边界具有 all-or-nothing
rollback 语义。它不覆盖 COMMIT 后 ACK 丢失（由 API/poison/reopen 测试覆盖），也不宣称
所有系统边界或外部副作用 exactly-once。

## 测试实现

测试文件：[`tests/test_result_acceptance_process_kill_matrix.py`](../../tests/test_result_acceptance_process_kill_matrix.py)

父进程为每个 kill point 建立独立临时数据库和合法 scoped start claim，将 request 以 JSON
快照传递给 child。child 在自身解释器打开 store 后安装仅限测试的 hook：

- Artifact hook 在 materialization 返回后触发；
- authority INSERT/terminal CAS hook 在底层调用成功后按精确 label 触发；
- event hook 按结果事件与 terminal 事件调用序号触发。

hook 不执行 rollback 或 cleanup；进程由 `os.kill(os.getpid(), signal.SIGKILL)` 终止，交由 SQLite
连接关闭时的事务语义和父进程重开验证。所有后续 retry 由父进程在 child 已完全退出后执行，
因此不存在同一连接或 inherited lock 共享。

## 可复现命令与结果

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_result_acceptance_process_kill_matrix.py
.venv/bin/ruff check tests/test_result_acceptance_process_kill_matrix.py
git diff --check
```

结果：矩阵测试通过（11 个 `subTest` 边界）；Ruff 和 diff-check 通过。

## 覆盖边界和仍缺事项

| 类别 | 当前证据 |
| --- | --- |
| 提交前 Artifact/DML/event/CAS | 本矩阵，11 个真实 SIGKILL 边界 |
| Artifact 写入后窗口 | `33_result_acceptance_process_kill_evidence.md`，独立跨进程复核 |
| 双进程 fresh acceptance | `34_result_acceptance_process_competition_evidence.md` |
| COMMIT ACK 丢失/poison/reopen | result acceptance API 与 durable prerequisite 专项 |
| backup publication SIGKILL | `35_result_backup_restore_compatibility_evidence.md` |
| restore 后 clean-process projection replay | `36_result_restore_projection_replay_evidence.md` |

仍未覆盖：COMMIT 指令本身被 OS 杀死的每种 driver 状态、ACK 丢失后的双进程恢复竞态、lease
expiry 期间中途 kill、全系统 admission/claim/heartbeat/worker/connector kill matrix、
跨版本/跨主机 restore、兼容回退、容量/SLO、可信认证 composition 和 receipt-bound worker
promotion。`HeartbeatPureWorkerGate`、真实模型/connector、IM、飞书、企微、语雀、Notion
outbound 继续 default-off。

## 本地优先同步策略

本文先进入本地 Markdown、Git commit 和远端分支备份。Notion 保持 `local_pending`，待本地阶段
全部完成后批量同步正文、证据和截图，并逐页回读更新
`analysis_report/notion_sync_manifest.json`；回读完成前不声明 Notion 同步闭环。
