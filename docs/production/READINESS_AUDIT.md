# Quantum Entanglement 初始生产就绪审计（历史）

> 本文保留最初内核基线的审计证据，不再代表当前实现状态。当前审计请阅读
> [`CURRENT_READINESS.md`](./CURRENT_READINESS.md)，强制运行边界请阅读
> [`SERVICE_BOUNDARY.md`](./SERVICE_BOUNDARY.md)。本文中的测试数量、缺失组件和源码行号只适用
> 于下述历史基线，不得用于当前 release promotion。

- 审计基线：`ce50aa8`
- 审计日期：2026-08-20
- 结论口径：审计期间出现的并发未提交代码不计入本审计结论；只有基线中已提交、可复现的内容作为正式证据。

## 生产就绪审计结论

当前项目是一个设计扎实、测试良好的 `0.1.x` 验证性内核，但还不是可部署服务，也不能承载真实客户数据或不可逆外部操作。

当前安全运行边界仅限：

- 单进程、可信调用方；
- 本地或合成数据；
- 内嵌 Agent；
- 无真实不可逆副作用；
- 人工观察下的 demo、单元测试和架构验证。

已提交基线有 54 个测试。审计期间并发工作树曾新增尚未提交的 publisher、tenancy 和 release gate 工作；审计执行测试时工作树中的 64 个测试通过，但其中新增测试不是基线的一部分，因此未将这些并发未提交文件算作正式交付证据。

## 十维审计

### 1. 可靠性与一致性

现状证据：

- SQLite 已有 WAL、事务、流序号、乐观并发、幂等 inbox/outbox：`store.py:41-125,190-341`。
- 已有完成任务及审批恢复：`runtime.py:263-310`。
- 任务实际调用链是 `RUNNING → invocation.started → Agent → artifacts → result → COMPLETED`：`runtime.py:388-503`。
- session 锁只是进程内 `asyncio.Lock`：`runtime.py:163-170,505-529`。
- Artifact 版本号由进程内 `_versions` 长度计算：`artifacts.py:66-121`。

P0：

- 崩溃发生在 `invocation.started` 后、result/COMPLETED 前时，恢复后任务永久保持 `RUNNING`，调度器不会接管、重试或标记未知。
- 多实例可同时通过各自的内存 session 锁调用同一 Agent；相同事件幂等键只会让事件去重，不能阻止两个外部副作用。
- task 状态、多个 Artifact、result、completion 不是一个事务；图状态先在内存改变再写事件，写失败会分叉。
- Artifact 跨进程可计算出相同版本号；没有数据库级 `(tenant, session, name, version)` 唯一约束。
- 外部副作用没有 durable attempt、fencing token、action receipt、UNKNOWN/reconcile 状态；不能诚实承诺 exactly-once。

P1：

- 无任务 heartbeat、超时、重试策略、取消、全局并发配额和 provider circuit breaker。
- `deadline`、`cost_budget`、输出 token/cost 均未执行。
- `_rebuild()` 最多读取 100 万事件，恢复和内存投影无分页、offset/upcaster。
- Acceptance criteria 只是 prompt 文本；Agent 返回任何结果都会直接完成任务。

P2：

- 自动补偿、跨区恢复、主动—主动执行。

验收标准：

- 在 plan/status/invocation/artifact/result/outbox ACK 每个边界注入进程退出，重启后不得永久 `RUNNING`，不得盲目重复未知副作用。
- 两个独立进程并发抢同一任务，只有一个合法 fencing token；旧 worker 的完成必须被拒绝。
- 100 个并发 Artifact writer 产生唯一、连续版本，digest 全部可验证。
- 同一 command 重放 100 次只产生一个逻辑结果；相同幂等键但不同 payload 必须返回冲突。
- 不支持远端幂等/查询的操作进入 `EFFECT_UNKNOWN`，必须人工或 reconciliation 处理，禁止自动盲重试。

### 2. 安全

现状证据：

- Policy 只评估调用者声明的 `ActionIntent`：`policy.py:40-57`。
- Policy 仅在整个 Agent 调用前执行一次，Agent 内部真实 tool call 不再检查：`runtime.py:393-464`。
- Harness 强制显式 factory，但隔离要求只存在于文档说明，代码无法证明实际沙箱：`deepseek_harness.py:69-88`。
- 异常文本和完整 context/artifact 会直接写事件，缺少敏感信息清洗。

P0：

- 每个真实 tool/connector action 必须在执行时重新授权，不能信任 Agent 自报 action。
- secret handle/KMS 接口；凭据不得进入 prompt、事件、Artifact、日志或异常。
- 默认拒绝的文件、网络、进程、工具沙箱；出网 allowlist、DNS/IP 重验证和 SSRF 防护。
- 输入大小、类型、任务数、图深度、Artifact 大小限制。
- TLS、静态加密、日志/事件 redaction。

