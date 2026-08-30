# 主线 2026-08-30 全量回归与静态门禁证据

> 证据日期：2026-08-30（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 验证时代码 HEAD：`9656dc2`（随后仅追加验证文档）  
> 远端：`origin/mainline_continue_quantum_entanglement`（已推送）  
> Notion 状态：`local_pending`

## 结果

在本次生命周期与 lease race 节点收口后，当前 worktree 完成：

```bash
PYTHONPATH=src .venv/bin/pytest --collect-only
PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/ruff check src tests
PYTHONPATH=src .venv/bin/python -m mypy --strict src
PYTHONPATH=src .venv/bin/python -m compileall -q src
git diff --check
```

- pytest 收集 **2,958 项**；全量回归退出码 **0**；
- Ruff 检查 `src tests` 通过；
- strict mypy 检查 76 个源码文件通过；
- `compileall` 通过；
- `git diff --check` 通过；
- 输出仅包含仓库既有 macOS 多线程 `fork()` `DeprecationWarning`，无失败、错误或未捕获异常。

本次 2,958 项包含新增生命周期、双连接 heartbeat/expiry 竞争和双连接 relinquish 竞争用例。
专项 10 项命令与断言见 [`43_scoped_lease_lifecycle_evidence.md`](./43_scoped_lease_lifecycle_evidence.md)。

## 版本留痕

本次可回滚节点按小步提交并已逐个推送：

```text
4fd6588  scoped lease heartbeat + expiry recovery
f0a989b  supervisor cancellation drains handler
36633d0  immediate scoped lease relinquish + lifecycle timestamp invariant
36cd0b4  scoped PURE worker lifecycle composition
a4196d3  preserve store wildcard compatibility
025b5c7  dual-connection heartbeat/expiry and relinquish races
3d8173f  lifecycle production contract documentation
36a6d1c  lifecycle race evidence
35e7900  current readiness checkpoint
9656dc2  next-stage plan lifecycle gate
```

远端分支与本地 HEAD 一致；没有合并到 `main`，也没有删除 worktree。

## 仍然关闭的生产边界

全量回归只证明记录环境中的源码和测试断言成立，不代表生产 GA。以下门禁仍未打开：

- `HeartbeatPureWorkerGate.dispatch()` 与模型/Agent/plugin/MCP/browser composition；
- 全系统 crash-at-every-boundary、SIGKILL 后 result acceptance、兼容回滚和 clean-host release；
- 可信认证入口、全 repository tenant/workspace SQL scope、跨进程服务 composition；
- handler revision allowlist、spawn/exec-before-secret-load、OS 级 sandbox 与容量/SLO/Soak；
- 真实 IM provider contract、测试 scope/profile/mapper、production exchange；
- connector/action receipt、ACK、`succeeded | rejected | effect_unknown` 和任何外部副作用。

飞书、企微、Notion、语雀均未在本次回归中发送消息或触发外部动作；Notion 仍按本地优先策略
保持待同步，等待用户确认该阶段后再批量上传并逐页回读。
