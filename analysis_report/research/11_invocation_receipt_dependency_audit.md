# Invocation start / result receipt 依赖与原子边界审计

审计日期：2026-08-20（Asia/Shanghai）

源码检查点：`1427deafc17ad71501993ef375e9897fa29a5f12`

状态：**只读设计审计；没有启用 worker、receipt、native/sparse migration 或生产 Gate**

## 1. 结论

当前实现不能从 bridge-only migration applier 直接跳到 receipt 表、可信恢复或 runtime
接线。安全依赖顺序必须是：

```text
bridge 接入与 mutation-before-preflight
  -> sparse-capable 只读 SchemaState
  -> exact backup manifest v2 writer / verifier / restore
  -> fresh v2 backup 与 mixed-binary/fleet 演练
  -> native/domain-sparse planner + executor（默认关闭）
  -> invocation result migration
  -> versioned invocation-start / result schemas
  -> single-connection admission / claim / result UoW
  -> store-owned trusted recovery bundle
  -> fenced fake/pure worker
  -> kill-at-every-boundary retained evidence
```

可以提前开发纯模型、decoder、canonical manifest、disabled executor 和测试，但以下动作在
前置门禁完成前必须不可达：

- 写入任何 native/sparse ledger row；
- 安装 invocation receipt 表；
- 把 caller 构造的 receipt 当成可信证据；
- 让 runtime 自动 claim、重试、接受结果或投影完成；
- 让旧 binary 打开 sparse/native database 后再“检测并退出”。

bridge-only 发布与首次 native/sparse 写入必须是两个独立、可回滚、带 soak 证据的阶段。

## 2. 权威现状证据

| 边界 | 当前事实 | 直接后果 |
|---|---|---|
| Legacy migration runner | `migrations/__init__.py` 只接受全局连续前缀 | 不能安全表达 domain-sparse ledger |
| Attempt-only store | 当前仍请求 `(1, 2)` | 被迫安装不属于 attempts 的 artifact v1 |
| Delivery v3 | 依赖 `SQLiteEventStore` 在 migration 外创建的 `outbox` | 不能把全局 ID 顺序误当语义依赖 |
| Bridge applier | 只允许 sidecar install、legacy bootstrap 和 no-op | 明确禁止 native/v4/sparse 写入 |
| Backup | format 固定为 `qe.sqlite-backup/1`，flat prefix + fixed tables | 不证明 sidecar、domain heads、native receipt 语义 |
| Start event | 只有 task/agent/envelope/context digest | 不是完整 invocation/attempt binding |
| Result event | 只有 task/narration/artifact refs/metadata | 不是 completion-capable receipt |
| Recovery receipt | 普通 frozen dataclass，可由 caller 构造 | 即使字段匹配也只能 `BLOCKED_RECEIPT_UNVERIFIED` |
| Session recovery | invocation start/result events 被跳过 | legacy `RUNNING` 只能 quarantine，不能自动恢复执行 |

关键源码：

- `src/quantum_entanglement/migrations/__init__.py`
- `src/quantum_entanglement/domain_migrations.py`
- `src/quantum_entanglement/backup.py`
- `src/quantum_entanglement/attempts.py`
- `src/quantum_entanglement/runtime.py`
- `src/quantum_entanglement/invocation_recovery.py`
- `docs/architecture/DOMAIN_SCOPED_MIGRATIONS.md`
- `docs/production/INVOCATION_RECOVERY_COORDINATION.md`

## 3. Versioned invocation-start evidence

当前 `task.invocation.started` payload 只有：

- `taskId`；
- `agentId`；
- 完整 envelope；
- `contextDigest`。

嵌套 envelope 的字符串 schema version 不能替代事件 payload 的数值型
`_schemaVersion`。当前 `invocation-started:{taskId}` idempotency key 也无法区分同一 logical
invocation 的不同 attempt。

权威 v2 start evidence 至少需要：

| 类别 | 必需字段 |
|---|---|
| Schema | `_schemaVersion=2`、`evidenceKind=attempt_bound`、invocation payload schema version |
| Scope | tenant、workspace、session、plan |
| Invocation | invocation ID、task ID、agent ID、idempotency key、payload digest |
| Attempt/fence | attempt ID/number、lease epoch、lease-token digest、worker ID |
| Time | claimed-at、lease-expires-at |
| Inputs | context digest、authorization decision/state stable digest |

`payloadDigest` 必须使用 domain separator，并覆盖 schema version、tenant/workspace、完整
task、完整 envelope、已提交 context digest 和所有执行相关固定配置。恢复时不能用当前
plugin、当前 context、当前 authorization state 或当前 Agent 配置重新生成。