P1：

- Threat model、abuse cases、插件隔离/签名、供应链扫描、密钥轮换、审计 hash chain、数据保留和删除。
- 插件 hook 目前可任意修改上下文并在进程内执行，也无超时和资源限额。

P2：

- 外部渗透测试、合规控制映射和持续攻击模拟。

验收标准：

- 恶意 Agent 尝试未声明发消息、写文件、访问 metadata IP 或执行 shell，全部在 action boundary 被阻止并留下拒绝审计。
- secret canary 扫描事件、日志、prompt 和 Artifact 结果为零。
- SSRF、路径穿越、压缩炸弹、超大 DAG、畸形 JSON fuzz 均 fail closed。
- capability 被撤销后，即使任务已进入 RUNNING，下一次工具操作也必须失败。

### 3. 多租户

现状证据：

- `events`、`snapshots`、`outbox`、`inbox_receipts` 均无 tenant/workspace 列：`store.py:47-111`。
- Actor 只有 caller-provided ID/name/kind。
- `Authority.data_scopes` 和 `ActionIntent.data_classes/target` 未被 Policy 使用。
- 审批只接受任意 `actor_id` 字符串：`runtime.py:546-578`。

P0：

- Tenant、workspace、member、role、service principal 成为所有对象的必填作用域。
- Repository 方法必须显式接收 scope，禁止依赖可选 filter。
- OIDC/JWT 身份映射、默认拒绝 RBAC/ABAC。
- 审批 capability 绑定 tenant、workspace、principal、action、resource、请求 digest、有效期和 revision。
- 同名 session/task/artifact 在不同 tenant 间完全独立。

P1：

- PostgreSQL RLS、防 noisy-neighbor quota、tenant 级密钥/保留策略/导出删除。
- 管理员 impersonation 需显式 break-glass 审计。

P2：

- Tenant 自管密钥、数据地域和跨组织 federation。

验收标准：

- 对每个 repository/API 做 property-based 交叉租户测试；随机替换任意 tenant/workspace/id 后只能得到拒绝或不可枚举的 404。
- 两个 tenant 使用完全相同的 session/task/artifact ID 不冲突。
- 任意查询、事件流、指标、错误和缓存均不泄露另一 tenant 的标识或基数。
- capability 只能缩窄，不能通过 handoff 扩权。

### 4. 协议互操作

现状证据：

- A2A 目前只是数据结构和 JSON-RPC mapping：`adapters/a2a.py:154-249`。
- 无 HTTP client/server、SSE、认证、重连、status reconciliation 或官方 SDK contract test。
- 仓库没有 MCP/ACP adapter，README 的相关表述超前。
- 内部 Envelope 严格只接受 `qe.agent-envelope/0.1`：`protocol.py:358-362`，无兼容/upcaster。
- A2A data part 只嵌入 WanWork envelope，通用远端不一定能理解。

P0：

- A2A 1.x 真实 client/server、Agent Card discovery/verification、认证、流式状态、取消和 reconciliation。
- MCP tool/resource client，所有工具调用经过 consent、数据分类和 action-time policy。
- Webhook 签名、时间窗、nonce/replay 和 inbox dedupe。
- 内部 schema registry、兼容策略和 upcaster。

P1：

- 官方 SDK/TCK 双向测试、未知字段保留、版本协商、网络中断恢复、规范化错误。
- W3C trace context 的真实传播。

P2：

- ACP/ANP/OASF 等仅在明确业务需要时增加，不应同时自造公网协议。

验收标准：

- 与官方 A2A SDK 双向执行完整 lifecycle。
- MCP 恶意 tool description、tool result 和 prompt injection 不能绕过 policy。
- SSE/stream 中断后按 cursor 恢复，无缺失、无重复应用。
- 当前版本与上一受支持版本全部通过固定版本 contract suite。

### 5. API 与可观测性

现状证据：

- 唯一入口是硬编码 demo CLI：`cli.py:24-110`。
- 无 HTTP API、OpenAPI、鉴权、分页、限流和流式事件。
- Envelope 有 `traceparent` 字段，但没有 tracing 实现。
- 已提交基线无 metrics、structured logging、health/readiness。

P0：

- 版本化 API：session、task、attempt、artifact、approval、event、action receipt。
- 请求幂等、ETag/revision、分页 cursor、统一错误合同、大小和速率限制。
- readiness、liveness、startup 和 dependency health。
- 结构化日志、OpenTelemetry trace/metrics、不可变业务审计。
- 可恢复事件流 API。

P1：

- Dashboard 和告警：queue age、stuck attempts、DLQ、latency、error、provider cost、approval age、backup age。
- 运维端重试、reconcile、DLQ replay 操作。

P2：

