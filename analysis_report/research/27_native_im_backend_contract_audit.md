# 独立原生 IM 后端真实入站合同复核

> 复核日期：2026-08-28（Asia/Shanghai）
>
> QE 评审分支：`mainline_continue_quantum_entanglement`
>
> 被复核 IM 分支：`dev_wanwork_quantum_entanglement`
>
> 被复核远端提交：`c623aeadc0693e63c0d34602ed45ae1d2bc8099f`
>
> 被复核提交 tree：`7319fa96c2544fe9bce3e2160a77e77eec403092`
>
> 决策性质：Level B inbound-only 介入前的源码合同审计；不是生产发布批准
>
> 安全边界：本次只读检查已提交源码和文档；未读取 `.env`、未解析任何密钥、未连接任何
> IM endpoint、未向任何人/群/bot/webhook 发送消息，也未修改被复核 worktree。

## 1. 执行结论

独立原生 IM 后端已经形成有价值的 **authority persistence substrate**，包括平台身份、普通会话、
provider mapping、membership/access、PostgreSQL transaction/UoW 和数据库级失败关闭约束。它可以
成为未来真实 IM 入站合同的权威底座。

但是截至被复核提交 `c623aea`，它还没有形成可供 Quantum Entanglement（QE）E2 provider bundle
连接的真实入站 provider contract。当前 HTTP composition 只有：

```text
GET /health/live
GET /api/v1/system/ping
```

并且配置明确固定为 fake auth、fake IM、outbound disabled，只读取 loopback listen address，故意不读
provider endpoint、credential 或真实 provider 选择。没有已注册的 authenticated read endpoint、
provider readiness、event page schema、cursor、snapshot token、rate/error contract 或 production
exchange。

因此当前决策是：

1. **不构造虚假的真实 provider profile。** `/health/live` 只证明 Go 进程 HTTP liveness，不证明
   provider dependency、事件读取、租户授权或可恢复游标；
2. **不把 PostgreSQL authority repository 当成 QE 入站 transport。** 内部 repository/UoW 不是
   versioned HTTP/provider contract，也没有允许 QE 直接依赖内部数据库；
3. **不连接真实网络。** QE 继续停在已完成的 provider-bundle 离线闭环；
4. **Level B 直接阻断仍为三类输入：** 后端合同、批准 scope、真实 profile/mapper/exchange；
5. **E3/E4 不必等待。** Result Authority、PURE Agent draft 和 Action Plane 仍是接入后的独立 TODO，
   不应重新混入 Level B inbound-only 的前置清单。

## 2. 复核方法与证据口径

### 2.1 固定源码边界

本次不以另一个 worktree 的瞬时工作区内容作为真相源，而以其已提交 `HEAD` 为证据基线：

```text
branch: dev_wanwork_quantum_entanglement
commit: c623aeadc0693e63c0d34602ed45ae1d2bc8099f
tree:   7319fa96c2544fe9bce3e2160a77e77eec403092
```

被复核 worktree 当时存在一个与本审计无关的未提交测试文件修改：
`apps/im-api/internal/platform/postgres/migrations/access_manifest_integration_test.go`。本报告没有读取、
修改、暂存或依赖该未提交 diff；所有关键判断均通过 `git show HEAD:<path>` 和 `git grep HEAD` 对已
提交对象复核。

### 2.2 直接证据

| 证据 | 已提交源码事实 | 对接含义 |
|---|---|---|
| `apps/im-api/README.md:3-4` | 当前模块起步时没有 Clerk、RongCloud、数据库或 outbound adapter | 文档没有宣称真实 provider composition 已成立 |
| `apps/im-api/README.md:20-22` | 只读 listen address；auth/IM 固定 fake，outbound 固定 disabled；endpoint/credential 故意不读 | 没有 production exchange 配置入口 |
| `apps/im-api/README.md:24-39` | 当前 endpoint 只有 liveness 和 ping；health 不证明 provider delivery | liveness 不能映射为 QE provider health evidence |
| `internal/app/app.go:10-26` | composition 明确 zero-network，外部 provider 未注册；仅注册 `/health/live` 和 system routes | 运行时不存在 inbound read composition root |
| `internal/adapters/httpapi/routes.go:5-8` | system route 只有 `/api/v1/system/ping` | 没有事件、cursor、snapshot 或 provider-ready route |
| `internal/config/config.go:9-31` | 仅有 listen address；Provider 只有 fake；outbound 只有 disabled | 无真实 endpoint class、auth 或 SecretRef binding |
| `internal/config/config.go:72-100` | 默认与 validation 都强制 fake/fake/disabled、numeric loopback | 即便设置环境变量也不能偷偷启用真实 provider |
| `apps/im-api/README.md:120-138` | EventStore production composition 不存在；当前 store 是 volatile fake | 内部 cursor fake 不能替代 durable inbound cursor |
| `apps/im-api/README.md:152-164` | RongCloud metadata codec 是 zero-authority projection；真实 readback/auth/dedupe/resume 未证明 | codec 通过不等于 provider 互操作通过 |
| `analysis_report/research/33_postgres_authority_persistence_checkpoint.md:36-40` | PostgreSQL 只完成 authority persistence substrate，adapter/event store/outbox 等仍缺 | 持久化基础不能冒充网络合同 |
| 同文件 `:55-64` | 文档主动禁止宣称 provider binding、消息投递、生产 EventStore 等已完成 | 本次 NO-GO 与 IM 分支自身边界一致 |

