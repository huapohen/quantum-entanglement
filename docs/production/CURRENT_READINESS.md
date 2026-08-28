# Quantum Entanglement 当前生产就绪审计

- 更新日期：2026-08-28
- 审计口径：只计算本文所在评审分支中已提交、可复现的源码和证据
- 硬边界：[`SERVICE_BOUNDARY.md`](./SERVICE_BOUNDARY.md)
- 结论：**内核组件已形成较强验证基线，但仍不是生产服务；Gate A–E 全部关闭**

## 执行结论

项目已不再是只有任务图和 demo 的空架子。当前主线包含 append-only event store、原子 workflow
初始化与 approval、严格流式 session recovery、event/RUNNING transition + queued invocation job +
immutable admission receipt 的原子 admission、admission-gated job/attempt/start-event 原子首次
claim、durable invocation-attempt store、持久 artifact store、transactional inbox/outbox、publisher、
leased projection、domain migration、tenant
authorization primitive、SQLite backup/restore、严格 service config、opaque/file secret provider、
依赖锁、可复现制品、source-bound 双 SBOM、publisher typed safe logging/redaction，以及一个
明确标记为非生产的 loopback-only 模型试用页面。该页面可把任意自定义指令交给 GPT
`gpt-5.6-sol`，按分析、生成、复核三 Agent DAG 生成三个 Markdown Artifact；它是当前产品体验
证据，不是正式 service composition root，也不改变任何生产 Gate 状态。

当前包的诚实 Python 兼容窗口是 `>=3.9,<3.14`。CPython 3.14 已复现 `95 failures / 7 errors`：
protected-operation context protocol 依赖的特殊方法 descriptor lookup 顺序发生变化，且其 SQLite
3.53.2/`ENABLE_STAT4` 会让 backup-v2 schema catalog 多出 `sqlite_stat4`。在两条路径重写、3.14
lock/CI 和完整安全回归通过前，3.14 不属于支持版本；启动脚本会在加载产品代码前拒绝它。

这些能力分别有真实代码和负向测试，但尚未被可信认证入口、强制 tenant-scoped repository、
runtime attempt/result 状态机、durable action receipt 和统一 service lifecycle 串成闭环。因此当前
只能在可信本机或隔离 CI 中使用合成数据、fake、no-op 或只读 fixture。禁止真实飞书/企微
发送，禁止公网监听，禁止真实客户敏感数据和不可逆外部副作用。

当前最关键的三个断层是：

1. `SQLiteEventStore` 已能原子提交 caller-supplied `RUNNING` transition、queued job 和 admission
   receipt，并由 `claim_invocation_start(...)` 在同一 SQLite transaction 内完成 admission
   复核、job CAS、attempt、schema-2 start event 与 readback；但 `OrchestratorKernel` 尚未使用这些
   API，也尚无 heartbeat-supervised worker、result/artifact receipt 与 terminal projection 闭环；
2. events、snapshots、delivery、attempt 和 projection repository 尚未统一强制 tenant/workspace
   scope，tenant domain object 不能替代可信认证与 SQL predicate；
3. connector acceptance 尚未与 action digest、authorization/approval revision、outbox ACK 和
   `succeeded | rejected | effect_unknown` receipt 原子绑定。

新增的模型试用路径也暴露了必须先收口的安全边界：`examples/product_trial_server.py` 当前从
本地 `.env`/进程环境加载 provider 配置，并在同一进程中构造模型 runtime；它尚未经过
`ServiceConfig + SecretRef + SecretProvider` composition、spawn/exec-before-secret-load、统一
出网策略或全输出 secret-canary 门禁。因此它只允许可信开发者在本机回环地址上使用显式批准的
测试数据，不能升级为生产凭据入口或真实客户数据处理路径。