事件 idempotency key 必须至少绑定 `invocationId + attemptNumber`。Raw lease token、credential
和授权 material 不得进入事件；只能持久化 canonical digest 或受控引用。

### Legacy upcast

旧 v1 start event 只能被标记为：

```text
evidenceKind = legacy_unbound
```

upcaster 绝不能合成 invocation ID、attempt ID、receipt ID、payload digest 或 scope。旧
`RUNNING` task 继续 quarantine，直到人工/专用 repair workflow 提供真实 durable evidence。

## 4. Completion-capable result receipt

当前 `task.result.received` 缺少 receipt identity、完整 invocation/attempt binding、manifest
和可信事件位置。建议 v2 event 至少包含：

| 类别 | 必需字段 |
|---|---|
| Schema | `_schemaVersion=2`、`evidenceKind=attempt_bound`、receipt schema version |
| Identity | receipt ID、完整 tenant/workspace/invocation binding |
| Fence | attempt ID/number、lease epoch、lease-token digest |
| Result | result ref、manifest schema version、manifest digest |
| Effect | effect class、action-receipt-set digest |
| Commit | accepted-at、可信 event ID/global position/stream sequence 关系 |

Canonical result manifest 应由 trusted store 保存，而不是只存在于 event JSON。它至少覆盖：

- bounded narration 或其明确摘要；
- ordered scoped artifact identity、version、digest、byte size、media type；
- bounded canonical metadata；
- action receipt identities，或明确的 `pure/no_external_effect` 分类；
- invocation/attempt binding digest。

Agent result receipt 与 external receiver/action receipt 是不同证明。若远端 effect 可能已经发生、
但 receiver receipt 缺失，Agent result receipt 不能授权重试，也不能被描述为 exactly once。

## 5. Trusted receipt store topology

Receipt 表不能被偷偷塞入 attempt migration 或 EventStore startup DDL。建议独立 domain，例如
`invocation_results@1`，显式声明 attempts、artifacts 和 EventStore base schema precondition。

建议最小 topology：

```text
invocation_result_manifests
  tenant_id + workspace_id + manifest_digest
  manifest_schema_version + canonical_manifest_bytes + byte_size

invocation_result_receipts
  receipt_id + receipt_schema_version
  complete scope / invocation / attempt / fence fields
  result_ref + manifest_digest
  result_event_id + global_position + stream_id + stream_sequence
  accepted_at

invocation_result_artifacts
  receipt_id + ordinal
  scoped artifact identity + version + digest
```

硬约束：

- one logical invocation 最多一个 accepted result；
- one attempt 最多一个 receipt；
- 同一 idempotency retry 必须逐字段和 canonical manifest bytes 完全一致；
- manifest 不得跨 tenant 全局去重；
- decoder 拒绝额外字段、错误 SQLite storage class、控制字符、future schema、非 canonical
  timestamp 和 oversized data；
- trusted recovery 同一 read snapshot 内读取 job、attempt、receipt、event 与 artifact 关系；
- API 不能通过 `trusted=True`、caller bool 或 caller-provided object 升格证据。

`InvocationRecoveryCoordinator` 的目标输入应是 store-owned、不可由普通 caller 构造的 durable
recovery bundle。

## 6. 三个必须原子的写边界

### 6.1 Admission

同一事务写入：

```text
task READY -> RUNNING event
+ invocation job enqueue
```

否则会出现 `RUNNING/no job` 或 `queued job/non-running task` split brain。

### 6.2 Claim/start

同一事务写入：

```text
job claim CAS
+ invocation_attempts insert
+ task.invocation.started v2
```

start evidence 不能早于 durable claim，也不能由 worker 在 claim 返回后用另一个 connection
补写。

### 6.3 Result acceptance

单 SQLite topology 下，同一 connection/transaction 必须完成：

```text
refresh RequestContext + action-time authorization
+ task stream expected revision check
+ active lease / attempt / epoch / token digest / deadline check
+ artifact blobs and versions
+ canonical result manifest
+ durable result receipt
+ task.result.received v2
+ attempt + job success CAS
+ task.status.changed -> COMPLETED
+ resulting outbox rows
```

大 blob 可以先上传 immutable content-addressed bytes，再由事务发布 metadata/reference；未引用
预上传 blob 可回收，但 receipt 绝不能引用缺失对象。

若未来 PostgreSQL/外部存储无法把 completion 放进同一事务，必须设计显式的
receipt-bound idempotent projector。不能用“最终会补上”代替 durable state machine。

## 7. Migration ID 与发布边界

文档中的 ID 4 / `artifacts@2` 目前只是 proposal，没有进入 packaged registry 或 durable
ledger，因此还不是不可重写历史。必须先用独立 ADR 决定：