### 2.3 负向搜索

对已提交的 HTTP adapter、application composition、config、入口和 IM 架构/计划执行了以下语义搜索：

```text
health | ready | read | cursor | snapshot | inbound | webhook | route | Listen
```

结果中不存在注册到 Fiber 的 authenticated inbound event route、read page route、provider readiness
route 或 cursor/snapshot route。架构和实施文档出现的相关词条是目标设计、fake contract 或后续 W3
门禁，不是可调用的当前实现。

## 3. QE E2 所需合同与当前后端逐项对照

| QE 真实 provider 输入 | 当前已观察事实 | 状态 | 为什么仍阻断 |
|---|---|---|---|
| 独立 liveness | `GET /health/live`，HTTP 200 `{"status":"ok"}` | 部分存在 | 只证明进程存活，不证明 provider dependency |
| provider readiness | 无已注册 route/typed response | 缺失 | QE 不能判断真实 provider 是否可读 |
| authenticated read endpoint | 无 | 缺失 | 没有允许范围内的 event source |
| service authentication | 当前 auth provider 固定 fake | 缺失 | 没有可批准的 read-only SecretRef audience/scope |
| event schema/version | 内部领域对象与 codec 存在，但无 HTTP event page schema | 缺失 | mapper 无真实 wire fixture |
| stable event/dedupe identity | authority mapping 有底座，无入站 event ID 合同 | 缺失 | 不能证明重放不会重复 admission |
| sequence/order scope | 无 provider page 合同 | 缺失 | 无法验证 gap、reorder、cross-stream 混淆 |
| opaque cursor | memory EventStore fake 有内部 cursor；无 production inbound cursor | 缺失 | fake checksum 不能作为外部恢复令牌 |
| snapshot token/cutoff | 无 | 缺失 | backfill 与后续页可能形成撕裂读 |
| pagination/limit semantics | 无 | 缺失 | transport 不能约束 page budget 或终止条件 |
| 429/rate limit/Retry-After | README 只定义未来 business envelope 语义 | 缺失 | 无确定的 retry/backoff 分类 |
| auth/validation/not-found/5xx taxonomy | 通用 envelope 有方向，但 read route 不存在 | 缺失 | 无法映射 provider-specific errors |
| timeout/maintenance contract | 无 | 缺失 | 无 bounded deadline 与维护窗口语义 |
| endpoint class/DNS/TLS/IP/redirect | config 故意不读取 endpoint | 缺失 | production exchange 无可准入目标 |
| body/header limits | 通用 HTTP 框架存在，入站 read 合同无冻结限制 | 缺失 | 不能形成 transport TCK 的拒绝矩阵 |
| test tenant/workspace/conversation scope | authority 模型存在，未形成批准 allowlist | 缺失 | 即使 endpoint 出现也无权读取任意数据 |
| synthetic-only data classification | 无批准记录 | 缺失 | 不能证明试验数据等级和保留边界 |
| kill switch + expiry | QE 侧离线原语已实现；无本次真实 scope 输入 | 输入缺失 | 无法实例化实际批准快照 |

结论：目前不是“mapper 少写几个字段”的问题，而是 provider wire truth 尚未冻结。现有 QE Mapper、
Transport、Bundle TCK 可以承接真实合同，但不能替真实 IM 后端发明合同。

## 4. 为什么不能把现有对象硬接起来