- 产品分析、质量反馈和成本预测。

验收标准：

- OpenAPI backward-compatibility gate。
- 每个请求均有 request/trace/tenant/correlation ID，且日志通过敏感信息扫描。
- 故障演练能在 SLO 窗口内触发告警。
- 断流后从 cursor 恢复，投影结果与完整重放一致。

### 6. 部署运维

现状证据：

- 无 Dockerfile、Compose/Kubernetes、process supervisor、环境配置 schema、signal handling。
- 无数据库迁移工具；初始化仅 `CREATE TABLE IF NOT EXISTS`。
- 无依赖 lock、SBOM、镜像签名、升级/回滚脚本。
- `pyproject.toml` 仍为 `0.1.0` 且只定义 demo entrypoint。

P0：

- 可验证配置模型、明确 data/runtime 目录、非 root 容器、只读 rootfs。
- schema version 和 forward migration。
- SIGTERM 停止接单、drain/relinquish lease、限时退出。
- 安装、启动、停止、升级、回滚、故障恢复 runbook。
- 一个明确支持的单节点 topology。

P1：

- PostgreSQL、Kubernetes/Helm、滚动升级、Pod disruption、资源 limit、autoscaling。
- 锁定依赖、SBOM、签名和 provenance。

P2：

- 离线部署、多区域和自动灾切。

验收标准：

- 全新主机从 release artifact 一次部署成功。
- 从上一 tag 自动升级并按文档回滚，数据和事件不丢失。
- SIGTERM 时不领取新任务，进行中任务完成或安全释放 lease。
- 重复构建产物可复现，镜像以非 root 运行并通过漏洞策略。

### 7. 备份恢复

现状证据：

- 有 snapshot 表和方法，但 runtime 未使用。
- 无备份、PITR、restore、integrity check 或灾备脚本。
- Artifact 内容直接嵌在事件 JSON 中；无 blob store 生命周期。
- SQLite WAL 备份一致性未处理。

P0：

- 事务一致的 SQLite/PostgreSQL 备份，包含 WAL、schema、Artifact blobs 和加密元数据。
- restore + projection rebuild + digest/invariant verification。
- 明确 RPO/RTO、异地加密存储和备份失败告警。
- 数据损坏时进入只读/隔离模式，禁止继续扩大损坏。

P1：

- PITR、保留策略、密钥轮换、删除/legal hold、定期灾备演练。
- Dead-letter 和 effect-unknown 的恢复流程。

P2：

- 跨区域自动恢复。

验收标准：

- 含运行中任务、待审批、DLQ、多版本 Artifact 的数据集备份恢复后，事件数量、stream head、projection、digest 和 receipt 完全一致。
- 定期破坏主数据库后从备份恢复，并记录实际 RPO/RTO。
- 恢复工具拒绝错误 schema、错误密钥或不完整 blob 集。

### 8. 性能容量

现状证据：

- 单 SQLite connection + 全局 `RLock` 串行所有 session：`store.py:31-39,115-125`。
- `read_stream/read_outbox` 使用 fetch-all。
- ArtifactLedger 启动扫描最多 100 万事件，并把所有内容放入内存。
- 每个 session run 单独创建 semaphore；多个 session 总并发无上限。
- ready task 全量创建 coroutine。
- token 估算仅 UTF-8 bytes/4，对中文和不同模型不可靠。

P0：

- 发布明确容量包络、全局并发/队列/tenant/provider quota 和 backpressure。
- 所有列表分页，payload/blob 大小受限。
- provider rate limit、timeout、circuit breaker。
- 大历史用 projector offset/snapshot，Artifact blob 脱离热事件行。

P1：

- PostgreSQL 分区/索引、worker pool、缓存策略、恢复时间优化。
- noisy-neighbor 和长时间 soak。

P2：

- 自动扩容、多区调度和成本优化。

建议首个单节点验收基线：

- 100 万事件数据集启动恢复小于 60 秒；
- 100 个并发 command、20 个并发 Agent attempt；
- 除模型耗时外，command acceptance p95 小于 250ms、p99 小于 1s；
- 24 小时 soak 无持续内存增长；
- 达到容量限制时返回可重试的 backpressure，而非 OOM；
- 单 tenant 不得突破配置配额挤占全部 worker。

具体数字可在首轮 benchmark 后调整，但必须在 tag 前冻结并发布。

### 9. 测试与发布

现状证据：

- 已提交 54 个测试，主要为内存/SQLite unit tests。
- 无 CI、coverage gate、static/type gate、migration、E2E、fault injection、security、load、protocol TCK。
- 当前并发 WIP publisher 测试尚未提交，不能计为 release evidence。
- 无 changelog、release manifest、兼容矩阵和签名构建。

P0：

