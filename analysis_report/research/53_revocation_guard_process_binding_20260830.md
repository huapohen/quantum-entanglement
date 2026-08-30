# Revocation Guard 进程绑定证据（2026-08-30）

> 提交：`3bb8f8b`
> 分支：`mainline_continue_quantum_entanglement`
> 运行边界：本地 SQLite；不触碰外部 IM、模型或 connector

## 变更

`SQLiteRevocationRevisionGuard` 现在在构造时捕获 PID + opaque process epoch，并在 `check_and_advance`、
`high_water`、`state_digest`、`schema` 上下文和 `close` 入口先执行守卫。fork 后的 child 立即抛出
`RevocationGuardProcessMismatchError`，不会访问继承的 `RLock`、SQLite connection 或 high-water row。

## 验证

```text
PYTHONPATH=src .venv/bin/pytest -q tests/test_tenancy.py
39 passed

PYTHONPATH=src .venv/bin/python -m mypy --strict src
Success: no issues found in 76 source files

PYTHONPATH=src .venv/bin/python -m compileall -q src
exit 0

.venv/bin/ruff check src/quantum_entanglement/tenancy.py src/quantum_entanglement/__init__.py tests/test_tenancy.py
exit 0
```

新增 fork 回归用 pipe/`waitpid` 验证 child 在 `high_water()` 前被拒绝；原有独立连接、重启、schema
完整性与 rollback 测试保持不变。新错误已加入 tenancy 模块和 package root 的公开一致性检查。

## 边界

该提交只闭合 revocation guard 自身的 inherited-instance 风险；key ring、recovery coordinator、
provider、secret 和通用 projection offset 仍需独立进程合同与专项证据。