### 4.1 `/health/live` 不是 provider health

它没有 provider identity、provider profile revision、dependency result、tenant scope、认证证据或
read capability。将其包装成 `NativeIMHealthEvidenceV1(healthy=true)` 会把“进程在”错误升级为“真实
事件源可读取”，破坏 E2 的 provenance 语义。

### 4.2 `/api/v1/system/ping` 不是 read contract

ping 返回 business envelope，但不承载 event page、稳定 cursor、snapshot cutoff、sequence、
event source digest 或 provider rate/error metadata。它只能证明 envelope 基础设施可工作。

### 4.3 直接读 IM PostgreSQL 会制造第二条旁路

即使 authority tables 已存在，QE 也不应直接拿数据库 credential 查询它们：

- IM repository 是内部 Go port，不是跨 bounded-context wire contract；
- 当前没有 message/event/outbox 的 production persistence；
- 直接查询会绕过未来 trusted request context、service authorization、rate limit 和 audit；
- 数据库 schema 演进会与 QE transport 强耦合；
- 读权限会远大于 Level B 所需的 allowlisted conversation scope。

正确路径仍是由 IM 后端拥有 authenticated versioned read API 或受控 stream contract，QE 只实现边缘
mapper/transport。

### 4.4 provider metadata codec 不是 event authentication

`ext_info` 的 canonical codec 和 forbidden-field matrix 很有价值，但它只是 zero-authority projection。
合法 JSON 仍可能指向不存在、已撤销或错误 scope 的 Actor/Conversation；入站必须在服务端完成认证、
binding、membership/status 与 policy resolution，QE 不可依据 metadata 自授权。

## 5. IM 后端最小 Level B 合同包

为不阻塞后续实现，独立 IM 后端只需先冻结 **inbound-only observation** 的最小合同；无需等待
Agent draft、Task terminal、Artifact acceptance 或 outbound Action Plane。

### 5.1 必须提供的三个 operation

```text
GET /health/live
  只做进程 liveness；不得用于 provider readiness

GET /api/v1/integrations/qe/readiness
  返回合同版本、provider profile revision、read capability、dependency 状态和当前维护状态

POST /api/v1/integrations/qe/events:read
  在 authenticated service identity 和 exact allowlist 下读取一页稳定事件
```

路径名可以改变，但三种语义必须分开，且最终以 versioned OpenAPI/JSON Schema/golden fixture 为准。

### 5.2 read request 最小字段

```text
contractVersion
requestId
tenantId
workspaceId?
channelId?
conversationIds[]       # exact allowlist subset，禁止空值解释为“全量”
afterCursor?
expectedNextSequence?
snapshotToken?
limit
deadline
```

### 5.3 read response 最小字段

```text
contractVersion
providerProfileRevision
requestId
snapshotToken
events[]
nextCursor?
hasMore
observedAt
rateLimit?
```

每个 event 至少包含：

```text
eventId
eventType
schemaVersion
tenantId
workspaceId?
channelId?
conversationId
threadId?
messageId?
participantId
sequence
occurredAt
payload
```

event ID、sequence 和 cursor 的作用域必须明示。`eventId` 是否全局唯一、conversation 内唯一或只在
provider realm 内唯一不能靠命名猜测。

### 5.4 必须冻结的失败语义

至少独立区分：

```text
unauthenticated
unauthorized_scope
invalid_request
unsupported_contract_version
cursor_invalid
cursor_expired
snapshot_invalid
snapshot_expired
sequence_conflict
rate_limited
temporarily_unavailable
maintenance
internal_error
```

并为每类定义：HTTP status、business code、是否可重试、Retry-After、是否必须从 snapshot 重新开始、
是否需要人工审批或 kill switch trip。timeout 不能自动当作“没读到事件”。

### 5.5 认证与批准输入

真实接入只接受 opaque、read-only 的 `SecretRef` 或 workload token exchange reference，不在 Git、
Notion、报告、事件、日志或 model context 放置材料。批准记录必须同时绑定：

- endpoint class 与环境（只能是专用 sandbox）；
- tenant/workspace/channel/conversation exact allowlist；
- synthetic-only 数据等级和附件策略；
- operation 只能是 `health/read`；
- provider profile/config/mapper/exchange digest；
- secret reference 的 broker、purpose、audience 和 policy revision；
- 生效时间、截止时间、审批人和撤销人；
- process-bound kill switch 与停止条件。

