# Quantum Entanglement 当前生产就绪审计

- 状态更新：2026-08-20
- 审计对象：本文所在提交可复现的已提交源码；未提交工作树不计入证据
- 结论：**内核组件持续成熟，但尚不是生产服务，所有生产 promotion gate 仍保持关闭**
- 硬边界：[`SERVICE_BOUNDARY.md`](./SERVICE_BOUNDARY.md)

## 执行结论

项目已经实现多项真实的耐久性原语：append-only event store、durable invocation attempt、
lease/heartbeat/fencing、artifact store、transactional inbox/outbox、publisher、projection
offset/receipt、domain-scoped migration、SQLite backup/restore、tenant authorization primitive，
以及请求/决定/状态转换和恢复因果链均经过事务与严格验证的 durable approval。

这些能力解决了早期审计中的一批具体缺陷，但还没有形成可以接收不可信请求、强制多租户
隔离并安全执行外部动作的服务闭环。当前只能在可信本机或隔离 CI 中使用合成数据、fake、
no-op 或只读 fixture。禁止真实飞书/企微发送，禁止公网监听，禁止真实客户敏感数据和不可逆
外部副作用。

当前三个最高优先级断层是：

1. tenant authorization 尚未与 events、delivery、attempt、projection 等全部 repository 的
   强制 tenant/workspace predicate 连接；
2. connector 返回的 accepted receipt 尚未与 outbox ACK、action digest、授权和审批 revision
   组成 durable action state machine；
3. 尚无可信认证入口、严格配置/secret handle、安全日志、统一 lifecycle 和可部署 service。

测试通过证明对应断言在指定环境成立，不证明端到端生产安全、容量、SLO、RPO/RTO 或 GA。

## 能力与缺口审计

### 1. 可靠性与一致性

已提交证据：

- `store.py` 提供 append-only event、optimistic stream version、transactional inbox/outbox 和
  多事件原子 append；
- `attempts.py` 与 `scheduler.py` 提供 durable attempt、lease、heartbeat、fencing、retry 和
  crash recovery；
- `artifact_store.py` 提供 tenant/workspace-scoped metadata/blob、digest、版本与事务恢复；
- `projections.py` 提供严格 schema、lease、offset、receipt、replay 和 tamper detection；
- `runtime.py` 的 approval request/decision 与 task transition 原子提交，恢复时严格核对
  canonical payload、actor、correlation、causation、idempotency 和事件相邻性；
- `delivery.py` 与 `publisher.py` 提供 bounded retry、dead letter、lease 与 ACK 基础设施。

未关闭 P0：

- 没有 API command journal/receipt，无法证明“HTTP 已接收但响应丢失”后的稳定重放；
- 没有 durable action receipt 与 `prepared → dispatching → succeeded|rejected|unknown` 状态机；
- publisher 验证 connector receipt 后没有把 receipt ID 与 action/outbox 原子保存；
- runtime、attempt、artifact 和 result 尚未被一个服务级 composition root 统一提交与恢复；
- 无 crash-at-every-boundary 的完整 fake-effect E2E promotion evidence。

晋级标准：任意持久化、dispatch、receiver accept、ACK 和响应边界崩溃后，只能恢复为“未发生、
已证明成功、明确拒绝或需要 reconcile 的 unknown”，不得盲目重复不可逆动作。

### 2. 身份、授权与安全

已提交证据：

- `tenancy.py` 提供显式 tenant/workspace、member、role、service principal、capability、有效期、
  action/resource/data scope、revision 与 revocation high-water；
- capability 只允许缩权，授权决定可验证并绑定状态 digest；
- approval authority snapshot 被深拷贝并绑定请求/决定因果链；损坏或缺尾历史 fail closed；
- `SECURITY.md` 与 `THREAT_MODEL.md` 记录当前可信边界和主要威胁。

未关闭 P0：

- 没有 Authenticator/OIDC adapter；caller-provided subject 不能视为可信身份；
- 没有只保存 `SecretRef` 的严格配置和 secret provider；
- 日志/异常/公开序列化尚未统一 allowlist 与 redaction，lease token 仍可能被序列化；
- action-time authorization 尚未覆盖每个真实 tool/connector boundary；
- 插件、Agent 和 connector 与主进程共享权限，没有文件、网络、进程、CPU/内存隔离；
- 没有系统性的 SSRF、路径、压缩炸弹、输入深度/大小和 secret-canary promotion suite。

