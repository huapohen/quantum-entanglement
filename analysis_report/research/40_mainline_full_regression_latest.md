# 主线最新全量回归证据

> 证据日期：2026-08-29（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 代码 HEAD：`313f99d`  
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`

## 结果

最新 HEAD 在当前 macOS worktree 执行：

```bash
PYTHONPATH=src .venv/bin/pytest --collect-only -q
PYTHONPATH=src .venv/bin/pytest -q
```

pytest 收集 **2,948** 项，完整回归退出码为 **0**。输出仅包含仓库既有的多线程 `fork()`
`DeprecationWarning`；无失败、错误或未捕获异常。新增的进程级结果 authority 测试也被纳入
本次全量集合。

本阶段静态门禁再次通过：

```bash
.venv/bin/ruff check src tests
PYTHONPATH=src .venv/bin/python -m mypy --strict src
PYTHONPATH=src .venv/bin/python -m compileall -q src
git diff --check
```

全仓 `ruff format --check` 的历史格式漂移仍按既有策略保留，未在本阶段对无关历史文件做批量
重排；新增和修改文件均通过 Ruff lint 与 diff-check。

## 证据边界

2,948 项是当前 pytest 收集计数，不是生产覆盖率、容量或 SLO 证明。当前仍不代表 Gate A–E
关闭，不替代 Linux clean runner、Python 3.9/3.12/3.13 兼容矩阵、PostgreSQL、真实认证、
全系统外部 action、负载/混沌/Soak、跨主机 RPO/RTO 或正式 worker promotion。真实 IM、飞书、
企微和其他不可逆 outbound 继续关闭。

## 本地优先同步策略

本证据先写入本地 Markdown、Git commit 和远端分支备份。Notion 保持 `local_pending`，待本地
阶段全部完成后批量上传正文、证据和截图，逐页回读并更新
`analysis_report/notion_sync_manifest.json`；回读完成前不声明 Notion 同步闭环。
