# 下一阶段详细执行计划：参考项目复评后闭合 Atomic Result Authority

> 计划版本：2026-08-29-stage-pause-v5
> 起点：`main` 上的 Result ReceiptV2 + ObservedV2 安全检查点
> 当前执行分支：`mainline_continue_quantum_entanglement`
> 当前状态：**E1 / Level A 与 E2 provider bundle 离线闭环已完成；E3 Result Authority 的 M1 private stored-event envelope codec（`d889751`）、M2 reserved fence（`dd0ba54`）、M3 private store adapter（`504824c`）、M4 inactive schema / Artifact owner transaction / private backup topology（`28b3d6a`）、M5 atomic result graph + `ObservedV2` + migration-7 opt-in（`144f449`）与 receipt-bound non-emitting reconciliation（`ee63f55`）已完成；随后补齐 migration-7 active result backup/restore、manifest/topology/bytes/geometry 绑定、有界输入防护、干净进程/双连接/SIGKILL 恢复证据、私有 PURE heartbeat supervisor、opt-in store-owned acceptance API 与 fresh-ACK `AcceptedV2`（当前 HEAD `7bed2b6`）。真实 provider sandbox 未连接，生产 worker、terminal business projection、crash/kill/two-process recovery 与 compatibility/rollback evidence 仍未完成。**
> 生产状态：Gate A–E 全部关闭；本计划不能被解释为发布批准。

> 原生 IM 调度说明（2026-08-27）：本文件定义 Atomic Result Authority 的最大强度实现计划，
> 不是“完成全部内容后才能开始 IM”的串行清单。原生 IM 的接入前硬要求、可延期加固和接入后
> TODO 以 [`NATIVE_IM_INTEGRATION_PREREQUISITES.md`](./NATIVE_IM_INTEGRATION_PREREQUISITES.md)
> 为准。2026-08-27 用户决定提前做 sandbox inbound-only 后，当前执行入口改为
> [`NATIVE_IM_EARLY_INTEGRATION_PLAN.md`](./NATIVE_IM_EARLY_INTEGRATION_PLAN.md)；本文件的必要
> 子集在其 E3 阶段使用。三份文档都不授权真实外部发送。

> E2 进展说明（2026-08-28）：provider-neutral V1 contract、strict codec、golden、四方法 port 和
> zero-network fake 已在 `7620200` 完成；离线 profile/config/verifier/migration/nonce/read-preparation
> 与整页单事务 admission 已在运行源码 `9cf1bfe` 完成；default-off inbound-only adapter/lifecycle、
> bounded parser、process-bound kill switch、typed observability、全链 canary 与 recorded probe 又在
> `2bdaea1` 完成；Mapper/Transport/Bundle TCK、zero-network exchange、read-exchange evidence、
> 增强 provenance 和 migration-v6 durable readback 又在 `ee0666f` 完成。Level B 下一硬门禁只剩
> 真实 provider contract、测试 scope/批准输入、production exchange/profile/mapper 和单独修订
> `SERVICE_BOUNDARY.md`；真实 sandbox 网络仍未连接。

## 1. 下一阶段的唯一目标

把当前 capability-free 的 result contract 连接到一个真正可验证的 SQLite durable graph，
并机械区分三种结果：

1. 本调用新写完整结果图且正常收到 COMMIT ACK：未来才允许返回一次性的 `AcceptedV2`；
2. 结果图早已存在、重放、重开、恢复或由其他进程写入：只返回 `ObservedV2`；当前已补齐
   对仍为 `RUNNING` 的 owner 做 receipt-bound、non-emitting reconciliation CAS；
3. COMMIT outcome 不明确、数据部分存在或任一绑定漂移：隔离当前 store，失败关闭，不返回成功。

该目标闭合前，不启用 worker dispatch、outbound connector、result migration 7 或任何“已经 exactly
once”的产品声明。

## 2. 已冻结、不得回退的架构决策

### 2.1 状态和权限分离

- Receipt/Observed 是可序列化、capability-free 的数据与观察；
- Accepted 是某次 store 调用在当前进程内收到 fresh COMMIT ACK 的非序列化结果分类；
- codec-valid 不等于 durable；
- `isinstance` 不得成为后续授权判断；
- replay/reopen/peer process/recovery 永远不能重建 Accepted；
- ACK 丢失不能通过读回后“补发” Accepted。

### 2.2 平台持有业务事实

任务状态、attempt、lease/fence、Artifact 版本、result receipt、event coordinates 与 outbox 必须
由平台同一事务持有。LLM、LangGraph checkpoint、IM 消息和 Agent 自报完成都不是结果真相源。

### 2.3 框架分层

| 层 | 选择 | 不允许越界 |
| --- | --- | --- |
| Domain kernel | 本项目 exact state machine + event store | 不依赖上层 Agent 框架类型 |
| Workflow | 内置 deterministic DAG / LangGraph adapter | checkpoint 不替代 durable business fact |
| Agent loop | DeepSeek Harness/Cordis 思想，经 `AgentRuntimePort` | 插件/上下文/effect 生命周期不拥有组织任务图 |
| 快速装配 | Deep Agents 仅作参考/生态适配 | 不成为平台 scheduler 或权限真相源 |
| 外部 Agent | A2A adapter | 不承载内部组织授权和 Artifact CAS |
| 工具/数据 | MCP adapter | 不把 connector credential 写入 Envelope |
| 内部协调 | Coordination Envelope + event state machine | 不再发明另一套公网协议 |