## 6. 后端合同到现有 QE 实现的映射

当上述合同冻结后，QE 不需要重做 E2 底座，只需提供三项真实输入：

| 后端交付 | QE 落点 | 验收 |
|---|---|---|
| versioned OpenAPI/schema + capability answers | `NativeIMProviderProfileV1` / provider config | unknown 明确标记，禁止 fake 默认值 |
| signed/authenticated golden request/response fixtures | provider-specific mapper | 复用 Mapper TCK，真实 accepted/rejected matrix |
| approved endpoint/SecretRef/exchange policy | production exchange | 复用 Transport TCK，DNS/TLS/IP/redirect/timeout/body-limit/credential 全验证 |
| 上述三项的 immutable bundle | atomic provider bundle admission | profile/config/manifest/mapper/transport provenance 一次性准入 |
| sandbox read exchange | durable observation admission | health → read → dedupe → resume；不驱动 Agent/outbound |

QE 已有的 stable event-source evidence 与 transient read-exchange evidence 分离必须保留：一次 HTTP
exchange 的 TLS/trace/request/response 证据可以变化，但同一个 provider event 的 source digest 不应因
重读而漂移。

## 7. Level B 第一轮联调的精确顺序

真实输入齐备后只按以下顺序执行：

1. 离线校验 provider profile、config、manifest、mapper golden 与 exchange policy；
2. 原子 admission provider bundle，确认 kill switch 默认为 trip/disabled；
3. 在批准窗口显式 arm，只调用 provider readiness；
4. 读取一个 bounded synthetic page，不调用 Agent、tool、browser、subprocess；
5. 精确重读同一页，验证 dedupe 与 source digest 稳定；
6. 使用 `nextCursor + snapshotToken + expectedNextSequence` 续读；
7. 注入 duplicate、gap、reorder、cursor-expired、snapshot-expired、429、5xx、timeout 和 malformed body；
8. 验证 durable observation/checkpoint/readback，确认零 outbound；
9. trip kill switch、关闭 lifecycle、验证之后任何 read 都 fail closed；
10. 生成只读 evidence bundle 并停止。是否进入 Agent draft 由后续独立批准决定。

## 8. 当前可继续与必须等待的事项

### 8.1 QE 当前可继续

- E3 Result Authority：accepted result/artifact + attempt + terminal task 的 store-owned atomic boundary；
- PURE/fake Agent draft worker 与 receipt-bound crash recovery；
- E4 fake-only Action receipt 与 `effect_unknown` reconcile；
- authenticated loopback API、service readiness/drain、tenant isolation、restore/upgrade 等生产门禁；
- 继续完善文档、TCK 和 synthetic provider adversarial fixtures。

这些工作不会自动授权真实 IM 网络，也不会补出缺失的 provider wire facts。

### 8.2 Level B 必须等待

1. IM 后端提交或提供 versioned readiness/read contract 与真实 golden fixtures；
2. 用户/系统提供 exact sandbox scope、synthetic data classification、expiry、kill switch 与 read-only
   `SecretRef`；
3. QE 在该合同之上实现真实 profile/mapper/production exchange，并通过已有 TCK；
4. 单独修订 `docs/production/SERVICE_BOUNDARY.md` 后才允许第一次真实 health/read。

## 9. 最终判断

| 问题 | 判断 |
|---|---|
| 独立 IM 后端是否“什么都没有”？ | 否。authority persistence、插件/配置边界和数据库不变量已经有实质价值。 |
| 是否已经能让 QE 做真实 inbound-only？ | 否。公开运行合同仍只有 liveness/ping，没有 authenticated event read。 |
| 能否先拿 `/health/live` 冒充 provider profile？ | 不能。它会把进程存活误报为 provider 可读。 |
| 能否让 QE 直连 IM PostgreSQL？ | 不应。会绕过服务合同与最小权限，且 message/event/outbox 尚未完成。 |
| 是否必须先做完 E3/E4 才能接？ | 否。Level B 只被真实合同、批准 scope、profile/mapper/exchange 阻断。 |
| 当前最短路径是什么？ | IM 后端先交付 readiness + authenticated paged read + schema/golden；QE 随即套入现有 E2 bundle/TCK。 |

本次审计没有发现可安全替代缺失合同的本地事实。真实 Level B 保持 **NO-GO**，原因不是网络波动，
也不是 QE 离线能力不足，而是独立 IM 后端尚未暴露真实入站读合同及其批准输入。