另有一个跨领域 P0：当前主线已经实现统一、无锁的 PID + opaque epoch process identity
foundation，并完成 `SQLiteEventStore` 的独立 process-bound 候选；artifact/projection/revocation
store、`RequestContextIssuer`、key/secret registry、plugin/runtime、connector 和 composition root
仍未完成同等级迁移。这些对象仍可能在 child 触碰 inherited lock/provider/connection 前继续运行。
`non-copyable`/`non-pickleable` 不阻止 fork 复制；credential-bearing 或不可信 worker 必须先
spawn/exec，再在 child 内构造 store、issuer、provider 和 event loop，并且必须发生在 secret load
之前。event-store mismatch child 即使已经稳定拒绝，也必须停止 admission 并以 `os._exit`/exec
替换；普通解释器 teardown 不能安全销毁 inherited SQLite graph。已实现基础合同见
[`PROCESS_INHERITANCE.md`](./PROCESS_INHERITANCE.md)，完整依赖与测试矩阵见
[`12_process_inheritance_dependency_audit.md`](../../analysis_report/research/12_process_inheritance_dependency_audit.md)。
event store 的单组件运行合同见
[`SQLITE_EVENT_STORE_PROCESS_BINDING.md`](./SQLITE_EVENT_STORE_PROCESS_BINDING.md)。

测试通过证明对应断言在记录环境中成立，不证明端到端生产安全、容量、SLO、RPO/RTO 或 GA。

### 原生 IM E1 与 E2 provider bundle 离线状态

E1 / Level A `CONTRACT_EXECUTABLE` 已在源码候选 `7620200` 完成；内部
`IM-P0 CONTRACT_READY` 仅按 provider-neutral contract/fake 里程碑完成。当前新增能力包括 21 个
V1 wire model、strict codec、23 个代表性 positive golden vectors、exact 四方法
`IMGatewayPort`、纯 result admission、默认 outbound 拒绝、进程本地不可序列化 fake permit、
receiver action/key 双账本、ACK-loss/post-accept exception 与 acceptance-query 故障语义，以及
socket/DNS/network-import/environment-credential zero-network gate。运行边界和证据见
[`NATIVE_IM_P0_CONTRACT_EXECUTABLE.md`](./NATIVE_IM_P0_CONTRACT_EXECUTABLE.md)。

E2 的离线原子 inbox 在运行源码 `9cf1bfe` 完成；其后续 default-off adapter/lifecycle 节点又在
运行源码 `2bdaea1` 完成：显式 inbound-only adapter、signed raw body/canonical page 分离、bounded
parser、process-bound kill switch/lifecycle、typed observability、全链 canary、recorded
disconnect/resume/duplicate/out-of-order/conflict probe、取消/关闭恢复和扩展 zero-network gate 均已
实现。其后 provider bundle 离线节点又在 `ee0666f` 完成 Mapper/Transport/Bundle TCK、
zero-network exchange、稳定 event-source 与 transient read-exchange evidence 分离、增强 admission
provenance 和 migration-v6 durable readback。admission 不调用 gateway、Agent、plugin、browser、
network、subprocess 或 outbound。阶段证据见：

- [`24_native_im_e2_atomic_page_admission_evidence.md`](../../analysis_report/research/24_native_im_e2_atomic_page_admission_evidence.md)；
- [`25_native_im_e2_adapter_lifecycle_offline_evidence.md`](../../analysis_report/research/25_native_im_e2_adapter_lifecycle_offline_evidence.md)。
- [`26_native_im_provider_bundle_offline_evidence.md`](../../analysis_report/research/26_native_im_provider_bundle_offline_evidence.md)。

这仍没有打开真实 IM：仓库没有 production HTTP/WebSocket/socket exchange、真实 credential
material、webhook 或 external IM send。下一硬门禁是真实 provider contract、测试
endpoint/scope/data/secret/path/expiry/rollback 批准输入、真实 profile/mapper fixture 与 production
exchange，并单独修订 `SERVICE_BOUNDARY.md`。在这些条件完成前仍不能连接真实 endpoint。Golden
vectors 是代表性正向 inventory；全部
event/revision/scope/mention/digest union/state 矩阵由参数化 contract tests 覆盖。Gate A–E 均未改变。

## 1. 可靠性与一致性

