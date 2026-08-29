# 主线 worker-seam 后最新全量回归证据

> 证据日期：2026-08-29（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 代码 HEAD：`73f1996`  
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`

## 结果

在加入 heartbeat supervisor→store-owned result acceptance 正向集成测试后，当前 worktree 执行：

```bash
PYTHONPATH=src .venv/bin/pytest --collect-only -q
PYTHONPATH=src .venv/bin/pytest -q
```

pytest 收集 **2,949** 项，全量回归退出码为 **0**。输出仅有仓库既有 macOS 多线程 `fork()`
`DeprecationWarning`，没有失败、错误或未捕获异常。

本阶段集成专项（worker、result acceptance、process kill、projection、backup/migration）和
以下静态门禁均通过：

```bash
.venv/bin/ruff check src tests/test_result_acceptance_worker_integration.py \
  tests/test_result_acceptance_process_commit_kill.py \
  tests/test_result_acceptance_process_kill_matrix.py \
  tests/test_result_restore_projection_replay.py \
  tests/test_result_acceptance_process_recovery.py \
  tests/test_result_acceptance_process_competition.py
PYTHONPATH=src .venv/bin/python -m mypy --strict src
PYTHONPATH=src .venv/bin/python -m compileall -q src
git diff --check
```

## 当前边界

正向集成只证明候选 PURE/fake supervisor 能把 exact request 交给 store-owned acceptor，并在
`AcceptedV2` 后结束；它没有使 `HeartbeatPureWorkerGate.dispatch()` 可达。默认产品 dispatch、
模型/插件/MCP/browser、真实 connector、原生 IM、飞书、企微、语雀和 Notion outbound 仍关闭。

2,949 项是当前 pytest 文件集合计数，不是生产覆盖率、容量、SLO、Linux/Windows 支持、
PostgreSQL、跨主机 RPO/RTO 或安全审批证明。全系统 admission/claim/heartbeat/lease-expiry/
worker kill matrix、可信认证 composition、全 repository tenant scope、兼容回滚与正式 promotion
仍是后续硬门禁。

## 本地优先同步策略

本证据先进入本地 Markdown、Git commit 和远端分支备份。Notion 保持 `local_pending`，待本地阶段
全部完成后批量同步正文、证据和截图，逐页回读并更新
`analysis_report/notion_sync_manifest.json`；回读完成前不声明 Notion 同步闭环。
