# Agent Store localdemo durable seam（2026-08-31）

状态：`local_pending`。按当前协作约束，本专题只写入本地工作树和 Git，Notion 在截止时间前统一批量同步；语雀、飞书、企微均未操作。代码提交：`22a65e4`（seam）与 `0df18f7`（catalog drift fail-closed）。

## 结论

localdemo 的 Agent Store 安装/撤权此前完全改动进程内 `agentCatalog`，重启后会丢失控制面状态，也没有复用已经完成的 PostgreSQL tenant-bound Agent Store repository。本轮补上了一个可运行的、显式的 durable backend seam：

- `localdemo.AgentStoreBackend` 暴露三项最小控制面动作：`SyncCatalog`、`CommitInstall`、`CommitOffboard`。
- `localdemo.PostgresAgentStoreBackend` 通过 `imstore.TenantUnitOfWork` 执行，不接收 raw `pgxpool`，复用 tenant binding、serializable transaction、`agent.*` command receipt、exact idempotency replay 和 Agent Store CAS repository。
- `NewWithRuntimeAndAgentStore` / `NewFromEnvWithAgentStore` / `app.NewLocalDemoWithAgentStore` 形成显式组合入口；不传 backend 时原有零网络内存 demo 行为不变。
- 构造时会把 reviewed Definition → Release → Trust Passport 及已有 Installation 链按一个 `agent.catalog.sync` 命令补齐；安装和撤权分别按一个 `agent.install` / `agent.offboard` 命令提交 CAS。
- 安装动作先完成 fake provider provisioning，再提交控制面 durable CAS；撤权动作先完成成员移除和 provider revoke，再提交 durable CAS。这样保留了本地验收产品的既有 effect 顺序，同时明确留下 provider effect outbox/reconcile 尚未并入本 seam 的边界。

## 变更位置

- `apps/im-api/internal/localdemo/agent_store_backend.go`
  - 定义 `AgentStoreRecord`、`AgentStoreBackend`。
  - 实现 PostgreSQL adapter；catalog seed 采用幂等的“缺失才 insert”策略，避免覆盖生产已有修订。
  - `CommitInstall` 原子提交新 installation 与旧 active installation 的 offboard CAS。
  - `CommitOffboard` 校验 expected revision 后执行单 installation CAS。
- `apps/im-api/internal/localdemo/service.go`
  - 新增可选 backend 构造路径，并在返回 HTTP 服务前执行 catalog seed。
- `apps/im-api/internal/localdemo/agents.go`、`agents_offboard.go`
  - 在内存投影变更前调用 durable backend；backend 失败统一返回 `ErrPersistence`，不会伪装成成功命令。
- `apps/im-api/internal/app/local_demo.go`
  - 新增 `NewLocalDemoWithAgentStore`；HTTP 路由保持原有 `/api/v1/demo/im/agents` install/offboard 契约。
- `apps/im-api/internal/localdemo/agent_store_backend_test.go`
  - focused seam test 验证 constructor seed、install CAS 参数、retired installation 集合以及 offboard current/next revision。

## 如何接入

```go
backend, err := localdemo.NewPostgresAgentStoreBackend(persistence)
if err != nil { return err }
server, err := app.NewLocalDemoWithAgentStore(backend)
```

其中 `persistence` 必须是 `postgres/imstore.NewUnitOfWork(attestedRuntimePool)` 返回的 tenant unit of work；不能把 owner/migrator pool 或 raw `pgxpool` 直接传入。当前 constructor 使用 localdemo 的固定 `ten_local_demo` synthetic tenant，因此它适合本地/预生产 acceptance composition；真实多租户 HTTP 仍需在 auth identity → tenant membership → trusted request context 完成后再调用同一 backend。

## 验证

已通过：

```text
go test ./apps/im-api/internal/localdemo
go test ./apps/im-api/internal/app
```

覆盖重点：

1. nil backend 继续保持零网络 deterministic demo。
2. 非 nil backend 构造时恰好收到两条 reviewed catalog records（一个 active installation、一个 available release）。
3. install 传递新 installation 与一个待 offboard 的旧 installation，且重复 HTTP action 不会再次调用 backend（由本地 idempotency map 短路）。
4. offboard 传递同一 installation 的连续 revision，状态为 `offboarded`。

## 尚未宣称完成的部分

本 seam 不是完整的生产 Agent Store cutover，以下项目继续列为 P0/P1：

- provider user/member/revoke effect 与数据库控制面尚未共用 outbox/reconcile transaction；进程在 provider effect 与 CAS 之间崩溃时需靠下一阶段 reconcile。
- localdemo 的 conversation/task/artifact/mention 仍是 synthetic memory state；此 seam 只持久化 Agent Store catalog/install/offboard 控制面。
- catalog discovery 仍使用 constructor 提供的 reviewed seed，尚未实现面向真实租户的分页 catalog query 与发布审批入口。
- runtime `NewRuntime` 尚未默认注册这套 demo UI；生产 HTTP 组合必须额外提供 tenant resolver、真实 provider effect worker 和 trusted request context。

因此验收口径是“可选 durable 控制面接缝已闭合”，不是“生产 provider/IM 全链路已经完成”。