1. 下一项真实 native migration 使用 ID 4，例如 `invocation_results@1`，artifact proposal
   后移；或
2. 显式保留 artifact ID 4，receipt 使用后续全局 ID，并证明 sparse planner 不会安装无关
   domain。

禁止创建空 SQL、fake ledger row 或“占号 metadata”。Global migration ID 是已发布身份，不是
roadmap 序号。

## 8. Backup manifest v2 是 native enable 前置条件

v2 不能只是把新表名加入 `_CORE_TABLES`，也不能给 format v1 添加 optional 字段。它必须使用
exact format `qe.sqlite-backup/2`，并保存、验证：

- registry digest、SchemaState digest、shape；
- domain heads；
- applied migration ID/descriptor/SQL/owned-schema digest；
- dependency edges；
- 从 validated owned-object registry 派生的 table topology，包括 sidecar；
- invocation receipt/result-event/artifact/attempt reconciliation watermark 或 canonical set
  digest。

Restore 必须先在 quarantine 状态验证 exact bytes、SQLite integrity、foreign keys、SchemaState
和 receipt relations，不得边验证边 migration。

Compatibility：

- v2 reader 可以严格读取受支持的 legacy v1 backup；
- v1 reader 永远不能接受 v2 manifest 或 sparse database；
- sparse enable 前，必须保留 fresh v2 backup 并完成 restore/rollback rehearsal。

## 9. Mixed-version epochs

| Epoch | 允许行为 | 禁止行为 |
|---|---|---|
| Legacy | global prefix 1-3，sidecar absent/bridged，native off | sparse/native write |
| Expand-read | fleet 可读 v1/v2 evidence，但仍不写 v2 | 把 reader rollout 当 migration approval |
| Bridge-ready | app/worker/admin/backup/restore 全 bridge-aware，fresh v2 rehearsal | 旧 backup job 或 mutation-before-preflight |
| Native schema | 显式安装 receipt migration | 旧 binary 接触 database |
| Writer enable | 仅 fenced fake/pure invocation 写 v2 start/receipt | real external connector 或 effect-unknown retry |

旧 binary 的阻断必须发生在任何 `CREATE IF NOT EXISTS`、migration side effect 或业务写之前。
当前 EventStore 先执行 base DDL 再验证 migration，因此不能把现状描述成安全 preflight。

Rollback：

- bridge-only 且没有 native row 时，只能回到已实测忽略 sidecar 的 binary；
- native schema 已写后，只有 empty tables、无 v2 events、无 active lease 且有原子 down guard
  的情况才可能考虑 down path；
- 一旦写入 v2 start/result/receipt，不得回滚到不理解它们的 binary；
- 不得编辑 ledger/checksum、删除 sidecar evidence 或把 sparse database 伪装成旧 prefix；
- 生产故障优先 forward-fix；恢复旧 backup 必须同时 reconciliation backup 后的本地写入和
  已发生的外部 effect。

## 10. P0/P1 threat matrix

| Severity | Failure | Required control |
|---|---|---|
| P0 | legacy event 补字段伪装 trusted evidence | `legacy_unbound`，永不合成 identity |
| P0 | caller receipt 授权 completion | store-owned atomic bundle |
| P0 | stale/expired worker 接受结果 | 同事务重验 attempt/epoch/token/deadline/auth |
| P0 | artifact/event/receipt/attempt/completion 部分提交 | single-connection UoW + kill injection |
| P0 | `complete()` 先于 receipt | API 结构上不可达 |
| P0 | conflicting duplicate receipt | scoped uniqueness + exact canonical retry |
| P0 | tenant/session/task confused deputy | scope 进入 binding、PK/FK、digest、query |
| P0 | result receipt 替代 external action receipt | effect classification；unknown 保持 blocked |
| P0 | old binary 写 sparse database | fleet floor + mutation-before-preflight + routing |
| P0 | backup v1 遗漏 sidecar/receipt semantics | v2 backup/restore 先部署并演练 |
| P0 | native migration partial commit | SQL/ledger/metadata/deps/postcondition 同事务 |
| P0 | COMMIT acknowledgement 丢失 | exact durable readback；否则 quarantine |
| P0 | forged event position link | event ID/position/stream sequence/payload 全量复核 |
| P0 | canonical JSON drift/oversize/cycle | one bounded encoder + domain-separated digest |
| P0 | 多进程同时 accept result | lease CAS + stream revision + receipt uniqueness |
| P0 | restore 后 orphan receipt/job/artifact | activation 前完整关系 reconciliation |
| P1 | 大 artifact 占用 writer lock | pre-validate/stage blob；事务只发布 immutable ref |
| P1 | receipt/history 无界增长 | bounded pagination、quota、retention/legal hold |
| P1 | manifest 可整体重造 | 后续 MAC/signature + authenticated custody |
| P1 | cancellation 被当成无 effect | 独立 authorized cancellation receipt |
| P1 | clock jump | durable/high-water time gate + alert |
| P1 | receipt metadata 泄密 | bounded/redacted manifest；日志只输出 ID/digest |
| P1 | GC 删除 receipt 引用 artifact | FK/reachability/retention tests |