### 已提交能力

- `store.py`：WAL、optimistic stream version、idempotent append、atomic multi-event append、
  transactional inbox/outbox、bounded reads，以及全 public/lifecycle/deferred stream process owner
  guards、exact SQL snapshots、transaction context enter/exit、owner-aware
  transaction/migration/constructor cleanup、nested clean mismatch 和完整 inherited graph
  quarantine；另有 `append_invocation_admission` 把 caller-supplied event batch、queued job 与
  checksum-bound immutable receipt 同 transaction 提交，只允许 receipt-proven exact replay，partial
  或无 receipt split binding、tamper 与缺失关联 row 全部 fail closed；运行合同见
  [`ATOMIC_INVOCATION_ADMISSION.md`](./ATOMIC_INVOCATION_ADMISSION.md)；
- `runtime.py`：plan/task/initial-ready 原子初始化；approval request/decision 与 task transition
  原子批次；commit-after-wrapper-error 精确协调；post-commit observer 故障隔离；
- session recovery：每页最多 1,000 events，验证同 stream/连续 sequence，并执行 1,000,000
  event、256 MiB canonical bytes、5,000,000 JSON nodes 累积门禁；边读边重建候选状态，整条
  历史验证完毕后才发布；
- event-backed `ArtifactLedger`：逐行 global replay，100,000 versions、256 MiB content、64 MiB
  metadata、1,000,000 metadata nodes、384 MiB state-data 累积门禁；多 ledger 写入以事务内
  global-position CAS 重建重算，幂等重试严格绑定完整稳定请求；
- `attempts.py`：单机 SQLite durable job/attempt、lease、heartbeat、retry、expiry、epoch fencing
  和 stale-owner terminal CAS；已提取 caller-owned first-claim helper，保留 public claim 的
  recovery/commit-ambiguity 合同，并覆盖 provider PID/fork 与无候选零调用；
- `store.py::claim_invocation_start(...)`：严格复核 canonical admission 后，以单一
  `BEGIN IMMEDIATE` transaction 完成首次 job claim、attempt insert、schema-2 start event 和完整
  readback；仅首次正常 COMMIT ACK 返回携带非重放 lease 的 `InvocationStartClaimed`，replay、第二
  worker、reopen 与 ACK-loss 后恢复只返回不含 lease 的 `InvocationStartObserved`；运行合同见
  [`ATOMIC_INVOCATION_START.md`](./ATOMIC_INVOCATION_START.md)；
- `artifact_store.py`：tenant/workspace-scoped blob/version、digest、并发版本和恢复检查；
- `publisher.py`：bounded callback admission、timeout、retry/dead-letter、lease deadline、ambiguity
  记录和 receiver receipt 校验；
- `native_im.py`、`native_im_gateway.py`、`native_im_fake.py`：冻结 provider-neutral V1 wire/codec、
  exact 四方法 port、纯 admission 和 zero-network fake；fake outbound 默认在请求检查前拒绝，测试
  permit 只能产生内存 effect，ACK-loss/unknown 只能 acceptance query 而不能盲重发；
- `native_im_sandbox.py`、`native_im_sandbox_lifecycle.py`、
  `native_im_sandbox_observability.py`：default-off composition、显式 inbound-only adapter、bounded
  parser、process-bound kill switch/lifecycle、取消可恢复 graceful close、typed body-free log/counters；
- `projections.py`：exact schema、leased offsets、receipts、upcast、tamper checks、handler capability
  撤销，以及 framework table/deferred VIEW/TRIGGER/VTABLE authorizer 边界。

### 未关闭 P0/P1

- runtime 仍直接调用 Agent，尚未接入 atomic admission，也没有 claim/heartbeat/fencing 的 attempt
  integration；
- canonical admission 与 claim + attempt + schema-2 start event + readback 的统一 UoW 已实现；但
  heartbeat-supervised receipt-aware worker gate、result/artifact acceptance 和 terminal state
  仍未实现；
