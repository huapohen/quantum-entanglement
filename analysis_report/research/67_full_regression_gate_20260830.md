# 10 小时截止冲刺：全量回归门禁证据（2026-08-30）

## 执行范围

在 `mainline_continue_quantum_entanglement`，migration 11 authority fixture 修复与本地 readiness
文档更新后，执行 `./.venv/bin/python -m scripts.regression_gate --full`。门禁选择器将 Python、Go、
Web 和 synthetic loopback 一并纳入；运行过程中没有启动飞书、企微、真实 IM 或 outbound。

## 结果

全部退出码为 0：

```text
pytest full      pass（当前测试库存 2,975 项）
Ruff             pass
strict mypy      pass（76 source files）
compileall       pass
go test ./...    pass
go vet ./...     pass
npm run build    pass（Vite production build）
verify_web_first pass（envelope、Agent Store、子群隔离、Workboard 审阅闭环）
regression_gate  pass
```

pytest 仅出现 Python fork 多线程 `DeprecationWarning`，没有失败或错误；该 warning 不改变支持窗口
和门禁退出码。

## 可复现命令

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/mainline_continue_quantum_entanglement
PYTHONPATH=src .venv/bin/python -m scripts.regression_gate --full
```

## 解释边界

全量回归证明当前已提交代码的确定性测试、构建和本地零网络体验闭环；它不等价于真实 Clerk/JWKS、
PostgreSQL projector writer、同事务 checkpoint、跨进程 crash/restore、真实 IM provider 或 Gate A–E
生产批准。materialized message reader 仍 inactive，默认读取继续走 bounded EventStore replay。