晋级标准：身份只能由验证过的认证层产生；所有受保护动作在执行时重新授权；撤销即时 fail
closed；凭据 canary 在日志、事件、prompt、artifact、异常和 release evidence 中为零。

### 3. 多租户与数据边界

已提交证据：

- tenant authorization domain model 和 revocation anti-rollback primitive 已存在；
- artifact repository 已把 tenant/workspace 作为强制 SQL predicate 和唯一键组成部分。

未关闭 P0：

- events、snapshots、inbox、outbox、attempts 和 projections 仍缺少端到端强制 scope；
- repository 仍存在裸 ID/全局 position 查询；外部 cursor 尚未签名并绑定 scope；
- inbox 相同 key 的不同 payload 尚未统一以 canonical request digest 拒绝；
- legacy 数据没有显式 operator mapping、可重入 backfill 和 NOT NULL contract migration；
- 没有可信 `RequestContext` 把认证 principal、membership revision 与目标 scope 绑定。

晋级标准：每个 repository/API 用完全相同 ID 运行双 tenant 正反测试；任何 scope 替换均只能
拒绝或返回不可枚举结果；SQL、cursor、错误、日志、指标和缓存均不泄漏另一 tenant。

### 4. 协议互操作

已提交证据：

- `protocol.py` 定义 canonical envelope、authority、causation、correlation、idempotency 和 trace
  字段；
- `adapters/a2a.py` 提供数据映射，`langgraph_bridge.py` 提供 LangGraph bridge；
- `adapters/deepseek_harness.py` 保持显式 factory 与隔离端口。

未关闭 P0/P1：

- A2A 仍是 mapping，不是通过官方 SDK/TCK 的 authenticated HTTP/SSE 实现；
- MCP/ACP transport adapter、webhook signature/replay、schema negotiation/upcaster 未闭环；
- 无跨版本 cancel、disconnect、resume、status reconciliation contract suite；
- 不得把本地 mapping 测试描述为真实平台互操作证明。

### 5. API、生命周期与可观测性

当前没有 HTTP/ASGI 服务、OpenAPI、认证 middleware、可信 request context、health endpoint、
统一 startup/SIGTERM lifecycle、结构化安全日志或 OpenTelemetry。现有 CLI/demo 只是可信本地
入口。

未关闭 P0：

- 严格配置、secret handle、redaction、schema preflight 和 composition root；
- `/api/v1` loopback API、固定错误合同、body/depth/concurrency limit、mutation idempotency；
- signed tenant-bound event cursor、bounded SSE 与 disconnect recovery；
- `/livez`、`/readyz`、受保护详细 health、停止 admission、有限 drain 和 lease relinquish。

未完成这些能力前不得监听公网，也不得接入真实 IdP 或客户流量。

### 6. Schema、部署与运维

已提交证据：

- `domain_migrations.py` 与 `migrations/` 提供带 registry/state digest 的 domain migration；
- `admin_cli.py` 提供本地管理入口；
- release evidence、distribution manifest、canonical sdist 与 reproducible-build verifier 已存在；
- Python/toolchain dependency lock 与 CI 完整性检查正在形成可验证供应链基线。

未关闭 P0/P1：

- store 构造仍可能隐式创建/迁移 schema；production 启动没有只读 preflight 和 plan/apply 分离；
- 没有配置 schema、Dockerfile、non-root/read-only-rootfs 单节点部署或 SIGTERM composition；
- 没有 stop-the-world upgrade/rollback rehearsal、支持矩阵和兼容门禁；
- SBOM、漏洞/许可证政策、签名 provenance、artifact signature 和 trusted builder 仍需独立证据；
- PostgreSQL、HA、Kubernetes 只能在单节点 scope/action/lifecycle 闭环后推进。

### 7. 备份、恢复与 anti-rollback

已提交证据：

- `backup.py` 使用 SQLite online backup 到新路径，验证 source identity、manifest、file identity、
  integrity 与 restore precondition；
- `admin_cli.py` 和 `SQLITE_BACKUP_RESTORE.md` 提供本地 backup/verify/restore 流程。

未关闭 P0：