- task `RUNNING`、attempt、artifact/result acceptance 和 terminal task state 不是端到端状态机；
- Agent 返回后逐个写 artifact、result 和 completion，任一边界崩溃仍需 receipt-based reconcile；
- succeeded attempt 没有不可变 result receipt 时不能安全自动投影 `COMPLETED`；
- connector 不支持 receiver idempotency/fencing 时只能诚实承诺 at-least-once；
- 原生 IM 已有 provider profile、签名/timestamp/raw-body verifier、durable nonce、migration 5 inbox
  schema、read preparation、整页单事务 admission、default-off inbound adapter/lifecycle、recorded
  probe、provider bundle TCK 与增强 exchange provenance；但现有 semantic provider candidate 仅用于
  离线 TCK，真实 profile/mapper/production exchange、sandbox contract probe 和任何 outbound
  composition 仍不存在；离线 bundle 不能替代真实 provider 证据；
- 编排 session lock 是进程内锁，多进程/多实例调度仍可能重复调用 Agent；
- 没有完整 crash-at-every-boundary、kill -9、long-running heartbeat 和 graceful drain E2E。
- 共享 process identity helper 已通过真实 fork、nested fork、PID drift、fork-while-unrelated-lock、
  spawn/forkserver、copy/pickle 和 parent-continuity；`SQLiteEventStore` 又独立覆盖全部入口、open
  transaction/clock/migration/iterator fork、transaction context enter/exit、child GC/finalizer、
  exact control、nested clean error graph 和 fresh fork-before-init/spawn/forkserver contention/CAS。
  但 artifact/projection/revocation store 与 recovery coordinator 的已构造实例仍未绑定 owner，
  单组件证据不能替代逐组件接入。

晋级标准：任意 admission、claim、dispatch、receiver accept、result accept、ACK 和响应边界崩溃
后，只能恢复为未发生、已证明成功、明确拒绝或需要人工/自动 reconcile 的 unknown；不得盲目
重复不可逆动作。

## 2. 身份、授权、凭据与日志

### 已提交能力

- `tenancy.py` 提供 tenant/workspace、member、role、service principal、capability、expiry、
  resource/data scope、revision 和 revocation high-water primitive；delegation 只允许缩权；
- approval durable identity、intent、reason、timestamp、actor/correlation/causation/idempotency
  envelope 均经过 canonical validation；
- service config 只读取 allowlisted `QE_*`，拒绝 unknown key、非 loopback production endpoint、
  production debug、真实 connector 与可写配置祖先，并绑定单次 snapshot；
- `SecretRef` 不包含 material；`SecretMaterial` 有界、redacted、不可复制/序列化，关闭时擦除
  owned buffer；file provider 使用 descriptor-relative open、`O_NOFOLLOW`、owner/mode/type/link
  和稳定性检查；
- safe logger 只接受固定 event schema 和 typed allowlisted fields；identifier 仅输出短 SHA-256
  关联值；redactor 对异常、对象、bytes、credential key/text、深度、宽度、整数和总节点 fail
  closed；
- publisher 不再把 connector exception/traceback、raw worker/message ID 或 active lease token 写入
  operational log/repr/to-dict，persisted error 只接受显式 code allowlist。

### 未关闭 P0/P1

- 没有 Authenticator/OIDC adapter；caller-provided principal/scope 不可信；
- capability 尚未在每个真实 tool/connector action-time boundary 强制验证；
- events/delivery/attempt/projection 尚未统一 scope，撤销也未穿透运行中工具调用；
- file secret provider 不是 KMS/Vault/HSM，没有轮换、版本 pin、访问审计或进程隔离；
- typed safe logging 只迁移 publisher；runtime、attempt、artifact、migration 和第三方 exporter 仍
  需逐路径迁移和 canary 扫描；旧数据库/备份可能含历史自由文本 `last_error`；
- Agent、plugin 和 connector 与主进程共享文件/网络/进程权限，无 sandbox、SSRF/DNS/IP
  revalidation、CPU/内存或出网 allowlist；
