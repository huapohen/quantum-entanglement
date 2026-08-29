# E3 Result Authority：Artifact 写入边界 SIGKILL 恢复证据

> 证据日期：2026-08-29（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 证据提交：`3f4a6c8`；测试收集修订：`f12898b`  
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`；本地阶段完成后批量上传并逐页回读

## 结论

本阶段新增一个真实跨进程故障测试，覆盖结果接受事务中“Artifact 已执行写入、但完整事务尚未
提交”这一窄边界。父进程先建立合法的 scoped start claim；子进程打开同一个 SQLite 数据库，
在 Artifact materialization 返回后、事务提交前发出确定性信号；父进程随后发送真实 `SIGKILL`。

重开数据库后，以下结果 authority 表全部保持空集，没有 Artifact、manifest、request、binding、
receipt 或 result artifact 半成品：

```text
artifact_blobs
artifact_versions
invocation_result_manifests
invocation_result_requests
invocation_result_event_bindings
invocation_result_receipts
invocation_result_artifacts
```

父进程随后使用原始、仍合法的 claim 重试一次，得到唯一的精确类型
`ScopedInvocationResultAcceptedV2`，并且 durable receipt 行数为 1。该证据证明 SQLite 事务对
这个边界具有 all-or-nothing rollback 语义；它不证明其他故障边界已经覆盖，也不打开 worker
promotion。

## 测试实现

测试文件：[`tests/test_result_acceptance_process_recovery.py`](../../tests/test_result_acceptance_process_recovery.py)

测试通过 JSON 临时文件传递准备好的请求快照（文件名为 `prepared-input.json`），避免 pickle
或 Python 对象跨进程传递。子进程只在创建本地 store 后安装测试用 materialization hook；hook
在 Artifact 行写入后触碰信号文件并保持事务未提交，父进程确认信号后调用 `child.kill()`，并断言
返回码为 `-9`。重开后的每张 authority 表都用 SQL 计数断言为 0，再执行一次 fresh acceptance。

测试没有调用模型、浏览器、MCP、connector、网络、飞书、企微、语雀或 Notion。

## 可复现命令与结果

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_result_acceptance_process_recovery.py \
  tests/test_result_projection.py \
  tests/test_result_acceptance_api.py \
  tests/test_result_acceptance_durable_prerequisites.py \
  tests/test_result_acceptance_preparation.py \
  tests/test_invocation_worker.py
```

结果：**187 tests passed**。其中新增进程级测试单独收集为 1 项并通过；projection 专项包含认证
读取负向矩阵、双连接 lease fencing 及既有 SIGKILL-after-lease 边界。

静态门禁：

```bash
.venv/bin/ruff check src tests/test_result_acceptance_process_recovery.py \
  tests/test_result_projection.py tests/test_result_acceptance_api.py
PYTHONPATH=src .venv/bin/python -m mypy --strict src
PYTHONPATH=src .venv/bin/python -m compileall -q src
git diff --check
```

以上命令均通过。仓库既有 `ruff format --check` 历史漂移未在本阶段格式化；本阶段没有引入新的
格式错误。

## 故障语义与限制

| 故障位置 | 本测试观察到的持久结果 | 是否允许盲目重试 |
| --- | --- | --- |
| Artifact DML 已执行，COMMIT 前 `SIGKILL` | 整个结果图回滚；所有 authority 表为 0 | 否；这里只证明原子回滚，重试策略仍由上层决定 |
| COMMIT 已持久化，ACK 丢失 | 由既有 API 测试覆盖；重开后只能 `ObservedV2` | 否 |
| lease claim 后投影进程被杀 | 由 projection 测试覆盖；过期后新 owner 可恢复 | 否，必须遵守 lease fence |

仍未覆盖的独立门禁包括：结果接受双进程竞争、每个 DML/commit/ACK 边界的完整 kill matrix、
restore/replay clean-host 证据、兼容/回滚矩阵、长时 heartbeat/graceful drain、可信认证组合、
production composition 和 receipt-bound worker promotion。`HeartbeatPureWorkerGate` 继续
default-off；真实外部副作用保持关闭。

## 本地优先同步策略

本证据先写入本地 `analysis_report/research`，由 Git commit 和远端分支备份。Notion 保持
`local_pending`，待本地阶段任务全部完成后批量同步正文、证据和截图，并逐页回读更新
`analysis_report/notion_sync_manifest.json`；在批量回读完成前不宣称 Notion 已同步闭环。