## 11. 建议的独立提交序列

所有代码在最后 promotion commit 前默认关闭。

| # | Commit scope | Minimum proof |
|---:|---|---|
| 1 | ADR：migration ID、receipt domain、event v1/v2 | doc links + diff check |
| 2 | 冻结 legacy descriptors/schema/backup v1 golden | prefixes 0/1/2/3 round-trip |
| 3 | native descriptor/pre/postcondition model | tamper/ownership/bounds/digest |
| 4 | read-only `domain_sparse` SchemaState | valid/hole/future/drift；zero writes |
| 5 | native dependency planner，policy default deny | closure/order/stale state |
| 6 | exact backup v2 codec + strict v1 reader | future/extra/missing/type/size |
| 7 | backup v2 create/verify + registry topology | state/domain/schema tamper |
| 8 | backup v2 quarantine restore | no migration + inode/path races |
| 9 | admin bridge preflight/readiness/fleet floor | no implicit startup writes |
| 10 | disabled native executor | statement/commit/BaseException/concurrency matrix |
| 11 | real two-wheel/two-process bridge/v2 matrix | old/bridge prefixes + old-writer reject |
| 12 | exact start/result v2 models + legacy decoder | shape/bounds/future/legacy-unbound |
| 13 | canonical invocation/result manifest | every-field/order/Unicode/cycle/mutation |
| 14 | package `invocation_results@1` SQL/descriptor/down guard | clean/full/sparse/wheel package data |
| 15 | store startup uses domain target/preflight | attempt-only does not touch outbox |
| 16 | internal single-connection transaction capability | nested/foreign connection reject |
| 17 | atomic `RUNNING + enqueue` | fault matrix：none-or-both |
| 18 | atomic `claim + start-v2` | two-process claim + attempt-2 key |
| 19 | trusted receipt read store/bundle | caller receipt never trusted |
| 20 | atomic result acceptance UoW，worker off | every statement/commit fault |
| 21 | receipt-bound recovery/projector | never reinvoke state matrix |
| 22 | fenced fake/pure worker | lease loss/timeout/cancel/drain/late result |
| 23 | process-kill matrix + v2 restore rehearsal | every kill point + two-process race |
| 24 | migration/rollback/threat/operations docs | commands + decision tree |
| 25 | source-bound retained release evidence | wheels/state/v2 manifest/restore digest |
| 26 | isolated promotion commit | installed-wheel E2E + rollback smoke |

## 12. 明确禁止的捷径

- 直接追加全局 migration，并让 attempt store 安装不相关 v3/outbox；
- runtime `CREATE TABLE IF NOT EXISTS` 冒充 migration；
- 只给 `_CORE_TABLES` 加 receipt 表就宣称 backup v2；
- 给 manifest v1 添加 optional domain 字段；
- 把 job `SUCCEEDED`、`result_ref`、普通 result event、projection receipt 或 Python
  dataclass 当 completion proof；
- 由 legacy event、当前 context 或当前 Agent 配置补造 binding；
- 多个 SQLite connections 顺序写入后宣称 atomic；
- receipt 之前调用 attempt `complete()`；
- lease expiry 后自动重试 effect-unknown Agent；
- 把 raw lease token、credential、authorization proof 写进 event/receipt/log；
- 在同一发布同时打开第一次 bridge/backup 与第一次 native gate；
- sparse 写入后继续允许旧 binary 触碰 database；
- 手改 ledger/sidecar/down SQL 掩盖 incompatible history；
- 把 Agent result receipt 冒充 receiver action receipt；
- tenant/event/attempt scope 未闭合时宣称 multi-tenant production-ready。

## 13. 当前发布判断

本审计没有关闭任何 production Gate。它关闭的是“下一步依赖顺序不清楚”这一设计风险。
在 backup v2、native/sparse default-deny executor、versioned evidence、atomic UoW、trusted
receipt recovery、fake-worker kill matrix 和 retained release evidence全部完成前：

- invocation execution 必须保持未启用；
- legacy `RUNNING` 必须保持 quarantine；
- Gate A-E 必须继续关闭；
- 产品不得宣称 exactly once、multi-tenant production 或可安全执行真实不可逆 connector。