- `RequestContextIssuer`、in-memory revocation high-water、HMAC key lifecycle 和 secret material
  尚无统一 fork/spawn contract；parent 擦除 secret 不会擦除已 fork child 的独立 buffer；
- stable identifier hash 不是匿名化，仍需 retention/access/cardinality policy。

晋级标准：身份只能来自验证过的认证层；所有受保护动作执行时重新授权；secret canary 在
日志、事件、prompt、artifact、异常、metrics、trace 和 release evidence 中为零。

## 3. 多租户与数据边界

Artifact repository 已把 tenant/workspace 纳入必填 predicate 和唯一键，authorization domain
也具备 anti-rollback primitive；但这不是全仓库隔离。

未关闭 P0：

- events、snapshots、inbox、outbox、invocation jobs/attempts/admissions 和 projections 仍有裸
  session/id 或 global position 查询；
- 没有可信 `RequestContext` 把认证 principal、membership revision 与目标 scope 绑定；
- 没有 tenant-bound/signed external cursor、统一不可枚举 error、cache/metric isolation；
- legacy 数据没有 operator mapping、可重入 backfill、NOT NULL/unique contract migration；
- 跨 tenant property suite 尚未覆盖每个 repository/API/backup/restore path。

晋级标准：用完全相同 ID 运行双 tenant 正反测试，替换任意 scope 后只能拒绝或返回不可枚举
结果；SQL、cursor、日志、错误、指标、cache 和 backup 均不得泄露另一 tenant。

## 4. API 与协议互操作

当前 `protocol.py` 有 canonical envelope、authority、causation、idempotency 和 trace 字段；A2A
和 LangGraph 只有数据/调用桥接，DeepSeek Harness 保持显式 factory port。

尚无：

- authenticated `/api/v1`、OpenAPI、request ID、body/depth/rate limit、ETag/revision、command
  journal/receipt 和统一错误合同；
- resumable bounded SSE/WebSocket event stream、signed cursor 和 disconnect reconcile；
- A2A 官方 SDK/TCK 的 HTTP/SSE/auth/cancel/status reconciliation；
- MCP tool/resource client 的 consent、data classification 和 action-time policy；
- webhook signature/time-window/nonce/replay、版本协商和跨版本 upcaster contract。

本地 mapping 或 demo 不能描述为真实平台互操作证据。

## 5. 服务生命周期与可观测性

严格配置/secret primitive 与原生 IM 的离线 process-bound lifecycle 已存在，但没有读取批准的真实
provider 输入并组装完整服务的 production composition root。原生 IM lifecycle 证明的是注入 fixture
下的 health/admission/kill/close，不是 `/livez`、`/readyz` 或 SIGTERM 服务合同。仓库现有
`examples/product_trial_server.py` 是 loopback-only `ThreadingHTTPServer` 试用
adapter，提供临时共享访问 token、Host/Origin/Fetch Metadata 防护、严格 JSON 和请求体上限；
它不是 authenticated `/api/v1`，也没有可信主体/tenant/workspace、`/livez`、`/readyz`、startup
preflight、admission stop、SIGTERM bounded drain、lease relinquish、structured audit store 或
OpenTelemetry。不得用该示例 listener 反推 Gate B 已完成。

P0/P1 门禁：

- production startup 必须显式 preflight schema/config/secret/backup compatibility，不得靠构造器
  隐式迁移；
- health 必须区分 liveness/readiness/dependency detail，详细信息需要授权且不能泄露拓扑；
- shutdown 必须停止 admission、等待/取消安全工作、释放 lease，并对 unknown effect 告警；
- production composition root 必须证明 worker topology 在 connection/issuer/secret/event-loop 初始化
  前完成；禁止 preload 后继承 live authority、SQLite connection 或 connector；
- 任何 process mismatch worker 必须停止 admission 后使用 `os._exit`/exec；禁止捕获后继续服务或
  依赖普通 `sys.exit`/解释器 teardown 完成 inherited native resource 清理；