DeepSeek Harness 的核心价值应落在底层 runtime port：turn/step、session event、插件 lifecycle、
context/tool/effect pipeline、compaction 与 sandbox/approval。LangGraph 负责长期流程和 interrupt，
两者都不能绕过本阶段要完成的 durable result authority。

## 3. 开工前的参考项目复评关口

用户可能加入更多参考项目。因此下一行代码开始前，先完成一次固定输入的 reference delta
review，避免在 store writer 写到一半时改变底层方向。

### 3.1 每个新增项目的接入步骤

1. 克隆到本地 `references/<repo>`；不复制其源码进本仓库；
2. 固定 upstream URL、commit/tag、采集时间和许可证；
3. 记录依赖树、语言/runtime、活跃度、维护者声明与安全公告；
4. 运行项目自己的最小测试/示例，但不输入本项目凭据或真实业务数据；
5. 按源码而不是宣传页标注真实能力；
6. 把可复用点映射到本项目的明确层和 port；
7. 检查 license/NOTICE/专利/商标/网络服务条款，禁止来源不明的代码搬运；
8. 形成“采用、适配、只借鉴、拒绝”四选一结论；
9. 单独提交研究证据，不和实现提交混在一起；
10. 用户确认综合评估后再启动 Phase 1。

### 3.2 统一评分矩阵

| 维度 | 权重 | 核验问题 |
| --- | ---: | --- |
| Durable state/恢复 | 20 | 是否有真实事务、幂等、replay、ACK-loss 与故障注入 |
| 多 Agent 协作语义 | 15 | Agent 是否独立身份，handoff/artifact/责任边界是否显式 |
| Runtime 底层能力 | 15 | context/tool/effect/plugin 生命周期是否可组合、可取消、可隔离 |
| 人机治理 | 10 | approval、Needs You、作用域权限和 action-time check 是否真实 |
| 协议互操作 | 10 | A2A/MCP/标准版本、TCK/SDK 与 extension 保留策略 |
| 安全边界 | 10 | tenant、credential、SSRF、sandbox、日志/DLP、供应链 |
| 产品可见价值 | 10 | 是否能稳定产出可验收 Artifact，而非只展示聊天 |
| 工程可采用性 | 5 | 许可证、测试、发布、维护、升级与回滚成本 |
| 与当前内核重叠 | 5 | 是补缺口、提供替代，还是只重复已有能力 |

评分不能直接决定采用。任何涉及权限、事务或外部副作用的候选，即使得分高，也必须通过本项目
的 exact contract 与故障测试。

### 3.3 复评输出

新增一个独立研究文件，至少包含：

- 固定版本与证据清单；
- 组件/层级映射图；
- 与 v0版、DeepSeek Harness、LangGraph、Deep Agents、Clawith 的差异表；
- 可复用设计和不可采用部分；
- 对本计划 Phase 1–8 的影响；
- ADR 变更建议；
- 最终 go/no-go。

若新增参考不改变 result authority 底层不变量，本计划继续；若改变 event envelope、事务边界或
authority 语义，必须先修订 ADR 和本文件，再写代码。

## 4. Phase 0：重建可复现起点

### 目标

确认阶段验收后的 `main`、参考输入和工具链完全一致。

### 提交与动作

1. `docs: record reference delta assessment`
2. `docs: decide result authority architecture after reference review`
3. 如依赖无变化，不修改 lock；如必须新增依赖，单独提交 lock/SBOM/risk evidence。

### 门禁

- `git status` clean；
- 只有正式 main worktree，或所有新增临时 worktree 均登记在统一目录；
- Python 3.9–3.13 contract 不变；
- 全量 pytest、ruff、mypy 通过；
- 新项目许可证与固定 commit 有证据；
- ADR_0005 没有与复评结论冲突。

### 停止条件

参考项目对 durable result authority 的影响仍不清楚时停止，不进入 codec 实现。

## 5. Phase 1：Canonical Stored-Event Envelope Codec

### 5.1 目标

新增私有、domain-separated、capability-free 的 stored-event envelope codec。M1 冻结 exact scalar
value primitive 与 SQLite 原始 row 独立重算；M3 再把 value primitive 机械绑定到 store-owned
`_EventWriteSnapshot`，并在 owning transaction 内证明两条路径得到相同 digest。

建议模块：

```text
src/quantum_entanglement/_stored_event_envelope_codec.py
tests/test_stored_event_envelope_codec.py
```

### 5.2 固定 envelope 字段

```text
schemaVersion
eventId
streamId
eventType
actorId
timestamp
correlationId
causationId
idempotencyKey
payload
sequence
globalPosition
```

建议 domain：

```text
quantum-entanglement.stored-event-envelope/1\n
```

digest：

```text
SHA-256(domain || canonical-json-body)
```

### 5.3 必须实现的不变量

- payload 根必须是 exact JSON object；
- M1 value primitive 只接受 exact scalar 与已冻结的 canonical payload text，不成为 public API；
- M3 写路径 adapter 只接受 `_EventWriteSnapshot` 已冻结的 canonical payload bytes；
- 读路径读取 `sqlite3.Row.payload_json` 原始 storage class 和 bytes/string；
- 严格拒绝 duplicate key、NaN/Infinity、数组根、非 canonical 排序/空白/escape；
- M1 generic JSON 重编码必须与输入 bytes 完全一致；M3 对 reserved result/terminal event 还必须先
  通过 exact typed payload codec，并证明其重编码与 durable bytes 完全一致；
