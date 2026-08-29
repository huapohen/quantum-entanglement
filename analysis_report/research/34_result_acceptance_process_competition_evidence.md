# E3 Result Authority：双进程竞争证据

> 证据日期：2026-08-29（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 证据提交：`8bc7c58`  
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`；本地阶段完成后批量上传并逐页回读

## 结论

本阶段验证两个独立的、全新 `spawn` 解释器同时提交同一份 scoped result acceptance request 时，
结果 authority 只会产生一个 fresh acceptance。两个进程使用相同的合法 start receipt、attempt
lease 和 request digest，在 SQLite 文件上通过 `BEGIN IMMEDIATE` 竞争：一个进程得到精确的
`ScopedInvocationResultAcceptedV2`，另一个在看到已提交图后只能得到 capability-free
`ScopedInvocationResultObservedV2`。

重开数据库后的 durable 计数为：

```text
invocation_result_receipts       = 1
invocation_result_event_bindings = 2
invocation_result_artifacts      = 请求中的 Artifact 数量
invocation_jobs.status           = succeeded
invocation_attempts.status       = succeeded
```

两个结果携带相同的 receipt ID，没有第二份 Artifact、结果事件、terminal 事件或 terminal CAS。
这证明 acceptance writer 的幂等/竞态分类在双进程条件下保持单一结果 authority；它不等于
worker promotion，也不证明外部副作用 exactly-once。

## 测试实现

测试文件：[`tests/test_result_acceptance_process_competition.py`](../../tests/test_result_acceptance_process_competition.py)

父进程先在 opt-in migration-7 数据库建立合法的 start admission/claim，并将经过 JSON 编码的
request 快照写入临时目录。两个子进程均在自身解释器中重新打开 SQLite、重建 exact typed
request/claim、在 barrier 后同时调用 `accept_scoped_invocation_result_v2()`。测试明确断言：

1. 两个子进程都正常退出；
2. outcome 集合恰好为 `{"accepted", "observed"}`；
3. receipt ID 集合大小为 1；
4. 重开数据库后 receipt、event binding、Artifact 和 job/attempt 状态符合唯一图约束。

测试连续独立运行 3 次均通过，未依赖进程启动顺序或人为 sleep。输入只包含合成 identity、
Artifact 内容和短期测试 lease token；没有模型、浏览器、MCP、connector、网络、飞书、企微、
语雀或 Notion 调用。

## 可复现命令与结果

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_result_acceptance_process_recovery.py \
  tests/test_result_acceptance_process_competition.py \
  tests/test_result_projection.py \
  tests/test_result_acceptance_api.py \
  tests/test_result_acceptance_durable_prerequisites.py \
  tests/test_result_acceptance_preparation.py \
  tests/test_invocation_worker.py
```

结果：**188 tests passed**。新增的两个进程级测试各自收集为 1 项；projection 专项继续覆盖
认证读取负向矩阵、双连接 lease fencing 及 projection SIGKILL-after-claim 恢复。

静态门禁：

```bash
.venv/bin/ruff check tests/test_result_acceptance_process_competition.py \
  tests/test_result_acceptance_process_recovery.py
git diff --check
```

以上命令通过；源码 strict Mypy、compileall 由同一阶段的共享门禁执行并通过。

## 竞态语义与限制

| 竞态/故障 | 允许的持久解释 | 是否能自动升级为 Accepted |
| --- | --- | --- |
| 两个 fresh acceptance 同时进入 | 一个完整图提交，另一个读取同一图 | 只有收到本次 fresh COMMIT ACK 的进程 |
| 第二进程在首进程提交后开始 | `ObservedV2`，不重新签发 capability | 否 |
| 首进程 COMMIT 后 ACK 丢失 | 既有图保留；重开/重放为 `ObservedV2` | 否 |
| Artifact 写入后、COMMIT 前进程被杀 | 既有 SIGKILL 证据证明全图回滚 | 不能盲目重试，须遵循上层 retry policy |

仍未覆盖的独立门禁包括：双进程中途 kill 与 lease expiry、所有 DML/commit/ACK 边界的完整
kill matrix、clean-host restore/replay、兼容/回滚、长时 heartbeat/graceful drain、可信认证
composition、全 repository tenant scope 以及 receipt-bound worker promotion。真实外部副作用
继续保持关闭。

## 本地优先同步策略

本证据先进入本地 Markdown、Git commit 和远端分支备份。Notion 保持 `local_pending`，待本地阶段
任务全部完成后批量同步正文、证据和截图，并逐页回读更新
`analysis_report/notion_sync_manifest.json`；批量回读完成前不声明 Notion 同步闭环。