- metrics/alerts 至少覆盖 queue age、stuck attempt、lease expiry、DLQ、projection lag、backup
  age、auth denial、secret/config failure、provider latency/cost 和 storage capacity；
- operational log、durable security audit 和业务 event 必须保持不同失败语义。

## 6. Schema、备份、恢复与部署

已实现 packaged/checksum-bound domain migrations、SQLite online backup、source/file identity、
integrity/foreign-key/schema/version/core-row-count manifest、restore precondition 和本地 admin CLI。
Backup manifest 已覆盖真实 `projection_offsets`、`projection_receipts`、
`qe_revocation_high_water` 和 migration-4 `invocation_admissions`；non-empty admission receipt 已有
backup/restore 后 exact replay 测试，v3 backup 也覆盖 restore-forward-upgrade。旧审计中的表名/漏表
结论已经关闭。

仍缺：

- future action/API/audit receipt 表进入 manifest topology 和 restore reconciliation；
- restore 后 non-emitting reconcile mode 与外部 monotonic revocation/security checkpoint；
- encrypted immutable off-host backup、调度、告警、保留/删除/legal hold 和实测 RPO/RTO；
- least-privilege non-root/read-only-rootfs 单节点部署、支持矩阵和 clean-host install；
- stop-the-world upgrade/rollback、schema compatibility 和 restore-forward-fix rehearsal；
- PostgreSQL/HA/Kubernetes 只能在单节点 scope/action/lifecycle Gate 关闭后推进。

## 7. 供应链与发布完整性

当前有四个 exact/hash-locked dependency target（74 package records）、binary-only installation
policy、canonical sdist normalization、双 detached-worktree reproducible wheel/sdist、strict
distribution manifest、source commit/tree binding、runtime/build CycloneDX 1.6 SBOM、官方 schema
验证和 retained local release evidence。主线也已有 versioned dependency-risk policy/result、
exact scanner/database snapshot identity、component/artifact coverage、single-use waiver 和严格离线
evaluator；当前提交策略明确 `promotionEnabled: false`，approved scanner/database/license allowlist
均为空，因此不可能产生 promotion success。

仍缺 production promotion 所需的真实 scanner adapter、经认证和批准的数据库 snapshot 获取链、
法律批准 license allowlist、真实扫描结果与 CI 接线；也缺 malware/maintainer-risk policy、
optional/deployment/platform SBOM coverage、independent immutable runner、verified
interpreter/bootstrap、immutable mirror、signed provenance、artifact signature 和 trusted builder。
合成 evaluator 测试或同一 runner 两次 byte-identical build 都不能替代这些证据。

## 8. 性能、容量与资源隔离

Recovery、payload、artifact、projection、publisher callback、config/secret 和部分列表已经有明确
单项/累计边界；`ArtifactLedger` 的 state-data 是逻辑数据预算，不是 RSS 上限，且 live chain
append、copy-on-write index 与全局冲突重建仍缺容量基准。服务级容量包络仍未冻结。

仍缺 global/tenant/provider quota、API backpressure、bounded admission queue、worker process
resource limit、artifact blob tiering、snapshot/compaction、数据库增长/retention 和 benchmark/soak。
SLO/RPO/RTO 只能引用真实 workload 下的 p50/p95/p99、CPU、RSS、disk、queue age、recovery time
和失败模式，不能引用设计目标。

## 9. 测试与证据

主线已有广泛 deterministic unit、tamper、recovery、migration、publisher、projection、tenancy、
backup、distribution、SBOM、config/secret、approval atomicity、atomic invocation admission/start
和 fault-wrapper tests。start 专项覆盖 admission/job/event/attempt 伪造、事务 body 与
BEGIN/COMMIT/ROLLBACK fault、ACK-loss poison/reopen、control signal、双 connection、双 spawn、
provider fork、backup/restore 与 lease-token canary；admission 专项另覆盖 partial/full split、
v3→v4 migration 与 non-empty receipt backup/restore。当前本地
门禁同时执行 dependency-lock verifier、完整 pytest、locked Ruff lint/format、strict mypy、
compileall、deterministic demo 和 diff check。