- `sequence/globalPosition` 是 exact positive signed-64-bit int，拒绝 bool-as-int；
- timestamp 固定 UTC 微秒格式；
- 所有文本按既有长度、NFC/control 规则验证；
- 使用 exact class 与 class-qualified method，拒绝动态方法遮蔽；
- Python 3.9 使用手写 slots，不使用 `dataclass(slots=True)`；
- 不从 package `__all__` 导出；
- 不在 events 表新增 digest 列，receipt 保存 digest，readback 时重算。

### 5.4 小步提交建议

1. `feat: define canonical stored event envelopes`
2. `test: freeze stored event envelope golden vectors`
3. `fix: reject noncanonical stored event payloads`
4. `test: exhaust stored envelope mutation matrix`
5. `docs: specify stored event envelope authority boundary`

每个提交单独通过 focused tests；不得在本阶段引入 writer 或 Accepted。

### 5.5 验收矩阵

- 固定 golden bytes/digest 在 Python 3.9/3.12/3.13 一致；
- 修改 actor、time、correlation、causation、idempotency、payload 任一叶子、sequence 或 global
  position，digest 都改变；
- key reorder、whitespace、escape、duplicate key、非对象根和错误 SQLite storage class 全拒绝；
- subclass、bool-as-int、hostile adapter、instance shadow、`object.__new__` 全失败关闭；
- 无 credential、lease token、Artifact 正文或 model narration 进入 envelope/repr/error/log。

### Phase 1 出口

codec-only 全量通过，且仍没有任何公共写 API 能据此签发 authority。

### 2026-08-28 M1 实施检查点

- 代码封板：`d889751e4cc3b7db548994a000a87e21688b4429`；
- Golden：372 bytes，digest
  `a7a2a28ed93454fe925dbdf676acd6bf758b9c5ac7afc50eeeae867d3d08e538`；
- Python 3.9.6 / 3.12.12 / 3.13.9 只读 verifier 同值通过；
- codec/golden 专项 102 tests；Python 3.13 全仓 2,489 tests；locked Ruff 0.16.3 与
  Mypy 1.19.1 strict 全绿；
- 详细证据：[`research/28_stored_event_envelope_codec_evidence.md`](./research/28_stored_event_envelope_codec_evidence.md)；
- 明确未完成：M2 fence、M3 store adapter、typed result dispatch、migration 7、writer、Observed
  readback、Accepted 和 worker 全部保持关闭。

## 6. Phase 2：Reserved Result Event Boundary

### 6.1 目标

在开放专用 writer 前，封锁 generic append 旁路，防止调用者直接写入看似 canonical 的结果事件。

### 6.2 必须拒绝

- 所有 `task.invocation.result.accepted`；
- `task.status.changed` payload 中出现 result terminal 保留词的 exact 或 near-canonical 组合：
  - `transitionKind`
  - `resultReceiptId`
  - `resultEventId`
  - `resultEvidenceDigest`
  - `runningTaskRevision`
  - `terminalTaskRevision`

精确合同冻结为：只检查 exact `task.status.changed` 的 store-owned payload 顶层 key；依次执行
`NFKC → casefold → NFKD → 仅保留 ASCII [a-z0-9]`，任一 key 的 skeleton 命中上述六项即拒绝，
不依赖 value、字段数量或 typed decode。nested key、字符串 value、其他 event type 不在该 terminal
namespace 内。Exact `task.invocation.result.accepted` 则与 payload 内容无关，全部拒绝。

### 6.3 必须覆盖的 generic surface

- `append`
- `append_many`
- `append_with_outbox`
- inbox/event 组合入口
- invocation admission/start 组合入口
- 任何未来公开 batch wrapper

检查必须作用于 store-owned snapshot，并在 `BEGIN` 之前完成。禁止增加 public
`trusted=True`、`allow_reserved=True` 或 caller-owned connection 逃生口。

### 6.4 兼容性

旧的五字段 `task.status.changed`、`READY → RUNNING` admission、demo/recovery 必须继续工作。
standalone `SQLiteInvocationAttemptStore.complete()` 对 canonical scoped job 必须结构性拒绝，不能
形成第二条 completion path。

### 小步提交建议

1. `feat: reserve canonical result event vocabulary`
2. `test: close generic result event append bypasses`
3. `fix: preserve legacy task status compatibility`
4. `docs: record reserved result event boundary`

### Phase 2 出口

每个 public/generic 入口的零写入失败测试通过，legacy tests 无回归。

### 2026-08-28 M2 实施检查点

- 代码/对抗修复封板：`dd0ba54`；
- `_snapshot_event` 保持未来 M3 私有 writer 可使用的纯冻结 primitive；caller-controlled 写入口只走
  class-qualified `_snapshot_generic_event`，没有 `trusted`、`allow_reserved`、caller connection 或
  transaction 参数；
- `append`、`append_many`、`append_with_outbox`、`append_inbox`、
  `append_invocation_admission` 均在 `BEGIN` 前检查 store-owned snapshot；canonical admission wrappers
  继续经过同一 base path，内部 start writer 只生成固定 start vocabulary；
