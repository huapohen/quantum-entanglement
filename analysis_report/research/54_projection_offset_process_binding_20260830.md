# Projection Offset Store 进程绑定证据（2026-08-30）

> 提交：`c8308e8`
> 分支：`mainline_continue_quantum_entanglement`
> 运行边界：本地 SQLite；不触碰外部 IM、模型或 connector

## 变更

`SQLiteProjectionOffsetStore` 现在在构造时捕获 PID + opaque process epoch，并在 `load`、`claim`、
`renew`、`advance`、`apply_event`、`release`、`close` 以及 transaction context 前执行进程守卫。
fork 后的 child 立即抛出 `ProjectionOffsetProcessMismatchError`，不会访问继承的 projection lock、
authorizer 或 SQLite connection。

## 验证

```text
PYTHONPATH=src .venv/bin/pytest -q tests/test_projections.py
72 passed

PYTHONPATH=src .venv/bin/python -m mypy --strict src
Success: no issues found in 76 source files

PYTHONPATH=src .venv/bin/python -m compileall -q src
exit 0

.venv/bin/ruff check src/quantum_entanglement/projections.py tests/test_projections.py
exit 0
```

新增 fork 回归用 pipe/`waitpid` 验证 child 在 `load()` 前被拒绝；既有 lease fencing、双连接、
receipt、handler transaction 和 schema drift 矩阵保持不变。Result Projection 自身已有更高层
process guard，本提交补的是可独立使用的通用 offset store。

## 边界

该提交只闭合 projection offset store 自身的 inherited-instance 风险；DurableProjector 的 event
source、handler、recovery coordinator、provider、secret 和跨组件重建仍需系统级测试与组合根门禁。
