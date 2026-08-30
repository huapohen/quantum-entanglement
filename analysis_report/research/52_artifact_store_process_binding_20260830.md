# Artifact Store 进程绑定证据（2026-08-30）

> 提交：`3a729b4`
> 分支：`mainline_continue_quantum_entanglement`
> 运行边界：本地 SQLite；不触碰外部 IM、模型或 connector

## 变更

`SQLiteArtifactStore` 现在在构造时捕获 PID + opaque process epoch，并在所有 public 入口先执行
进程守卫：`write`、`get`、`head`、`history`、`verify_scope`、`schema_version`、`close` 与上下文
管理器入口。fork 后的 child 不会触碰继承的 `RLock` 或 SQLite connection，而是立即抛出固定的
`ArtifactProcessMismatchError`。

## 验证

```text
PYTHONPATH=src .venv/bin/pytest -q tests/test_artifact_store.py
28 passed

PYTHONPATH=src .venv/bin/python -m mypy --strict src
Success: no issues found in 76 source files

PYTHONPATH=src .venv/bin/python -m compileall -q src
exit 0

.venv/bin/ruff check src/quantum_entanglement/artifact_store.py tests/test_artifact_store.py
exit 0
```

新增 fork 回归通过 pipe/`waitpid` 证明 child 在 `schema_version()` 前被拒绝，父进程继续负责
关闭自己的连接。现有 spawn/two-process version allocation 测试仍保留，证明“拒绝 inherited
instance”和“传递路径重新构造新 store”是两种不同合同。

## 边界

该节点只闭合 artifact store 自身的 process ownership；revocation store、通用 projection offset
store、recovery coordinator、provider/secret/runtime 仍需逐组件迁移，不能由本证据推断全系统
fork/spawn 安全已经完成。