- `SQLiteInvocationAttemptStore.complete()` 在同一写事务内、读 clock 与任何 DML 前，按 durable
  job/admission/execution/start 结构分类 scoped job；candidate query 由 canonical idempotency、exact
  payload `invocationId` needle 和 receipt coordinate 定位，最多 64 条，超限失败关闭；
- 独立逆向复核发现并修复了 stripped scope marker 降级与 type/key coordinate drift 两条真实旁路；
- legacy 五字段状态、schema-1 admission/start、attempt-only database、demo/recovery 继续兼容；
- 详细证据：[`research/29_reserved_result_event_boundary_evidence.md`](./research/29_reserved_result_event_boundary_evidence.md)。

M2 已达到本阶段出口，但没有开放 M3 adapter、migration 7、result writer、Accepted 或 worker。

## 7. Phase 3：Write-Snapshot 与 Raw-Row 双路重算

### 7.1 目标

让 store 在专用私有路径里：

1. 对实际交给 INSERT 的 `_EventWriteSnapshot` 计算 envelope；
2. INSERT 后拿到真实 sequence/global position；
3. 在同一事务内 SELECT 原始 row；
4. 从 raw row 独立重算；
5. 两路字段、bytes 与 digest 完全一致才继续。

### 7.2 禁止来源

以下对象都不能作为权威 digest 来源：

- caller `DomainEvent`；
- `DomainEvent.to_dict()`；
- `StoredEvent.to_dict()`；
- runtime `_canonical_event_json`；
- caller 提供的 coordinates/digest；
- 已经过宽松 JSON decode 的普通 read model。

### 7.3 小步提交建议

1. `feat: derive envelopes from event write snapshots`
2. `feat: verify envelopes from raw durable event rows`
3. `test: reject raw row storage and byte drift`
4. `test: reject caller event mutation around snapshots`
5. `docs: define durable envelope readback`

### Phase 3 出口

两路重算契约完成，但专用 result writer 仍不公开。

### 2026-08-29 M3 实施检查点

- 封板：`504824c`；前置提交 `3c1b9a8`、`f9da335`、`2df5b63`、`a35561c`、`296aae1`；
- private adapter 只接受 exact typed result/terminal snapshot；从真正 INSERT 的 hidden frozen bytes
  和 INSERT 后 fixed 11-column raw `sqlite3.Row` 独立重算 fields、canonical bytes 与 digest；
- idempotent replay 不可伪装 fresh insert；`changes()` 与 `total_changes` 冻结 zero-trigger-side-effect
  合同，relocation/clone、ignored INSERT、extra event 和 audit side effect 均失败并全回滚；
- contract/integrity/concurrency 三类 classified error 使用 fixed clean reissue，完整 exception graph 不
  保留 event/payload/digest canary；pre-M3 82-name wildcard surface exact 不变；
- M3 两文件 64 tests；M1–M3 与 typed models 组合 209 tests 在 CPython 3.9.6/3.12.12/3.13.9
  全绿；Python 3.13 全仓 2,578 tests、Ruff lint、Mypy strict 66 source files 全绿；
- 详细证据：
  [`research/30_stored_event_envelope_store_adapter_evidence.md`](./research/30_stored_event_envelope_store_adapter_evidence.md)。

M3 已达到本阶段出口，但没有 migration 7 schema/registration、Artifact same-transaction primitive、
result writer、receipt、Observed、Accepted 或 worker。

## 8. Phase 4：Inactive Result Schema、Artifact 事务原语与备份拓扑（已完成）

### 8.1 目标

准备 result durable graph 所需 schema 和跨组件事务原语，但仍不把 result migration 7 注册进 legacy
bootstrap。

### 8.2 Schema 候选

至少覆盖：

- result acceptance request/manifest digest 与 exact preimage identity；
- result receipt、evidence 和两个 event coordinates；
- invocation/job/attempt/result ref 绑定；
- Artifact candidate 顺序、version/head、blob/metadata digest；
- idempotency/conflict identity；
- outbox/result publication identity；
- schema version、created/accepted timestamps；
- tenant/workspace/session/plan/task/agent/invocation 全 scope 索引与唯一约束。

### 8.3 ArtifactStore 要求

- 增加只供 owner store 调用的 same-connection transaction primitives；
- caller 不能拿到 connection 或伪造“已经在事务内”；
- blob CAS、metadata insert、head CAS 与 result graph 在同一 SQLite transaction；
- rollback 后无孤立 metadata/head；
- blob 暂存/清理策略有 crash evidence；
- ordered candidates 的顺序进入 request/receipt digest。

### 8.4 Migration/backup 门禁

- migration 候选先走 domain migration graph，不直接追加 legacy `MIGRATIONS`；
- backup-v2 manifest/topology 必须包含新增表、索引、trigger 与依赖；
- sparse upgrade、fleet floor、旧版本 reader/writer 兼容矩阵明确；
- 空库与非空库 upgrade/restore/reopen/reconcile 均有测试；
- downgrade/rollback 策略不删除已经接受的结果图；
- 这些完成前 result migration 7 继续 disabled。`0005` 已用于 `native_im_inbox`，`0006` 已用于
  `native_im_sandbox_provenance`；Action Plane 使用 `0008`。

### 小步提交建议