E1 源码候选 `7620200` 已保留的组合证据包括：本机 Python 3.13 完整 1,775 tests；Python
3.12.12 完整 1,775 tests；Python 3.9.6 完整 suite 通过并有一个既有 platform-capability skip；
locked Ruff format/check 覆盖 152 files，strict mypy 覆盖 49 source files。Native IM 专项收集并
通过 271 tests，golden verifier 23/23，Python 3.9/3.12 zero-network 均通过；canonical local
release evidence 5/5 通过且 commit/tree 在门禁前后稳定、checkout 始终 clean。该本地
`releasable=true` 只证明固定 local predicate，不是生产 promotion。这些结果不把范围外 OS、
SQLite、服务级 crash/soak、真实 IM 或生产 Gate 推定为已通过。

E2 原子页运行节点 `9cf1bfe` 又在本机 Python 3.13 完成 88/88 auth + nonce + prepared-read +
page-admission 专项和 2,060/2,060 全仓测试；dependency locks（4 targets / 74 records）、Native IM
golden（23/23）、zero-network、Ruff（169 files）、strict mypy（55 source files）、compileall、
deterministic demo 与 diff check 全绿。矩阵覆盖 durable graph 篡改、空页/多页、同页/不同页双连接、
输家 nonce rollback、COMMIT/rollback ACK-loss、poison/reopen、blocked waiter、exact-type、caller
mutation snapshot 和零 gateway/Agent/plugin/browser/network/outbound。该结果仍不包含真实网络、
provider sandbox、服务级 crash/soak 或生产批准。

E2 adapter/lifecycle 运行节点 `2bdaea1` 又完成 2,114/2,114 全仓测试（77 个既有 Python 3.13
fork deprecation warnings）、616 项 Native IM + safe-logging 收集、66 项本节点显式文件收集、
Ruff check/format（177 files）、strict mypy（58 source files）、golden 23/23、zero-network、
dependency locks（4 targets / 74 records）和 compileall。矩阵覆盖 default-off、outbound fence、
raw/mapped byte-domain 分离、atomic kill race、disconnect/resume、duplicate/out-of-order/conflict、
process inheritance、cancellation/graceful close、hostile transport/mapper/secret、logger failure 和
message/trace/secret/nonce/signature canary。该结果仍不包含真实 provider network、service-level
SIGTERM/soak、Agent activation、outbound 或生产批准。

E2 provider bundle 离线运行节点 `ee0666f` 又完成 2,386/2,386 全仓测试、Ruff lint/format
（207 files）、strict mypy（65 source files）和三套 fresh-process/hash-seed TCK verifier。Mapper
TCK 为 3 accepted / 6 rejected，digest `e569232b…f5d51`；Transport TCK 为 5 accepted /
12 rejected，digest `173a05e4…06c1d4`；Bundle digest 为 `7fbdec73…50d4e7`。矩阵覆盖 signed
wire → HMAC → pure mapper → canonical page → enhanced provenance → atomic SQLite admission 与回读，
并证明重复 event 的 stable source evidence 不随 exchange 漂移。该结果仍不包含真实 endpoint、
production exchange、service-level soak、Agent activation、outbound 或生产批准。

仍缺：

- 完整的 supported OS/SQLite 组合矩阵；当前不可变 CI 只覆盖 GitHub Linux 的 Python 3.9/3.12，
  本机 Python 3.13 证据不能替代所有目标平台 clean runner；
- API/fake-connector E2E、cross-tenant property、fuzz、crash-at-every-boundary、load/chaos/soak；
- artifact/projection/revocation/authorization/secret/runtime/connector 的 POSIX fork/fork-while-lock、
  spawn/forkserver、parent continuity、fresh child composition 和 spawn-before-secret-load retained
  matrix；`SQLiteEventStore` 自身的对应矩阵已存在，但不是系统级证明；
- coverage/mutation policy、branch protection、review ownership 和正式 promotion decision；
- 每次代码变化后重新生成的 exact source-bound packages/SBOM/release evidence。

