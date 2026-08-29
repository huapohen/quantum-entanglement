# Result business projection（候选、默认关闭）

状态：**已实现为 opt-in 候选读模型，不是生产 composition。**

`SQLiteResultProjectionStore` 使用现有 `DurableProjector` 的 leased offset、receipt
deduplication、upcaster 和 fenced transaction，把一条已提交的
`task.invocation.result.accepted` 事件与其绑定的 terminal transition 投影为
`task_result_projection_v1`。它只 materialize 业务查询需要的最小字段：tenant/workspace/
invocation identity、task metadata、result reference、receipt/event coordinates、revision、
Artifact 数量、manifest digest 和 `result_accepted | completed` 状态。

本地 Markdown 与 Git/GitHub 是开发期事实源；Notion 保持 `local_pending`，待当前本地阶段全部
完成后批量上传并逐页回读。此候选不调用 Notion、语雀、飞书、企微、模型、浏览器、MCP、connector
或任何网络服务。

## 使用边界

```python
from quantum_entanglement import SQLiteEventStore, SQLiteResultProjectionStore

events = SQLiteEventStore(
    "event-store.sqlite3",
    enable_result_acceptance_schema=True,
)
projection = SQLiteResultProjectionStore(
    events,
    "event-store.sqlite3",
    owner_id="result-projector-dev-1",
)
run = projection.run_once(limit=100)
view = projection.read(tenant_id, workspace_id, invocation_id)
```

生产服务尚未把该类接入 composition root。调用方必须先通过可信认证层确定
`tenant_id`/`workspace_id`，不能把客户端传入的 scope 当成授权证明。默认
`SQLiteEventStore(enable_result_acceptance_schema=False)` 仍然不启用 result authority。

## 投影合同

| 字段 | 约束 |
| --- | --- |
| 主键 | `(tenant_id, workspace_id, invocation_id)`，禁止跨 scope 覆盖 |
| receipt | 每个 scope 唯一绑定一个 `receipt_id` |
| result event | 只接受严格解码的 `ScopedInvocationResultEvidenceV2` |
| terminal event | 必须匹配 receipt、result event、running revision，且 terminal revision = running + 1 |
| 状态 | 先 `result_accepted`，收到匹配 terminal transition 后才 `completed` |
| 正文 | 不写入 narration、metadata、Artifact bytes、原始 handler 输出或模型响应 |
| 凭据 | 不写入 lease token、secret、credential、connector payload 或 capability |
| 时间 | canonical UTC、微秒精度；digest 为小写 SHA-256 |
| 规模 | identity 文本最多 4 KiB，Artifact 数量最多 256，revision 使用 SQLite 有符号整数边界 |

投影实例在创建时捕获 PID + opaque process epoch；fork/PID drift 后的 `run_once()`、`read()` 或
`close()` 会在接触 SQLite/lock 前抛出 `ResultProjectionProcessMismatchError`，子进程不会替父进程
关闭或操作继承的 connection。投影表由模块自有 DDL 创建，并在每次打开时逐列校验
`PRAGMA table_info` 和 `sqlite_master.sql`。
任何列、主键、NULLability、CHECK 或表 SQL 漂移都会抛出 `ResultProjectionSchemaError`，不尝试
猜测迁移。projection offset、lease 和 receipt 表仍由通用 projector 管理；handler 只能使用
`ProjectionTransaction`，无法获得 event-store connection、文件路径或 framework-table capability。

## 事件顺序与故障语义

1. 结果事件先到：插入 `result_accepted`，不提前猜测 `completed`。
2. terminal 事件先到或找不到唯一结果投影：抛出 `ResultProjectionConflictError`，该事件事务回滚。
3. 已存在同一 scope 的另一结果 identity：抛出 conflict，不覆盖旧行。
4. projector 重跑通过 durable offset/receipt 去重，不产生第二行或第二次状态跃迁；重开第二个
   connection 会复用同一 durable offset。
5. 两个 projection connection 同时竞争时，lease owner fencing 只允许一个 owner 处理事件，
   另一个得到 `ProjectionLeaseConflictError`，不会覆盖读模型。
6. lease 过期、进程崩溃或 ACK 不明确仍遵循通用 projector 的 fence/rollback 合同；业务读模型
   可以重建，但不产生新的结果 authority。
7. 结果图缺失、digest 漂移、terminal 绑定漂移由上游 result acceptance/reconciliation
   quarantine 处理；projection 不修复、不删除、不重写 event history。

## 已验证断言

`tests/test_result_projection.py` 覆盖：

- 完整 result + terminal 事件得到 `COMPLETED`，字段与 receipt/event coordinates 一致；
- tenant/workspace/invocation 任一 scope 替换都返回不可枚举的 `None`；
- 重复 run 是幂等的；
- terminal-only 输入 fail closed；
- 结果 identity 冲突 fail closed 且保留原投影；
- projection schema 漂移拒绝；
- handler trace 不触碰 events、invocation jobs/attempts 或 outbox framework tables；
- 投影对象 repr 不包含结果正文或 lease token；真实 fork 子进程在触碰 SQLite 前被拒绝，父进程仍可继续运行；
- 重开 connection 复用 durable offset；双 connection lease 竞争只允许一个 owner。
- 真实子进程 `SIGKILL` 恰在 lease claim 后触发，lease 过期后由新 owner 成功恢复完整投影。
- 子进程在 lease 已提交、尚未读取事件时收到真实 `SIGKILL`，新 owner 等 lease 过期后可
  重新 claim 并完整重建 projection；这只覆盖该边界，不替代全系统 crash-at-every-boundary 证据。

验证命令：

```bash
PYTHONPATH=src ./.venv/bin/python -m pytest -q tests/test_result_projection.py
./.venv/bin/ruff check src/quantum_entanglement/result_projection.py \
  src/quantum_entanglement/__init__.py tests/test_result_projection.py
PYTHONPATH=src ./.venv/bin/python -m mypy \
  src/quantum_entanglement/result_projection.py src/quantum_entanglement/__init__.py
```

这些是本地 SQLite 读模型测试，不证明多进程容量、kill-9 每个边界、clean-host restore、
认证 API、SLO/RPO/RTO 或生产租户隔离。process binding、全 repository scope、可靠恢复和
compatibility/rollback 仍是独立 release gate。

## 不可晋级事项

- 没有可信 RequestContext/认证 composition 时，不得把 `read()` 暴露给公网或真实客户；
- 没有跨 tenant property、双连接竞争、kill/restore replay、容量/soak 证据时，不得宣称生产；
- 没有完整 result receipt 时，绝不把 succeeded job 猜测为 completed；
- 该 projection 不创建 outbox/publication，不连接原生 IM，也不改变
  `SERVICE_BOUNDARY.md` 的 outbound 禁止边界。