1. `docs: define inactive result storage schema`
2. `feat: add inactive result domain migration candidate`
3. `test: freeze result schema topology`
4. `feat: add artifact same transaction primitives`
5. `test: prove artifact transaction rollback isolation`
6. `feat: extend inactive backup v2 result topology`
7. `test: reconcile nonempty result restore candidates`

### Phase 4 出口

schema/backup/artifact 组合证据通过，但默认 bootstrap、产品 UI 和 worker 仍不使用新路径。

### 8.5 2026-08-29 完成证据

- migration 7 只存在于 disabled domain descriptor 和 isolated rehearsal；legacy bootstrap、默认
  `SQLiteEventStore` 与 active migration registry 仍停在 6；
- 六张 result 候选表、八个显式索引、31 个 autoindex、down guard 与非空 v6→v7 演练已冻结；
- 私有 backup profile 固定 45 个对象，但 active backup registry 仍是 11 profiles / 88 objects；
- exact owner handle 绑定 store、connection、process owner 和 generation，不能 copy、pickle、跨
  store、跨 owner context 或 fork 复用；
- ordered Artifact batch 在 owner transaction 内完成 preflight、DML、readback 与 change accounting；
  写中失败会把 owner 标成 rollback-only；`os._exit` / SIGKILL 不留下 committed prefix；
- 完整 version history 使用最多 64 行一批的轻量 SQL 预检，所有 TEXT 先验证 storage class 与
  UTF-8 byte bound，再按 `rowid` 单行读取并重算 canonical metadata、request digest 和 UTC 时间；
- 所有 Artifact SQL 显式绑定 main schema；clock 前后与最终回读冻结 main 9-object DDL/rootpage/
  schema-version snapshot 并拒绝 TEMP shadow；clock 遗留 callback 被 strict writer callback fence
  接管，依赖意外关闭 transaction 时 store poison；
- clock 调用前建立带随机 128-bit 后缀的私有 SQLite savepoint，返回、抛错或非法时间路径都必须
  释放同一 savepoint；`COMMIT`/`ROLLBACK` 后重新 `BEGIN` 不能用“transaction 仍打开”伪装原事务，
  一律按 continuity ambiguity poison 并进入 reconcile-only；
- confirmed rollback 与 ambiguous outcome 固定分类；ambiguous control 保留干净控制类型，并以
  `_ResultArtifactCommitAmbiguityError` 为 cause，同时 poison store；异常图读取不执行 hostile
  属性钩子，也不会复活被 `from None` 抑制的历史 control；
- Result Artifact 专项 55 tests、M4 组合 96 tests、全仓 2652 tests、Ruff、Mypy 与 diff-check
  通过，最终独立 reviewer 为 0 blocker。完整证据见
  [`research/31_inactive_result_schema_artifact_transaction_evidence.md`](./research/31_inactive_result_schema_artifact_transaction_evidence.md)。

这些结果只关闭 M4 私有候选节点，不授权 migration 7 注册、Atomic Result Writer、`ObservedV2`、
`AcceptedV2`、worker result acceptance 或真实 IM outbound。

## 9. Phase 5：Atomic Result Acceptance Writer

### 9.1 公共输入

专用 store 方法只接受 exact：

- `ScopedInvocationResultAcceptanceRequestV2`；
- store-issued scoped start receipt/evidence；
- 当前 worker/attempt 的受保护调用上下文；
- Artifact 内容通过明确的受控输入边界进入，不把 raw body 放进 receipt/event envelope。

不得接受 caller coordinates、event IDs、receipt ID、acceptedAt、fresh boolean 或 authority token。

### 9.2 事务内验证顺序

1. 重新加载 persisted execution-request manifest 与 digest preimage；
2. 验证 tenant/workspace/session/plan/task/agent/invocation 全 scope；
3. 验证 exact scoped start receipt/evidence 与 start durable row；
4. 要求 effect class 为 `PURE` 且 `retryClass=never`；
5. 要求任务仍是 exact `RUNNING@runningRevision`；
6. 验证 job/attempt/attempt number/lease epoch/worker/lease token digest；
7. 验证 deadline、heartbeat、fence 与 store process identity；
8. 验证 ordered Artifact candidates、blob digest、metadata digest 与 expected head；
9. 由 store 单次采样 canonical microsecond `acceptedAt`；
10. 由 store 创建 receipt ID、result event ID、terminal event ID；
11. 构造 exact ResultEvidenceV2 与 TerminalTransitionV2；
12. 连续写入 result event 和 terminal event；
13. 用 raw rows 双路重算 envelope digest；
14. 写入 Artifact metadata/head、result receipt/ref；
15. CAS 完成 job/attempt/task terminal state；
16. 写入 outbox（若该阶段只做本地 durable message，则 publisher 继续 disabled）；
17. 在同一事务内完整 readback graph；
18. transaction body 只返回 private readback + `fresh_inserted`；
19. transaction context 正常退出后再分类 Accepted/Observed。

### 9.3 两个 canonical event

Result event：

```text
type            task.invocation.result.accepted
stream          session:<sessionId>
actor           canonical orchestrator
payload         exact ScopedInvocationResultEvidenceV2
timestamp       store-owned acceptedAt
correlation     persisted scoped manifest correlation
causation       scoped start event ID
idempotency     acceptanceIdempotencyKey
```

Terminal event：

