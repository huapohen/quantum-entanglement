# Agent Store Provider Effect PostgreSQL Worker Seam（2026-08-31）

状态：`local_pending`。本阶段代码、测试和文档只写入本地 Git 与私有 GitHub 开发分支；按当前协作约束，Notion 暂不上传，语雀、飞书、企微均未操作，也没有向任何群聊或个人发送消息。

本报告记录的是 Agent Store / Native IM 接入前的一段底层收口：把已经定义的 provider-effect outbox 从“表结构 + enqueue function”推进到一个可以由 worker 使用的 PostgreSQL durable seam。它不是融云真实接入，也不是生产商用全链路完成声明。

## 1. 本阶段要解决的问题

Agent 安装、撤权、建群和消息发送都会产生外部 provider 副作用。数据库事务可以可靠保存“要做什么”，但不能把 PostgreSQL 提交和外部 provider 的网络调用合成一个真正的 exactly-once 事务。因此 worker 必须面对三类现实情况：

1. 领取任务后进程崩溃，provider 可能已经收到请求，也可能完全没有收到；
2. provider 已处理但 ACK 丢失，重试不能产生第二个外部对象；
3. lease 过期后旧 worker 仍尝试写回，不能覆盖新 worker 的结果。

本阶段选择的最小可靠边界是：数据库只负责 durable intent、状态、租约、fencing digest 和 provider receipt evidence；真实 provider adapter、operation-key 幂等保证、readback 查询和部署级告警仍由后续阶段完成。

## 2. 交付内容

### 2.1 migration 0017：function-only worker 写面

新增 `0017_agent_provider_effect_worker_functions`，提供四个 `SECURITY DEFINER`、`STRICT`、`VOLATILE`、`PARALLEL UNSAFE` 函数。函数统一使用 `SET search_path TO pg_catalog`，调用方必须先绑定 `wanwork.tenant_id`，函数再次校验传入 tenant，形成双重 tenant 边界。

| 函数 | 作用 | 返回值 |
|---|---|---|
| `claim_agent_provider_effect` | 对 queued/failed 或已过期 sent 记录执行 `FOR UPDATE SKIP LOCKED` 领取，增加 attempt、设置 lease digest 和 expiry | effect id；无任务返回空字符串 |
| `record_agent_provider_effect_receipt` | 持有有效 lease 时写入 committed/replayed/unknown receipt，清除 lease | boolean |
| `mark_agent_provider_effect_terminal` | 持有有效 lease 时将发送过程标为 unknown 或 failed，并保存错误码 | boolean |
| `resolve_agent_provider_effect` | 只允许把 unknown 通过独立 readback 证据解析为 committed/replayed | boolean |

函数定义、参数 identity、结果类型和 SHA-256 definition digest 均纳入代码拥有的 authority manifest；migration catalog、SQL policy、postcondition、access manifest 和 golden fixture 会拒绝函数签名或 SQL 面漂移。

### 2.2 Go repository：`ProviderEffectRepository`

新增 `apps/im-api/internal/platform/postgres/imstore/provider_effects.go`，实现 `imstore.ProviderEffectOutbox`：

- `Enqueue` 复用 migration 0015 的 function-only writer，支持 exact replay 与冲突识别；
- `ClaimDue` 只调用数据库 claim function，不在 runtime Go 中直接 UPDATE 表；每次领取生成随机 lease token，仅把 token 返回给 worker，数据库只保存 `provider-effect-lease/1` digest；
- lease 上限为 1 小时，worker id、limit 和 duration 均有边界校验；
- `RecordReceipt`、`MarkUnknown`、`MarkFailed` 通过 function 返回的 boolean 判断 lease/state 是否仍有效；
- `ResolveUnknown` 不接收 lease，要求业务层先完成 provider readback 并提供 committed/replayed receipt；
- `Load` 对 nullable receipt evidence、时间顺序、状态形状和 digest 重新验证，数据库异常不会被伪装为业务成功；
- PostgreSQL `23505`、`22023`、约束错误和 `42501` 被映射为明确的冲突、非法请求或 lease 错误。

repository 接受已有 `pgx.Tx`，不接收 owner/migrator pool 或 raw DSN。调用方必须在同一事务上完成 tenant binding；事务 commit/rollback 由上层控制。领取事务应在网络调用前提交，避免持锁访问 provider。

### 2.3 集成测试夹具

新增 `provider_effects_integration_test.go`，在 PostgreSQL 18.6 disposable 实例上覆盖：

1. enqueue 首次写入和 exact replay；
2. function-only claim、attempt=1、lease token 生成；
3. 错误 lease 被拒绝；
4. 正确 receipt 写入 committed 和 provider evidence；
5. 第二条 effect 的 claim → unknown；
6. unknown 通过 replayed readback resolve；
7. migration authority grant 只授予 function execute 与受控 read，不授予 runtime 直接表写权限。

## 3. 状态机与故障语义

