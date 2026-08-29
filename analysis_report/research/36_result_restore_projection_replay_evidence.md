# E3 Result Authority：恢复后读模型重放证据

> 证据日期：2026-08-29（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 证据提交：`3a4a7de`  
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`；本地阶段完成后批量上传并逐页回读

## 结论

本阶段把“恢复后的结果 authority 能否在干净进程重放业务读模型”从文档假设变成可复现测试。
父进程建立含完整 result graph 的 opt-in migration-7 SQLite，生成并验证 result-specific backup，
再恢复到全新的 SQLite 路径。随后启动一个全新 Python 解释器，在 child 内重新构造
`SQLiteEventStore` 与 `SQLiteResultProjectionStore`，从零创建 projection-owned table，读取完整
事件历史并执行 `run_once(limit=1000)`。

child 最终按无身份正文的 JSON 输出：

```json
{"projected": true, "status": "completed", "replayed": true}
```

父进程严格比较该 JSON，而不是自比较。结果说明恢复副本中的 result accepted event 与 terminal
event 可以在新进程中按 durable global position 重放为唯一 `completed` 读模型，且不需要继承
父进程的 SQLite connection、lock、projection offset 或 Python 对象。它仍不等于跨主机灾备、
多副本一致性或生产 API readiness。

## 测试实现

测试文件：[`tests/test_result_restore_projection_replay.py`](../../tests/test_result_restore_projection_replay.py)

测试步骤：

1. 在父进程用合成 request/claim 写入一个完整 result acceptance graph；
2. 通过 `create_result_backup()` 生成带 topology、row count、geometry 和 digest 的备份；
3. 通过 `restore_result_backup()` 写入不存在的 restored target 并再次验证 topology；
4. 用 `subprocess.run()` 启动全新解释器，child 自己打开 event store、projection offset store
   和 projection table；
5. child 只读取 receipt identity，执行有限批次重放，读取 projection view 并断言状态为
   `completed`，然后关闭本进程资源；
6. 父进程断言 child exit code 为 0，并精确比对稳定 JSON 输出。

该测试不调用模型、浏览器、MCP、connector、网络、飞书、企微、语雀或 Notion。

## 可复现命令与结果

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_result_restore_projection_replay.py
.venv/bin/ruff check tests/test_result_restore_projection_replay.py
git diff --check
```

结果：测试通过；Ruff 与 diff-check 通过。

## 边界和未关闭事项

- projection table 与 offset 是恢复后的新进程按代码合同创建并重放的派生状态，不写入 result
  authority，不改变 receipt/event history；
- 该证据覆盖“完整备份恢复后、clean process、从零 projection replay”这一边界；不覆盖
  restore 中途 SIGKILL、损坏页、跨版本 schema upcast、跨主机文件系统语义、PostgreSQL 或
  RPO/RTO；
- result acceptance 双进程竞争、Artifact 写入后 SIGKILL、备份发布 SIGKILL 及空库 rollback
  由独立证据覆盖；全系统每个边界 kill matrix、双进程中途 kill/lease expiry、兼容/回滚矩阵、
  长时 heartbeat/graceful drain、可信认证 composition 与 receipt-bound worker promotion
  仍未关闭；
- `HeartbeatPureWorkerGate` 继续 default-off，真实外部副作用继续关闭。

## 本地优先同步策略

本文先写入本地 `analysis_report/research`、Git commit 和远端分支备份。Notion 保持
`local_pending`，待本地阶段全部完成后批量同步正文、证据和截图，逐页回读并更新
`analysis_report/notion_sync_manifest.json`；回读完成前不声明 Notion 同步闭环。