```text
type            task.status.changed
stream          same session stream
actor           canonical orchestrator
payload         exact ScopedInvocationResultTerminalTransitionV2
timestamp       same acceptedAt
correlation     same correlation
causation       result event ID
idempotency     task-status:<taskId>:<terminalRevision>
```

### 9.4 冲突与隔离

- partial graph：integrity/conflict，不能修补后成功返回；
- exact replay：完整验证后 Observed；
- request/manifest/artifact drift：conflict；
- duplicate receipt/event ID 或坐标错序：integrity failure；
- transaction rollback 已确认：result transaction error；
- COMMIT ACK 不明确：poison 当前 store 并抛 result-specific ambiguity；
- ambiguity 后只能关闭并重开，新 store 全图验证后返回 Observed。

### 小步提交建议

writer 应拆成至少以下提交，任一提交都保持 API 不可误用：

1. `feat: validate result acceptance durable prerequisites`
2. `feat: atomically reserve result artifact heads`
3. `feat: append canonical result event pair`
4. `feat: persist scoped result receipts`
5. `feat: atomically complete result job and attempt`
6. `feat: read back complete result graphs in transaction`
7. `fix: quarantine partial and drifting result graphs`
8. `test: exhaust atomic result transaction faults`
9. `docs: specify atomic result acceptance writer`

### Phase 5 出口

完整事务图能够在 synthetic/local SQLite 环境通过并发与 fault injection；仍不启用 Accepted 或
产品 dispatch。

## 10. Phase 6：Observed Read、Replay、Recovery 与 ACK-Loss

### 10.1 API 行为

```text
read_scoped_invocation_result_v2(...) -> Optional[ObservedV2]
accept_scoped_invocation_result_v2(exact replay) -> ObservedV2
reopen / restart / peer process / recovery -> ObservedV2
```

读取必须在单一 bounded snapshot 中验证：

- request/manifest；
- start receipt/event/attempt；
- Artifact chain/head/blob/metadata；
- result/terminal raw event rows与 envelope digest；
- receipt self-digest 与全图坐标；
- job/attempt/task terminal result ref；
- outbox identity；
- 表/索引拓扑和 scope。

普通 `StoredEvent` read model 不足以完成这次权威验证。

### 10.2 ACK-loss 矩阵

| 场景 | 本次调用 | 重开后 |
| --- | --- | --- |
| COMMIT 前失败且 rollback 确认 | transaction error | 无结果 |
| COMMIT 成功并正常 ACK | fresh classification candidate | Observed |
| COMMIT 可能成功但 ACK 丢失 | ambiguity + poison，不返回成功 | 完整图则 Observed |
| exact peer 已先提交 | Observed | Observed |
| partial/tampered graph | conflict/integrity | 继续失败关闭 |

### 小步提交建议

1. `feat: observe complete scoped result graphs`
2. `feat: reconcile exact result acceptance replays`
3. `fix: poison ambiguous result commit stores`
4. `test: prove result ack loss never mints acceptance`
5. `test: prove peer and reopen observations`
6. `docs: define result recovery classifications`

### Phase 6 出口

所有 durable read/recovery path 只返回 Observed；没有 Accepted class 也能完成整套恢复验证。

## 11. Phase 7：Fresh-COMMIT `AcceptedV2`

### 11.1 最小语义

Accepted 只包裹一份重新验证的 ReceiptV2 snapshot，不增加可持久化的 `accepted=true` 或
`fresh=true` 字段。

### 11.2 构造边界

只允许在公共 store 方法满足以下全部条件后创建：

1. 本调用新写了完整图；
2. transaction body 完整 readback 通过；
3. transaction context 正常退出；
4. COMMIT 正常返回 ACK；
5. post-COMMIT process/store lifecycle check 通过；
6. 没有 control signal、ambiguity 或 cancellation 漂移。

### 11.3 对象约束

- manual opaque slots；
- private store issuer token；
- 无 `to_dict/from_dict`；
- copy/deepcopy/pickle/reduce 全稳定 `TypeError`；
- opaque repr/str；
- 不提供值相等 authority；
- fork、另一 store、token transplant、GC/id reuse、reflective tampering 均不能创建权限；
- 推荐只作为 store 调用的即时返回分类，不被任何后续 API 接受为授权输入；
- 如果未来必须消费它，增加 issuer weak identity registry，而不是信任 `isinstance`。

### 11.4 返回矩阵

| 情形 | 返回 |
| --- | --- |
| 本调用 fresh 写入 + 正常 COMMIT ACK | `AcceptedV2` |
| exact replay | `ObservedV2` |
| read/reopen/restart/recovery/peer | `ObservedV2` |
| ACK-loss/ambiguous exit | 抛错并 poison，无成功对象 |
| drift/partial/tamper | conflict/integrity failure |

### 小步提交建议

1. `feat: define process-bound result acceptance outcomes`
2. `feat: issue result acceptance after fresh commit ack`
3. `test: reject copied and reconstructed acceptance outcomes`
4. `test: reject fork store and issuer transplants`
5. `test: distinguish fresh acceptance from every replay path`
6. `docs: freeze accepted versus observed semantics`

### Phase 7 出口

Accepted 的唯一 mint 点可由代码和故障测试机械证明；仍不能据此自动打开生产 Gate。

## 12. Phase 8：集成、迁移晋级与产品可见性

该阶段只有在 Phase 1–7 独立评审通过后才开始。

### 12.1 Result Migration 7 晋级