- 当前 core-table manifest 对 projection offset 的历史表名有误，且尚未完整覆盖 projection
  receipt 与 revocation high-water；修复和负向篡改测试是 promotion blocker；
- action/API/audit receipt 加入后必须进入 manifest v2 topology，不得静默忽略缺表；
- restore 后没有 non-emitting reconciliation mode 或外部 monotonic security checkpoint；
- 没有加密 immutable off-host backup、持续调度、告警和实测 RPO/RTO；
- backup cutover 和旧数据删除必须由操作员显式批准，不得自动执行。

晋级标准：复杂数据集恢复后 event/task/artifact/approval/action/projection/security high-water
一致；旧备份不能复活已撤销权限；不一致时服务保持 not-ready。

### 8. 性能、容量与隔离

当前 SQLite 路径以单节点正确性为目标，尚无冻结容量包络。全局并发、tenant/provider quota、
API backpressure、stream buffer、artifact/payload 大小和 worker resource limit 尚未形成统一服务
门禁。

商用前必须提交可复现的 benchmark/soak 证据，记录 workload、数据规模、并发、p50/p95/p99、
CPU、内存、磁盘、queue age 和失败模式。SLO、RPO、RTO 只能引用实测结果，不能引用设计目标。

### 9. 测试与供应链

当前已有广泛的 deterministic unit、recovery、migration、tamper、publisher、tenancy、backup、
distribution 和 approval atomicity 测试；CI 与本地 release evidence 会绑定 source identity。测试
总数会随提交变化，因此本文不使用一个很快过时的数字作为 readiness 结论。

未关闭门禁：

- clean supported Python/OS matrix、clean-clone install/demo/full suite 的 retained evidence；
- 完整 dependency closure、SBOM、vulnerability/license policy、provenance/signature；
- API/connector E2E、cross-tenant property、fuzz、crash-at-every-boundary、load、chaos 和 soak；
- branch protection、review/promotion ownership 与正式 release evidence。

### 10. 产品与人机协同界面

调研与本地报告已描述群聊、任务图、Artifact、Needs You、审计与多 Agent 协作目标，但仓库仍
没有正式 Web/desktop UI 或服务端页面。CLI demo 不能替代产品工作流、权限一致性、刷新恢复、
可访问性、本地化和浏览器 E2E。

首个产品垂直切片必须由同一事件源驱动 conversation、task graph、artifact revision、approval、
takeover/retry/reconcile 与 audit timeline；UI 不得通过隐藏控件代替服务端授权。

## Gate 状态

| Gate | 目标 | 当前状态 | 主要阻断 |
|---|---|---|---|
| A | 离线 tenant-scoped 内核 | 关闭 | config/redaction/schema control/request context/全 repository scope |
| B | authenticated loopback API + fenced fake connector | 关闭 | API/idempotency/action receipt/stream/lifecycle |
| C | 受控私有试点候选 | 关闭 | complete backup/container/upgrade/restore evidence |
| D | 有限商用候选 | 关闭 | quota/OTel/alerts/isolation/security/capacity/soak |
| E | 多实例 GA 候选 | 关闭 | PostgreSQL/HA/Kubernetes/continuous DR |

Gate 是累积条件。完整阶段定义、promotion 和回退规则见 `ROADMAP.md`、`RELEASE_GATES.md`。

## 下一实现顺序

严格按依赖推进：

1. 修复 backup manifest 对 projection/revocation 的覆盖并保留负向证据；
2. 固化 service boundary、严格 config/secret handle 和 safe logging/redaction；
3. 将 migration 从构造器副作用迁出 production startup；
4. 建立可信 RequestContext，然后 expand/backfill/contract 并逐 repository 强制 scope；
5. 实现 authenticated loopback API 与 transactional command receipt；
6. 实现 durable action receipt、fenced fake connector 与 unknown reconciliation；
7. 实现 stream、health、SIGTERM、单节点容器、upgrade/restore rehearsal；
8. 通过 Gate C 后再推进 capacity/OTel/isolation/PostgreSQL/HA/DR。

每项独立行为与证明它的测试放在同一原子提交；每个阶段都必须有运行命令、失败边界、迁移、
回退和证据文档。默认分支在每次提交后保持可运行，但“可运行”不自动等于“可生产晋级”。