## 10. 产品与人机协同界面

调研报告已经定义“群聊即协作界面、任务图、Artifact、Needs You、takeover/reconcile 和审计
timeline”的目标。仓库现有本地产品试用页已经展示自定义指令、三 Agent DAG、真实模型
narration、Artifact 预览/下载、Needs You、事件时间线和架构图，并完成一次本机 Playwright
验收；但它仍是 source-checkout 下的单用户 loopback 示例，不是正式 Web/desktop 产品或服务端
composition。现有截图和一次浏览器成功不能替代可信认证、权限一致性、持久重连、可访问性、
本地化、故障/断流矩阵和打包后浏览器 E2E。UI 不得用隐藏按钮代替服务端授权。

## Gate 状态

| Gate | 目标 | 状态 | 当前主要阻断 |
|---|---|---|---|
| A | 离线 tenant-scoped 内核 | 关闭 | trusted context、全 repository scope、legacy contract、全路径 redaction |
| B | authenticated loopback API + fenced fake connector | 关闭 | API/command receipt、attempt/result/action state、stream/lifecycle |
| C | 受控私有试点候选 | 关闭 | deployment、upgrade/rollback、完整 restore 与 measured recovery |
| D | 有限商用候选 | 关闭 | quota/OTel/alerts/isolation/security/capacity/soak |
| E | 多实例 GA 候选 | 关闭 | PostgreSQL/HA/Kubernetes/continuous DR |

## 下一实现顺序

按用户决定的提前接入顺序推进：

1. E1 已闭环；E2 durable nonce、verified page、event/verification/link rows、read CAS、checkpoint
   与独立 readback 的单一事务及 rollback/ACK-loss/reopen 对账已完成；
2. default-off inbound-only adapter、bounded parser、kill switch/lifecycle、safe logging/canary 和
   recorded contract probe，以及 provider Mapper/Transport/Bundle TCK 和 exchange provenance 已完成；
   仍未进入真实网络；
3. 下一步冻结真实 provider contract、sandbox endpoint class、测试 tenant/conversation、数据等级、
   read-only credential reference、方法路径、截止时间和 kill switch；用真实 fixture 实现
   profile/mapper 和 production exchange 并通过现有 TCK，再单独修订 `SERVICE_BOUNDARY.md`；
4. 上述批准与 full gates 完成后，才执行 health/read/dedupe/resume；Level B 只生成 observation，
   不驱动 Agent、tool、browser、subprocess 或 outbound；
5. E3 在已完成 event/job/receipt atomic admission 与 claim + attempt + schema-2 start event + readback
   统一 UoW 之上，已冻结默认关闭的
   [`heartbeat worker 合同`](./HEARTBEAT_SUPERVISED_PURE_WORKER.md)；下一步先把 accepted
   result/artifact、attempt 和 terminal task state 组成单一原子验收边界，没有 result receipt 时
   绝不把 succeeded job 猜成 completed；
6. 完成 receipt-bound crash/kill recovery 后，才启用只接受 exact first-claim authority 的
   heartbeat-supervised pure/fake worker；
7. E4 建立 durable action receipt 与 `effect_unknown` reconcile，connector 继续只用 fake；真实
   outbound 在 E1–E4 完成且针对单一 sandbox 另获明确授权前保持不存在/关闭；
8. 建立可信 RequestContext，然后 expand/backfill/contract，逐 repository 强制 tenant/workspace；
9. 迁移剩余自由文本 log/error，并建立全输出 secret-canary gate；
10. 实现 authenticated loopback API、transactional command receipt、stream、health 和 SIGTERM；
11. 完成单节点部署、upgrade/rollback、restore/non-emitting reconcile 和 clean-host evidence；
12. 通过 Gate C 后再推进 capacity/OTel/isolation/PostgreSQL/HA/DR。

每个独立行为及其测试单独提交；每阶段都有运行命令、失败边界、兼容/迁移、回退和证据文档。
默认分支每次提交后保持可运行，但“可运行”不等于“可生产晋级”。