- 每个 commit 通过 unit、lint、type、schema/migration、integration。
- crash-at-every-boundary、双 worker fencing、tenant isolation、backup/restore、API/protocol E2E。
- branch protection，禁止红色默认分支。
- 每个 schema 变更包含迁移、兼容和 rollback。

P1：

- 核心分支覆盖率建议不低于 85%，并补 mutation/property/fuzz。
- 真实 SDK sandbox contract、负载、chaos、72 小时 RC soak。
- 锁定依赖、SBOM、漏洞/许可证 gate。

P2：

- 更长 endurance、多平台认证和形式化状态机验证。

验收标准：

- clean clone 在声明的 Python/OS/DB matrix 全绿。
- release candidate 无未解决 P0/P1，完成 soak。
- wheel/container 可复现，并有 commit、SBOM、provenance 和验证报告。
- 每次 release 可从上一支持版本升级并回滚。

### 10. 产品界面

现状证据：

- 无前端、无服务端页面。
- CLI 只输出一次性 JSON。
- Needs You、任务图、Artifact 版本和审计虽有领域对象，但用户不可操作。

P0：

- 同一事件源驱动的群聊、Agent 身份、任务图、实时状态、Artifact、Needs You、审计时间线。
- approve/reject/revise/takeover/cancel/retry/reconcile 明确交互。
- Artifact diff、来源、版本、回滚和下载。
- UI 权限与 API 权限一致，不可只在前端隐藏。
- 所有真实外发操作必须有清晰目标、预览、权限和确认。

P1：

- 搜索、通知、成本/token/延迟、失败诊断、管理员控制台。
- WCAG 2.1 AA、键盘操作、中英文、本地化和响应式验证。

P2：

- 工作流模板、Agent/技能市场、协作分析和多端体验。

验收标准：

- 浏览器 E2E 完成“建会话→多 Agent 执行→审批→Artifact 修订→回滚→审计”。
- 刷新、断网、服务重启后 UI 与事件投影一致。
- 无权限用户无法从 URL、API 或 stream 观察资源。
- 飞书/企微 connector 在获得新的明确授权前只使用 fake 和只读 fixture，绝不做真实发送测试。

## 从验证性内核到服务的关键断层

最关键的不是再增加几个领域类，而是补齐以下闭环：

```text
认证命令
  → tenant/action-time policy
  → 原子持久化 command + attempt + outbox
  → lease/fencing worker
  → connector idempotency/reconciliation
  → receipt/result/artifact 原子收口
  → projection/API/stream
  → UI/监控/告警
  → backup/restore/upgrade
```

当前内核只覆盖这条链的部分领域模型和本地执行。

## README 三项必须纠正

- “模型可见即已记录”只在 Orchestrator context 路径被强制；MentionRouter 本身并不持久化。
- ACP/MCP adapter 尚不存在。
- “可恢复”不包含运行中 attempt 的 crash recovery。

## 建议发布阶段和最小可运行边界

### `0.2.0`：可靠单节点沙箱

必须包含 outbox publisher、durable attempts、lease/heartbeat/fencing、effect receipt、原子结果收口、Artifact 持久化、迁移、备份恢复、生命周期和本地可观测性。

正式运行边界：内部受控环境、fake/read-only connector、无客户敏感数据、无不可逆真实副作用。

### `0.3.0`：安全私有试点

增加 tenant/workspace、认证授权、action-time capability、secret handle、沙箱/出网策略、输入限制，以及最小认证 API 和 Needs You/UI。

正式运行边界：单租户私有部署或少量隔离试点。只有实现幂等/reconciliation 的白名单 connector 可产生真实副作用。

### `0.4.0`：有限商用

增加正式 A2A/MCP、签名 webhook、stream API、完整核心 UI、协议 TCK、安全/恢复/性能报告。

这是首个可收费的有限生产边界。必须有明确 SLO、支持拓扑、容量、RPO/RTO 和 connector 白名单。

### `0.5.0`：多实例生产

PostgreSQL、分布式 leasing/fencing、完整 OTel、HA、负载/chaos/noisy-neighbor、autoscaling。

### `1.0.0`：GA

签名发布、SBOM/provenance、长期兼容/弃用政策、升级/回滚/DR/事件响应、外部安全评估和 RC soak。

## 每阶段强制文档

每个阶段都应有：

- scope 与明确不支持项；
- 架构图和 ADR；
- 数据 schema、迁移和 rollback；
- API/协议合同；
- threat model 与安全验收；
- 安装、运维、备份、恢复、升级 runbook；
- SLO、容量和性能报告；
- 测试/故障注入/安全验收证据；
- CHANGELOG、release checklist、SBOM/provenance。

建议 commit 原则是“一项独立行为 + 证明它的测试 + 必要文档”，每个 commit 都必须保持默认分支可运行；小步提交不能以留下不可运行的中间状态为代价。
