# Invocation Recovery Coordinator 进程绑定证据（2026-08-30）

> 提交：`33faa43`
> 分支：`mainline_continue_quantum_entanglement`
> 运行边界：本地 fake/SQLite；不触碰外部 IM、模型或 connector

## 变更

`InvocationRecoveryCoordinator` 现在在构造时捕获 PID + opaque process epoch，并在 `__enter__`、
`closed`、`assess`、`assess_scoped` 与 `close` 入口先执行守卫。fork 后的 child 抛出
`InvocationRecoveryProcessMismatchError`，不会读取或关闭继承的锁、attempt store 或 result store。

## 验证

```text
PYTHONPATH=src .venv/bin/pytest -q tests/test_invocation_recovery.py
31 passed

PYTHONPATH=src .venv/bin/python -m mypy --strict src
Success: no issues found in 76 source files

.venv/bin/ruff check src/quantum_entanglement/invocation_recovery.py src/quantum_entanglement/__init__.py tests/test_invocation_recovery.py
exit 0
```

新增 fork 回归通过 pipe/`waitpid` 验证 child 在 `closed` property 前被拒绝；原有 recovery decision、
receipt-bound observation、owned/borrowed cleanup 和 close retry 矩阵保持不变。新错误已加入模块
和 package root 的公开一致性检查。

## 边界

该提交只闭合协调器自身的 inherited-instance 风险；它不替代底层 attempt/result store、event
source、provider、secret 或真实 service composition 的 process/kill/recovery 证明。