- fleet floor 已冻结；
- active writer/reader compatibility 已证明；
- backup-v2/topology/restore/reconcile 完成；
- upgrade/downgrade/rollback runbook 完成；
- 非空生产等价快照演练有证据；
- 才能单独提交 migration registration；
- 注册和 worker dispatch 不能在同一提交。

### 12.2 Worker dispatch

- 先只接 PURE、`retryClass=never` scoped jobs；
- dispatch 只消费 store-owned verified state，不消费 caller Accepted；
- cancellation、heartbeat、lease expiry、process mismatch 与 shutdown 有端到端测试；
- publisher/outbox ACK-loss 和 dead-letter 已闭合；
- 真实外部副作用仍需 action-time authorization 和独立 receipt，不因 result writer 完成而开放。

### 12.3 产品体验

- 在本地 UI 显示 Accepted/Observed 分类和证据坐标；
- 不展示内部 token/credential/digest preimage；
- 提供 Artifact diff、结果引用、恢复来源和失败原因；
- UI 明确区分“新提交成功”“已观察到既有结果”“提交结果未知”；
- Gate A–E 状态继续来自 readiness truth，不由 UI 自行推断。

### 12.4 文档

更新：

- `docs/production/ADR_0005_ATOMIC_RESULT_AUTHORITY.md`
- `docs/production/CURRENT_READINESS.md`
- `docs/production/RELEASE_GATES.md`
- result writer/recovery/operations runbook
- `docs/LOCAL_PRODUCT_TRIAL.md`
- `analysis_report/README.md`
- 阶段 release evidence 与 report-sync bundle

## 13. 全阶段测试矩阵

| 类别 | 必测内容 |
| --- | --- |
| Golden codec | 固定 bytes/digest，Python 3.9/3.12/3.13 一致 |
| 字段覆盖 | 任一 envelope 字段或 payload 叶子改变都改变 digest |
| JSON canonical | key/space/escape/duplicate/NaN/root/storage class drift 全拒绝 |
| Exact typing | bool-as-int、subclass、unknown/missing/future schema 全拒绝 |
| Caller forgery | method shadow、object new/setattr、伪 coordinates/digest、hostile adapter |
| Snapshot drift | snapshot 前/中/后 caller mutation，不影响冻结写入或造成旁路 |
| Reserved fence | 每个 generic append surface 零写入失败；legacy status 通过 |
| Raw-row tamper | 每列、坐标、payload bytes、SQLite type 修改后 readback 失败 |
| Cross-binding | actor/start/terminal causation、correlation、idempotency、顺序对调失败 |
| Artifact | order、blob/metadata、head CAS、parent/version、rollback、orphan cleanup |
| Transaction fault | 每个 insert/CAS/readback/COMMIT 前后 fault，无可见 partial graph |
| ACK loss | durable commit + lost ACK 只能 reopen 为 Observed |
| Concurrency | 两 connection、两 spawned process：一个 fresh，其余 Observed/conflict |
| Process | fork/spawn/peer/reopen/store ownership/token transplant/GC id reuse |
| Backup/migration | empty/nonempty upgrade、restore、reconcile、fleet floor、rollback |
| Secret canary | lease/token/Key/Artifact body/narration 不进 repr/error/log/WAL/SHM |
| UI/API | Accepted/Observed/Unknown 分类、loopback/auth/token、disconnect/retry |
| Regression | 全量现有 pytest、ruff、mypy、distribution/branch/terminology gates |

## 14. 安全与 NO-GO 门禁

任何一项成立都必须停止，不能用文档措辞绕过：

- durable row 不能独立重算 event envelope digest；
- generic append 仍能写 reserved result vocabulary；
- readback 使用 `StoredEvent.to_dict()` 替代 raw row；
- fresh COMMIT ACK 与 replay 无法机械区分；
- ACK-loss 会返回 Accepted；
- Accepted 可 copy/pickle/reopen/跨 store 重建；
- partial graph 能被自动补齐后当成 fresh success；
- Artifact、task、attempt、event、receipt、outbox 不在同一事务；
- migration/backup topology 对非空库不完整；
- worker 能绕过 store verified state；
- 日志、WAL/SHM、exception 或 UI 出现完整 credential/lease token；
- Python 3.9 compatibility 或 supported-version matrix 失败；
- 新参考项目许可证/来源不清楚；
- Gate 状态没有 retained evidence 却被改成通过。

## 15. 提交、Worktree 与评审纪律

### 15.1 Commit

- 一个独立不变量或一个测试矩阵扩展一个 commit；
- 每个 commit 可构建、可测试、可回滚；
- 实现、测试、文档可分开提交，但稳定阶段三者必须齐全；
- 不用“misc”“cleanup”掩盖多个语义变化；
- 不在同一 commit 注册 migration、开放 writer 和启用 worker；
- 每个阶段完成后推送当前评审分支并远端读回 SHA；用户验收前不自动合并 `main`。

预计本计划会产生数十个小提交；数量不是目标，可审查性和每一步稳定才是目标。

### 15.2 Worktree

- 并行工作只在
  `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/` 下建短生命周期
  worktree；
- 每个 worktree 只负责一个不重叠主题；
- 不让多个 Agent 同时编辑 `store.py` 的同一区域；
- 合并前 rebase/cherry-pick 到最新 main，独立全量验证；
- 完成后先推当前评审分支；经用户验收后才合并目标分支，需要保留独立尖端时建同 SHA
  `archive/*`；
