# Agent Store provider effect outbox seam（2026-08-31）

状态：`local_pending`。本文件只记录本地/Git 证据，按当前调度暂不上传 Notion、语雀；没有向飞书或企微发送消息。

## 本轮交付

Commit `e8195c5` 新增 `apps/im-api/internal/imstore/provider_effect_outbox.go` 及对应测试，冻结了
Agent Store 安装/撤权外部副作用的最小 provider-neutral 合同：

- `ProviderEffectIntent` 只保存 tenant/workspace/installation、effect kind、provider realm、外部 subject、
  operation key、request reference 和 SHA-256 request digest；不保存 token、`ext_info`、原始请求正文或连接串；
- `ProviderEffectOutbox` 提供 `Enqueue`（同 key 同 digest replay、digest 漂移 conflict）、`ClaimDue`（租约与尝试
  计数）、`RecordReceipt`、`MarkUnknown`、`ResolveUnknown` 和 `MarkFailed`；
- `unknown` 是平台持久状态，不能被超时或盲重试改成 `committed`；只有独立的 provider readback 通过
  `ResolveUnknown` 才能结束未决；
- committed/replayed/unknown/failed 的状态转移有显式约束；过期或错误 lease 不能写回旧 worker 结果；
- `DurableProviderEffectFileStore` 是单进程 append-only + `fsync` 本地 fixture，启动时恢复最后一条记录；完整
  损坏行 fail-closed，仅丢弃进程崩溃留下的无换行尾部并立即截断+`fsync`。

## 可复核证据

```text
cd apps/im-api
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./internal/imstore -count=1
go test -race ./internal/imstore -count=1
go vet ./internal/imstore
```

三条命令均通过。测试覆盖：

1. enqueue exact replay 与 request digest conflict；
2. claim lease、错误 lease 拒绝、失败后的新 lease 重试；
3. provider timeout 写入 `unknown` 后不进入 `ClaimDue`，重启后仍保留 unknown；
4. committed provider receipt 只能通过 `ResolveUnknown` 写入并恢复；
5. append-only 日志恢复、权限模式、崩溃不完整尾部截断。

## 生产边界

该文件适合本地恢复/合同测试，不是生产数据库实现。生产接入仍必须：

- 在现有 `TenantUnitOfWork.Execute` 的同一 PostgreSQL serializable transaction 中把 Agent Store command 与 outbox
  intent 原子写入；
- 增加 tenant-bound `agent_provider_effects` 表、RLS、精确 function-only 写入口和 repository；
- 由独立 worker 使用 lease/fence 派发 provider 请求，commit unknown 时用新连接执行 provider readback/reconcile；
- 将 installation/offboard 的 active/offboarded CAS 绑定到所有 required effect 的 committed/replayed receipt；
- 接入真实 provider callback/authenticity、重放防护、mapping drift、审计、备份恢复和凭据租约回收。

因此本轮证明的是“可持久化 provider effect 状态机和 future PostgreSQL adapter 的窄合同”，不宣称真实
RongCloud、PostgreSQL outbox worker 或 Agent Store 生产闭环已完成。
