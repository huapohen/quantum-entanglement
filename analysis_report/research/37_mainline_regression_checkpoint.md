# 主线本地回归 checkpoint

> 证据日期：2026-08-29（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 代码 HEAD：`e4738dd`  
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`

## 回归结果

在当前 worktree、源码路径显式设置为 `PYTHONPATH=src` 的条件下运行：

```bash
PYTHONPATH=src .venv/bin/pytest -q
```

pytest 收集 **2,946** 项，退出码为 **0**。输出只包含仓库已有的 macOS 多线程 `fork()`
`DeprecationWarning`；没有测试失败、未捕获异常或新增 warning gate。

本阶段新增测试覆盖：

- 结果接受 Artifact 写入后、COMMIT 前真实 `SIGKILL` 回滚；
- 两个独立 `spawn` 进程同时接受同一结果时一次 `AcceptedV2` / 一次 `ObservedV2`；
- result backup 恢复后由全新进程从零重放 `SQLiteResultProjectionStore` 并得到 `completed`。

专项门禁也再次通过：

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

上述组合为 **188 tests passed**；备份/迁移/拓扑组合为 **39 tests passed**。Ruff、源码 strict
Mypy、`compileall` 和 `git diff --check` 均通过。

## 解释边界

2,946 是当前 pytest 文件集合的可复现计数，不是生产容量或覆盖率指标；它不代表 Gate A–E
关闭，也不替代 Linux clean runner、Python 3.9/3.12/3.13 矩阵、PostgreSQL、负载/混沌、
全系统 crash-at-every-boundary、真实认证或外部 provider 证据。`HeartbeatPureWorkerGate`、
真实 IM、飞书/企微 outbound 继续 default-off。

## 本地优先同步策略

本 checkpoint 只写本地 Markdown 并由 Git/GitHub 备份。Notion 保持 `local_pending`，待本地
阶段全部完成后批量同步并逐页回读；同步过程中不向飞书或企微发送消息。