- 最后 `git worktree remove`、删本地阶段分支、删远端活动分支；
- 每次生命周期变化刷新 `BRANCH_CATALOG.md`。

### 15.3 独立评审

每个高风险阶段至少安排：

1. 实现者自测；
2. 独立 adversarial reviewer 做 P0/P1/P2 审计；
3. migration/backup reviewer；
4. process/concurrency/fault reviewer；
5. 主线全量验证与文档事实复核。

发现 P1/P2 时先补失败测试，再修实现；不能只改说明。

## 16. 预期交付物

实现阶段结束时至少应有：

- private stored-event envelope codec；
- reserved result event boundary；
- write-snapshot/raw-row dual verifier；
- inactive result schema 与 backup-v2 topology；
- Artifact same-transaction primitives；
- atomic result acceptance writer/readback；
- Observed replay/ACK-loss readback 与 receipt-bound non-emitting reconciliation；
- process-bound AcceptedV2；
- migration/worker 的独立晋级门禁；
- 对应单元、属性、故障、并发、process、backup、UI tests；
- ADR、runbook、readiness、阶段证据、教程和分支目录更新。

## 17. 阶段里程碑与可停点

| 里程碑 | 可停条件 | 下一步授权前保持关闭 |
| --- | --- | --- |
| M0 参考复评 | 新项目 delta/ADR 完成 | 全部实现 |
| M1 Codec（已完成） | golden/canonical/raw JSON contract 通过 | writer、Accepted |
| M2 Reserved fence（已完成） | generic bypass 全封 | writer、Accepted |
| M3 Store adapter（已完成） | snapshot/raw-row 双验通过 | writer public API |
| M4 Inactive schema（已完成） | migration/backup/artifact 候选通过 | migration registration |
| M5 Atomic writer（已完成） | 完整事务图/fault/readback 通过；`ObservedV2` 与 opt-in migration-7 已形成 | 生产 worker、projection |
| M6 Recovery（部分完成） | receipt-bound non-emitting reconciliation、idempotent replay、stale/CAS/trigger rollback 通过；crash/kill/restore replay 与双连接证据仍待完成 | 生产 worker、projection |
| M7 Accepted（候选完成） | fresh ACK 唯一 mint 点通过；ACK-loss/reopen 与 replay 不升级证据已通过 | migration/worker promotion |
| M8 Integration | 独立 release evidence 通过 | 生产 Gate 仍需分别审批 |

本计划现在是 E3 Result Authority 的当前串行入口。提前接入路线的 E1/E2 离线节点已完成，M1–M5
均已形成安全停点；当前分支又完成了 opt-in migration-7 activation、receipt-bound
reconciliation、store-owned result acceptor、fresh-ACK `AcceptedV2` 与接受期间 heartbeat fencing。
active backup/restore topology、非空迁移演练、离线 PURE heartbeat supervisor、ACK-loss/reopen
与 replay evidence 已完成；下一串行实现节点是业务 projection、crash/kill/双连接恢复、
compatibility/rollback evidence 与独立 production promotion。实现必须复用 M3 的 stored-event
adapter 与 M4 owner transaction，且仍不得开放真实 IM outbound。
若用户新增会改变底层 result/store 方向的参考项目，仍先做 M0 delta review，不从原子 writer
中途改变合同。

## 18. 远端文档策略

本地主仓继续作为 canonical source。用户在 2026-08-28 进一步确认：Notion 页面写入、图片上传和
逐页回读明显影响开发速度，因此当前计划的本地任务全部完成前不再逐阶段操作 Notion；开发期间只
更新本地文档并频繁 commit/push，全部任务完成后再一次性批量同步并逐页回读。私人语雀仍只有用户
另行明确授权时才操作：

- 新报告先落本地、进入 Git 并推送 GitHub；
- 全部当前计划任务完成后同步 Notion 页面、附件、manifest/checkpoint 并逐页 fetch 回读；
- 回读未完成时只声明“已写入待核验”，不能声明同步闭环；
- 不自动操作私人语雀；
- 永远不向飞书、企微、任何人、任何群聊、bot 或 webhook 发送消息。

当前截至 `3a92f3c` 的稳定内容已经完成 Notion 同步；M2、M3、M4 及后续本地增量只进入 Git/GitHub，
不能在最终批量同步前声称已进入 Notion。

## 19. 启动下一阶段时的第一组命令

继续本最大强度 Result Authority 路线时，从以下只读检查开始；下一步是本文件 Phase 5，真实 IM
Level B 仍以 `NATIVE_IM_EARLY_INTEGRATION_PLAN.md` 的合同输入为独立门禁：

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/mainline_continue_quantum_entanglement
git status --short
git worktree list --porcelain
git branch --show-current
git ls-remote --heads \
  ssh://git@ssh.github.com:443/huapohen/quantum-entanglement.git \
  refs/heads/mainline_continue_quantum_entanglement
PYTHONPATH=src python3 -m pytest
```

确认 clean baseline 后按本计划的 M6 recovery / projection 子阶段开始；默认 migration-7 仍关闭，
opt-in candidate writer 仅用于离线 rehearsal。没有用户新的继续指令，不进入真实 sandbox 网络，
不开放生产 worker、publication、真实 IM 或任何 outbound。
