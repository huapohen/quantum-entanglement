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

## 认证读取入口

`read_authorized(composer, operation, context, request)` 是候选的 service-facing read seam。它只接受
精确的 `ProtectedOperationComposer`、`AuthorizedOperation`、`RequestContext` 和 `AccessRequest`；request
必须使用固定的 `resource.read` action、`task_result_projection` resource type，并包含 workspace scope。
方法先让 composer 做 action-time reauthorization，再消费一次性 operation，最后从同一 request 派生
tenant/workspace/invocation 调用低层 `read()`。调用方不能另传一组 scope 或 invocation ID，也不能复用已
消费的 operation；每次读取都必须重新授权。认证失败、request/resource 漂移或依赖类型伪造均转换为不含
身份/凭据细节的稳定 `ResultProjectionAuthorizationError`。

```python
view = projection.read_authorized(
    composer,
    operation,
    request_context,
    AccessRequest(
        request_id="request-1",
        subject_id="subject-1",
        tenant_id=tenant_id,
        action=RESULT_PROJECTION_READ_ACTION,
        resource=ResourceRef(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            resource_type=RESULT_PROJECTION_RESOURCE_TYPE,
            resource_id=invocation_id,
        ),
    ),
)
```

低层 `read(tenant_id, workspace_id, invocation_id)` 仍是重建/内部 repository primitive，不得直接暴露给
未经认证的 transport；该 seam 也不等于真实 OIDC/JWT、API、全仓库 scope 或 production composition。

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
3. 已存在同一 scope 的另一结果 identity，或 receipt/result-event identity 已在其他 scope
   使用：抛出 conflict，不覆盖旧行。
4. projector 重跑通过 durable offset/receipt 去重，不产生第二行或第二次状态跃迁；重开第二个
   connection 会复用同一 durable offset。
5. 两个 projection connection 同时竞争时，lease owner fencing 只允许一个 owner 处理事件，
   另一个得到 `ProjectionLeaseConflictError`，不会覆盖读模型。
6. lease 过期、进程崩溃或 ACK 不明确仍遵循通用 projector 的 fence/rollback 合同；业务读模型
   可以重建，但不产生新的结果 authority。
7. terminal 的 session/plan/task/agent、timestamp 和 result identity 任一漂移都会 fail closed；
   结果图缺失、digest 漂移、terminal 绑定漂移由上游 result acceptance/reconciliation
   quarantine 处理；projection 不修复、不删除、不重写 event history。

## 已验证断言

`tests/test_result_projection.py` 覆盖：

- 完整 result + terminal 事件得到 `COMPLETED`，字段与 receipt/event coordinates 一致；
- tenant/workspace/invocation 任一 scope 替换都返回不可枚举的 `None`；
- 重复 run 是幂等的；
- terminal-only 输入 fail closed；
- 结果 identity 冲突 fail closed 且保留原投影；
- terminal session/plan/task/agent 绑定漂移 fail closed；
- projection schema 漂移拒绝；
- handler trace 不触碰 events、invocation jobs/attempts 或 outbox framework tables；
- 投影对象 repr 不包含结果正文或 lease token；真实 fork 子进程在触碰 SQLite 前被拒绝，父进程仍可继续运行；
- 重开 connection 复用 durable offset；双 connection lease 竞争只允许一个 owner。
- 真实子进程 `SIGKILL` 恰在 lease claim 后触发，lease 过期后由新 owner 成功恢复完整投影。
- 子进程在 lease 已提交、尚未读取事件时收到真实 `SIGKILL`，新 owner 等 lease 过期后可
  重新 claim 并完整重建 projection；这只覆盖该边界，不替代全系统 crash-at-every-boundary 证据。
- 认证读取从已 reauthorize 的 request 派生完整 scope；action/resource 不匹配、subject drift 和
  forged composer/operation/context/request 均在 SQLite read 前拒绝。

验证命令：

```bash
PYTHONPATH=src ./.venv/bin/python -m pytest -q tests/test_result_projection.py
./.venv/bin/ruff check src/quantum_entanglement/result_projection.py \
  src/quantum_entanglement/__init__.py tests/test_result_projection.py
PYTHONPATH=src ./.venv/bin/python -m mypy \
  src/quantum_entanglement/result_projection.py src/quantum_entanglement/__init__.py
```

这些是本地 SQLite 读模型测试，不证明多进程容量、kill-9 每个边界、clean-host restore、
真实认证 API、全 repository scope、SLO/RPO/RTO 或生产租户隔离。process binding、全系统恢复和
compatibility/rollback 仍是独立 release gate。

## 不可晋级事项

- 没有可信 RequestContext/认证 composition 时，不得把低层 `read()` 暴露给公网或真实客户；
- 没有跨 tenant property、双连接竞争、kill/restore replay、容量/soak 证据时，不得宣称生产；
- 没有完整 result receipt 时，绝不把 succeeded job 猜测为 completed；
- 该 projection 不创建 outbox/publication，不连接原生 IM，也不改变
  `SERVICE_BOUNDARY.md` 的 outbound 禁止边界。