```text
queued ──claim──> sent ──receipt(committed)──> committed
   │                 │
   │                 ├─receipt(replayed)───> replayed
   │                 ├─terminal(failed)────> failed ──claim──> sent
   │                 └─terminal(unknown)───> unknown ──readback──> committed/replayed
   │
   └────────────── exact operation-key replay/conflict remains deterministic
```

- `sent` 不是 provider 已成功的声明，只表示数据库已授予本次发送 lease；
- `unknown` 是 fail-closed 状态，禁止自动猜测成功，也禁止把超时直接改成 committed；
- lease 过期的 `sent` 可以再次 claim，但旧 token digest 不再匹配，因此旧 worker 的 receipt/terminal 写回失败；
- `failed` 可重试，`unknown` 必须先经过 provider readback/reconcile；
- receipt 的 operation key、external id、status、observed time 会被重新计算 digest，防止“状态列与证据列”脱节。

## 4. 安全与权限边界

- worker runtime role 只能 `EXECUTE` 四个 function，并可按租户读取 outbox 记录；不能直接 `UPDATE/INSERT/DELETE` `agent_provider_effects`；
- function 使用显式 schema 名称、固定 search path 和 tenant context mismatch 拒绝，降低 `SECURITY DEFINER` 搜索路径劫持风险；
- lease token 不写入日志、报告或数据库，只存不可逆 digest；
- receipt digest 只证明本地收到的结构化证据，没有把 provider 返回正文、凭据或 API key 写入数据库；
- 本阶段没有启用真实 RongCloud outbound，所有 provider effect 仍停留在 fake/local 或受控 seam；
- 本阶段没有实现生产 authority provisioner、TLS/secret provider、跨区域 HA、告警、死信运营台或真实 provider readback。

## 5. 验证证据

在分支 `dev_wanwork_quantum_entanglement` 上执行，Go module 使用本地 toolchain、离线 module cache：

```text
go test ./internal/platform/postgres/imstore -count=1
go test -race ./internal/platform/postgres/imstore -count=1
go vet ./internal/platform/postgres/imstore
go test ./internal/platform/postgres/migrations -count=1 -timeout=15m
go test ./... -count=1 -timeout=30m
go test -race ./... -count=1 -timeout=30m
go vet ./...
```

结果：以上命令全部通过；全量包包含 `cmd/im-api`、`cmd/im-migrate`、Agent Store、IM、authority cutover、connection policy、event store、provider-effect、migration runner、runtime pool 等包。provider-effect 集成测试在本机 PostgreSQL 18.6 disposable 实例上通过。

为计算 migration function digest 临时创建的 `qe_worker_digest_0831a` 已在验证后删除；没有清理其他 PostgreSQL 实例或用户数据。

## 6. Git 记录

本阶段采用小提交：

| 提交 | 内容 |
|---|---|
| `13822b5` | 刷新 authority cutover plan 与 preflight report golden digest |
| `8d6ef2d` | 新增 migration 0017 worker functions、catalog/policy/postcondition/authority fixture |
| `2e9a483` | 新增 PostgreSQL `ProviderEffectRepository` 与集成测试 |

当前开发分支尚未合并 `main`。阶段文档和代码推送完成后会创建 `backup_0831_HHMMSS` 固定备份分支；备份只用于恢复和审计，不作为开发分支。

## 7. 当前可验收范围

可以验收：

- 一个 Agent Store provider-effect intent 能在 PostgreSQL 中 durable 保存；
- worker 能安全 claim、提交 lease、记录 receipt、进入 unknown、通过 readback resolve；
- exact replay、错误 lease、租户错绑、函数定义漂移和直接表写权限均有门禁；
- Go normal/race/vet 与 PostgreSQL migration/integration gate 全绿。

不能验收或不能宣称：

- 真实融云群组、成员、消息发送已经接通；
- provider 的 operation-key exactly-once 已由外部系统保证；
- commit-unknown 后系统会自动知道 provider 真相；
- 生产集群 HA、备份恢复、跨区故障切换、secret rotation、告警和运营台已完成；
- Quantum Entanglement 的整个 IM/Agent 产品已达到商用生产级别。

## 8. 下一步（本阶段暂停后的解锁顺序）

用户验收通过后再继续，建议顺序保持为：

1. 为真实 provider 定义 adapter/readback port，明确 operation key、external id 和错误分类；
2. 实现 worker loop 的 commit-before-network、退避、lease fencing、unknown reconcile 和死信观测；
3. 将 Agent Store durable backend 与安装/撤权/建群/消息命令的同一事务边界接起来；
4. 在受控 sandbox 做 inbound-only/provider callback authenticity 验证，真实 outbound 默认关闭；
5. 再评估 IM 原生接入和 production authority/cutover，补齐 secret、TLS、HA、备份和恢复证据；
6. 最后统一把 `local_pending` 报告批量同步 Notion，并回读核对页面与 Git HEAD。

本报告的结论是“PostgreSQL durable provider-effect worker seam 已闭合，可进入人工验收”，不是“所有计划任务已经完成”。
