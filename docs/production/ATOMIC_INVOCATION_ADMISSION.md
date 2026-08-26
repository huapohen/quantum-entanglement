# 原子 Invocation Admission：生产合同、恢复与运维手册

状态：**admission spine 已实现并推送；它不是 worker 执行、result/artifact acceptance 或生产晋级完成。Gate A–E 全部关闭。**

本文描述 `SQLiteEventStore.append_invocation_admission(...)` 的已提交合同。它解决的边界只有一个：
调用方决定让一个 task 进入执行队列时，把同一 session stream 的事件批次、一个 `queued`
invocation job 和一张不可变 admission receipt 放进同一个 SQLite transaction。它不声称 Agent 已经
运行，也不声称 result、artifact、attempt terminal state 或任何外部副作用已经完成。

实现与 schema 分别绑定以下已推送提交：

| 内容 | 提交 |
|---|---|
| migration 4、domain migration/backup topology、升级/降级与恢复测试 | `245ecde231e1292f2e0e39d7bcf409c44243763d` |
| atomic admission API、receipt 验证、故障/竞争/process 测试、包级公开类型 | `5226ef1994fab5f165c244ac0716ca329c0950fe` |

后续文档提交不会改变上述源码边界。每次发布仍必须对最终候选提交重新生成 source-bound evidence；
仅引用实现提交不能证明当前 checkout、CI runner 或部署制品与它们一致。

## 1. 解决的问题与明确不解决的问题

过去可能出现以下裂缝：session 已记录 `RUNNING`，但 invocation job 尚未入队；或者 job 已入队，
事件尚未提交。进程在两次提交之间退出后，恢复方无法知道应该补哪一边，也无法安全判断是否可以
再次调用 Agent。

现在一个 admission transaction 的提交单元是：

```text
BEGIN IMMEDIATE
  ├─ append caller-supplied session events in exact order
  ├─ enqueue one immutable invocation job in queued state
  ├─ insert one receipt binding the complete event/job unit
  └─ read back and validate the receipt, events and job
COMMIT
```

标准调用会让事件批次包含 execution request 与 `RUNNING` transition，但 admission API **不会**
解释 event type 或 payload 业务语义。它不会验证 payload 中的 `taskId` 是否等于
`InvocationJobSpec.task_id`，也不会自行生成 `RUNNING` 事件。这个语义检查仍必须由未来可信的
runtime command boundary 完成。

本实现尚未完成：

- caller-owned atomic first claim、attempt 创建、lease/epoch fencing 与 start event 的统一提交；
- heartbeat-supervised worker、SIGTERM bounded drain、kill/restart reconciliation；
- immutable result receipt，以及 result、artifact、attempt success、task terminal projection 的原子
  acceptance；
- receipt-aware claim gate。现有 invocation attempt store 可以独立看到 `queued` job，但
  `OrchestratorKernel` 尚未接入本 admission API；
- tenant/workspace-scoped admission identity、可信 `RequestContext` 与 action-time authorization；
- connector action receipt 或 `effect_unknown` 外部副作用协调。

因此，当前代码不能因为“job 已排队”就被描述为端到端 exactly-once execution。尤其在 COMMIT
acknowledgement 丢失时，job 可能已经可见；在 receipt-aware claim 协议完成前，不得让真实副作用
worker 自动消费这条路径。

## 2. 原子提交不变量

一次新 admission 只有同时满足以下不变量才会提交：

1. `events` materialize 后不能为空；每个元素必须能收敛为 exact、bounded、canonical
   `DomainEvent` snapshot，调用方对象不会在持锁后才被延迟求值。
2. 所有 event 的 `stream_id` 必须精确等于 `session:<spec.session_id>`。
3. 批次内 `event_id` 不得重复；非空的 event `idempotency_key` 也不得重复。
4. 若提供 `expected_version`，新写入前的 stream version 必须与它精确相等。生产调用方应始终提供
   已观察的 version，而不是用 `None` 绕过 optimistic concurrency。
5. event sequence 从 `original_version + 1` 开始连续增长；同一批 event 的 global position 也必须
   是连续区间。
6. job 使用同一 transaction 中的 invocation enqueue primitive 写入，初始状态为 `queued`；
   `invocation_id`、`(session_id, task_id)` 与 `(session_id, idempotency_key)` 保持既有唯一身份合同。
7. receipt 最后写入，并在 transaction 内立即从持久行重新解码；关联 job、每个 event、sequence、
   global position、canonical manifest digest 和 immutable job digest 全部验证通过后，transaction
   body 才算完成。
8. receipt、events 与 job 要么一起可见，要么在已确认 rollback 后一起不可见。没有 receipt 的
   event/job 组合永远不能在运行时“补票”。
9. 相同身份只有 complete exact replay 才返回原始 `StoredEvent` tuple 与原始 `InvocationJob`；任何
   内容、顺序、身份、stream version 或 enqueue policy 变化都拒绝。

SQLite `BEGIN IMMEDIATE` 把 version read、event append、job enqueue 和 receipt insert 放在一个
writer critical section 内。两个 connection 或两个 fresh process 竞争不同 binding 时只能有一个
winner；另一个得到 conflict，而不是产生两份工作。

## 3. Durable receipt 格式与绑定内容

表名为 `invocation_admissions`，格式版本为
`qe.invocation-admission-receipt/1`。公开 API 返回的是验证后的 events/job，不直接暴露可由调用方
修改或伪造的 receipt 对象。

| 字段 | 合同 |
|---|---|
| `invocation_id` | receipt 主键，同时外键绑定 `invocation_jobs.invocation_id` |
| `receipt_format` | 必须精确为 `qe.invocation-admission-receipt/1` |
| `session_id` | admission 所属 session identity |
| `task_id` | session 内 task identity；与 `session_id` 组成唯一键 |
| `stream_id` | 必须精确为 `session:<session_id>` |
| `job_idempotency_key` | 与 `session_id` 组成唯一键，并绑定 durable job idempotency identity |
| `original_version` | admission 观察到的写前 stream version |
| `event_count` | 正整数，必须与 event ID 数量和 sequence range 一致 |
| `event_ids_json` | exact order 的 canonical JSON string array；不得重复或非 canonical encoding |
| `first_sequence` | `original_version + 1` |
| `last_sequence` | `original_version + event_count` |
| `first_global_position` | 批次第一条 event 的 durable global position |
| `last_global_position` | `first_global_position + event_count - 1` |
| `event_manifest_sha256` | 完整、有序、canonical event manifest 的 lowercase SHA-256 |
| `job_binding_sha256` | immutable enqueue request 的 lowercase SHA-256 |
| `admitted_at` | canonical timestamp，同时必须等于 durable job `created_at` |

event manifest 对每条 event 按顺序绑定：`stream_id`、`event_type`、`actor_id`、`event_id`、
`timestamp`、`correlation_id`、`causation_id`、`idempotency_key` 和 canonical payload JSON。sequence 与
global position 不放入该 digest，而由 receipt 的连续区间字段单独绑定。

job binding digest 绑定 `invocation_id`、`session_id`、`plan_id`、`task_id`、`agent_id`、
`idempotency_key`、`payload_digest`、`priority`、`max_attempts` 和 normalized requested
`available_at`。job 的 mutable lease/status/result 字段不属于 admission 时的 immutable request；
它们需要后续 claim/result receipt 协议保护。

schema 还用 `CHECK` 约束格式、stream identity、正数/range 与 lowercase SHA-256，用 `RESTRICT`
foreign key 保护 job 以及 event 首尾 sequence/global position。read/replay 会逐条读取整个 event
range 并重算 manifest，所以即使数据库曾在 foreign key 关闭时被离线篡改，也不能只靠首尾外键
蒙混通过。

## 4. Exact replay 与 fail-closed 判定

重放不是“使用相同 task 再构造一份近似请求”。调用方必须保留并重用原始：

- `invocation_id`、session/plan/task/agent identity 与 job idempotency key；
- payload digest、priority、max attempts 与 requested available time；
- event IDs、event 顺序、所有 event envelope 字段与完整 payload；
- admission 首次使用的 `expected_version`。

API 会同时按 `invocation_id`、`(session_id, task_id)` 和
`(session_id, job_idempotency_key)` 搜索 receipt：

- 找到一张 receipt 且 receipt、全部 events、job 与本次请求完全一致：返回首次提交的原始 rows；
- 找到的 identity 已绑定到不同 receipt 或不同工作：
  `InvocationAdmissionConflictError`；
- receipt 存在，但 receipt 本身 malformed、关联 event/job 缺失、range/digest/job binding 不一致：
  `EventStoreIntegrityError`；
- receipt 不存在，但任一请求 event identity/idempotency identity 或 job identity 已存在：
  `InvocationAdmissionConflictError`，无论数据库里看起来是 partial split 还是 events/job 两边都齐全；
- replay 提供的 `expected_version` 不等于 receipt 的 `original_version`：`ConcurrencyError`。它不会拿
  admission 之后的当前 stream head 代替原始写前 version。

这意味着下列状态一律 fail closed：只有部分 events、只有 job、events 与 job 都在但 receipt 丢失、
跨 stream 拼接、receipt digest 被改、receipt 指向缺失 row、同一批 event 调换顺序。运行时不得
通过直接 `INSERT` receipt、修改 digest、删除冲突行或更换 idempotency key 来“修复”。

## 5. Transaction failure、COMMIT ambiguity 与 poison

| 结果/异常 | 已知事实 | 调用方动作 |
|---|---|---|
| `InvocationAdmissionResult` | exact unit 已提交并在 transaction 内验证，或 exact replay 已验证 | 可以把 admission 视为 durable；仍不能视为已执行 |
| `InvocationAdmissionTransactionError` (`invocation_admission_transaction_failed`) | COMMIT/BEGIN 失败且 rollback 已确认 | 同一 store 未 poison；允许按有界策略重试 exact request，仍需保留原 version CAS |
| `InvocationAdmissionCommitAmbiguityError` (`invocation_admission_commit_ambiguous`) | transaction 可能已提交，但 COMMIT 或 rollback acknowledgement 不足以证明唯一结果 | 停止当前 store 的所有业务访问，close、reopen，再 exact replay |
| `EventStorePoisonedError` (`event_store_poisoned`) | 该 store instance 已经历未知 transaction outcome | 除 `close()`/context exit 外不得继续使用；重新构造连接 |
| `InvocationAdmissionConflictError` | identity 已被不同、partial 或无 receipt 的状态占用 | 停止 dispatch；进入证据驱动 reconciliation，禁止自动换 ID |
| `EventStoreIntegrityError` | receipt 或其 durable binding 不可信 | 隔离数据库与 worker，保留副本，执行完整 integrity/restore 调查 |
| `ConcurrencyError` | 写前 version 或 replay original version 不匹配 | 重新加载 session 决策；不得盲目提高 `expected_version` |
| `EventStoreLifecycleError` | 使用了 fork-inherited/wrong-process store | child 停止 admission 并 `_exit`/exec；在最终 worker 内 fresh construct |

COMMIT 抛错后，如果 SQLite 仍确认 transaction open 且 rollback 成功，实现报告稳定 transaction
failure，不误报 ambiguity。若 COMMIT 可能已经生效、transaction state 不可信、rollback 本身失败或
acknowledgement 丢失，则报告 ambiguity 并 poison 当前 store。public entry 和锁获取后的第二次
poison 检查共同阻止已经排队等待锁的 read、write 或 admission 在 poison 后继续访问 connection。

poison 是“当前连接图不可再信任”，不是“数据库必然损坏”。`close()` 被显式允许，是为了释放当前
进程拥有的资源；关闭成功后仍必须用 fresh `SQLiteEventStore` 做 exact replay。不要读取
`_connection`、`_poisoned` 或私有 receipt helper 作为生产恢复 API。

## 6. Control signals

exact `KeyboardInterrupt`、`SystemExit`、`GeneratorExit` 和 `asyncio.CancelledError` 不会被普通
`Exception` 包装吞掉。admission boundary 会先让内部 transaction/caller frames 解引用，再构造一个
clean、同类型的新 control signal：

- rollback 已确认时，fresh control signal 没有 ambiguity cause；
- commit/rollback 结果未知时，fresh control signal 的显式 cause 是一个无 traceback 的
  `InvocationAdmissionCommitAmbiguityError`，store 同时 poison；
- `SystemExit` 只保留安全的 `None`、bool 或 0–255 integer code；其他 code 收敛为 `1`；
- caller 原始 signal object、driver exception、SQL、path、job/event identity 和 caller argument
  不会被保留在公开错误图中。

捕获 cancellation 的 service lifecycle 必须先检查显式 ambiguity cause；不能因为外层看到的是
`CancelledError` 就自动再次 admission。

## 7. Process/fork 合同

`append_invocation_admission` 继承 `SQLiteEventStore` 的 exact PID + opaque epoch owner contract：

- fork child 使用 inherited store 时，在读取 caller argument、lock 或 SQLite connection 前稳定拒绝；
- child 不得对 inherited transaction 做 commit、rollback、close 或普通解释器 teardown；应停止
  readiness/admission 后使用 `os._exit` 或 exec；
- live store、transaction context、stream iterator 及其可达 dependency graph 都不可作为 fork
  后复用许可；
- 多进程竞争只允许各 worker 在 spawn/exec 或 fork-before-initialization 后 fresh construct store；
- fresh 两 connection 与 spawn 两 process 的对抗测试证明不同 binding 只有一个 winner；这不是
  多实例 HA、Linux production runner 或跨主机一致性证明。

完整进程拓扑要求见
[`SQLITE_EVENT_STORE_PROCESS_BINDING.md`](./SQLITE_EVENT_STORE_PROCESS_BINDING.md) 和
[`PROCESS_INHERITANCE.md`](./PROCESS_INHERITANCE.md)。credential-bearing worker 仍必须遵守
spawn/exec-before-secret-load；event store guard 不能擦除 fork 时已经复制的 secret。

## 8. Migration 4、rollback 与 backup/restore

### 8.1 Upgrade contract

`0004_invocation_admissions.up.sql` 是正式、checksum-bound schema migration：

- domain coordinate 为 `admission/1`；
- 依赖 migration 1 的 invocation job schema；
- schema table、显式 index、自动唯一 index、foreign key 与 `CHECK` constraints 全部进入 exact
  topology validation；
- migration body 与 `qe_schema_migrations` version 4 ledger row 同 transaction 提交；
- populated v3 数据库可以升级到 v4，不修改既有 jobs、attempts、artifacts 或 outbox ambiguity；
- 预先伪造的 weakened `invocation_admissions` table 会触发 drift，不能得到 v4 ledger row；
- 当前 validator 在传入仅到 v3 的 registry 时，会在不修改数据库的前提下拒绝
  v4 database。这是进程内 registry simulation，不是历史 v3 wheel 的独立进程实证；
  mixed-wheel/process matrix 仍是发布门禁。

部署必须先停 admission/worker，备份并验证 v3 数据库，再用 v4 binary 完成 migration preflight；
不能让 constructor-time 隐式升级替代 production startup gate。

### 8.2 Down migration is proof-destructive

`0004_invocation_admissions.down.sql` 会删除 receipt index/table 和 version 4 ledger row，保留 events 与
invocation jobs。自动测试证明 SQL 可执行并可重新升级，但这只是 schema rehearsal，**不是逻辑上
无损的生产回滚**。

一旦已有 receipt 被 down migration 删除，原 events/job 会成为“无 receipt 的完整 split binding”；
重新升级后 exact replay 必须 conflict，不能自动补票。因此 active database 的 rollback 必须：

1. 停止新 admission、claim 与 execution；
2. 保存并验证包含 v4 receipt 的不可变备份；
3. 为每条已 admission job 制定显式 disposition/reconciliation；
4. 获得数据丢失/证明丢失审批后才执行 down SQL；
5. 重新升级时不得通过手工 receipt synthesis 绕过 fail-closed contract。

### 8.3 Backup and restore

`invocation_admissions` 已进入 backup v1 table inventory；其 schema objects 属于
`qe.domain-migration-0004/1` topology profile。当前测试覆盖：

- non-empty receipt 进入 online backup manifest table count；
- restore 后 foreign key check 为空，并可用同一请求 exact replay 得到原始 admission result；
- v3 backup 可以先恢复，再由 v4 binary 升级得到空 receipt table；
- future version、migration gap/checksum drift、缺失或 weakened migration-owned object 在 backup
  publication 前 fail closed。

恢复操作仍必须执行 [`SQLITE_BACKUP_RESTORE.md`](./SQLITE_BACKUP_RESTORE.md) 的完整验证、目标路径
保护与 non-emitting reconciliation。receipt 被保留只证明 admission unit，不证明 worker 没有执行或
外部 effect 状态已知。

## 9. API 示例

下面示例刻意固定所有 identity、timestamp、payload 和 expected version。生产 command handler 必须
在可信授权、task semantic validation 与 canonical payload construction 后生成这些值，并把 exact
request 保留到 reconciliation 完成；不得在异常后重新调用随机 ID factory。

```python
from quantum_entanglement.attempts import (
    InvocationJobSpec,
    invocation_payload_digest,
)
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.store import (
    InvocationAdmissionCommitAmbiguityError,
    SQLiteEventStore,
)

database_path = "state.sqlite3"
recorded_at = "2026-08-26T09:00:00Z"
invocation_payload = {
    "taskId": "task-42",
    "instruction": "summarize the approved local fixture",
}

spec = InvocationJobSpec(
    invocation_id="invocation-42",  # generate once; persist until reconciled
    session_id="session-7",
    plan_id="plan-3",
    task_id="task-42",
    agent_id="research-agent",
    idempotency_key="invoke:session-7:task-42",
    payload_digest=invocation_payload_digest(invocation_payload),
    priority=50,
    max_attempts=1,
)
events = (
    DomainEvent(
        stream_id="session:session-7",
        event_type="task.execution_requested",
        payload={"taskId": "task-42", "payloadDigest": spec.payload_digest},
        actor_id="orchestrator",
        event_id="event-task-42-requested",
        timestamp=recorded_at,
        idempotency_key="admission:task-42:requested",
    ),
    DomainEvent(
        stream_id="session:session-7",
        event_type="task.status_changed",
        payload={"taskId": "task-42", "status": "running"},
        actor_id="orchestrator",
        event_id="event-task-42-running",
        timestamp=recorded_at,
        causation_id="event-task-42-requested",
        idempotency_key="admission:task-42:running",
    ),
)

store = SQLiteEventStore(database_path)
try:
    admitted = store.append_invocation_admission(
        events,
        spec,
        expected_version=0,
    )
except InvocationAdmissionCommitAmbiguityError:
    store.close()
    # Reuse the exact same events, spec and original expected_version.
    with SQLiteEventStore(database_path) as reopened:
        admitted = reopened.append_invocation_admission(
            events,
            spec,
            expected_version=0,
        )
else:
    store.close()

assert admitted.job.status.value == "queued"
```

`InvocationAdmissionResult.events` 与 `.job` 是 durable readback。示例最后的 assertion 只确认 job
已排队；它不是允许调用真实 connector 或把 task 投影为 completed 的条件。

## 10. Operator reconciliation runbook

出现 `invocation_admission_commit_ambiguous`、poison、conflict 或 integrity failure 时：

1. 立即停止该 database/service shard 的 admission 与 worker claim；若当前系统没有 receipt-aware
   claim gate，则该缺口本身阻止生产 readiness。
2. 日志只记录 stable error code、source commit、database logical identity 和受控 correlation digest；
   不记录 prompt、payload、完整 identifiers、SQL/driver detail 或任何 credential。
3. 对 poisoned instance 只执行 best-effort `close()`；不要做 readback、stream version 查询或私有
   connection inspection。
4. 保存原始 immutable request。若进程崩溃后无法重建 exact event/job manifest，状态必须保持
   unknown/manual，不得生成“等价”请求。
5. 在正确 process topology 中用同版本 binary fresh open；先完成 migration、SQLite integrity、
   foreign key、backup compatibility preflight。
6. 用原 events/spec/original expected version 调用同一 admission API：
   - 返回 result：receipt 证明首次提交存在，或在确认无 durable state 时安全完成了同一 unit；
   - transaction error：rollback 已确认，可按有界策略继续 exact retry；
   - concurrency：session 已由别的 command 推进，转入重新规划，不能改 version 强行写入；
   - conflict：存在不同或无 receipt binding，冻结 job，导出只读证据，人工决定 disposition；
   - integrity：隔离数据库，禁止 claim/execute，优先从已验证 backup 恢复并做完整差异审计。
7. 不允许通过 SQL 手工新增/更新/删除 receipt 或关联 event/job。必要的数据修复必须有单独 versioned
   migration、双人审查、原始备份和 retained rehearsal evidence。
8. admission reconcile 成功后，也只能进入未来的 receipt-aware claim state machine；没有 result
   receipt 时不得把 task 猜成 completed，没有 action receipt 时不得猜测外部 effect。

## 11. 已提交测试资产与本地观察

提交 `245ecde...` 与 `5226ef1...` 包含以下 deterministic/adversarial coverage：

- 25 个 admission tests：atomic happy path、exact replay、changed binding、partial/full split without
  receipt、receipt/job/event tamper、post-job fault rollback、BEGIN/COMMIT/ROLLBACK acknowledgement
  fault、clean control signals、poison post-lock recheck、双 connection、spawn 双 process、fork
  inherited rejection；
- 8 个 migration-4 tests：populated v3 upgrade、exact table/index schema、weakened prebuilt table、down
  rehearsal、old-binary fence、FK/CHECK/digest、commit failure 与 rollback acknowledgement failure；
- backup module 中包含 non-empty admission receipt round-trip 与 v3 restore-forward-upgrade 测试，
  同时继续覆盖 manifest/schema/path tamper 和恢复发布边界。

2026-08-26 在源码 HEAD `5226ef1994fab5f165c244ac0716ca329c0950fe` 所在当前工作树本地执行：

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_invocation_admission \
  tests.test_invocation_admission_migration \
  tests.test_backup
```

观察结果为 `Ran 77 tests ... OK`。执行时工作树另有与本专项无关的未提交文件，所以这是一条诚实的
本地验证观察，不是 clean-checkout、不可变 CI、独立 runner、正式 release evidence 或生产批准。
完整 suite、lint/format、strict mypy、dependency locks、package/SBOM 和最终 source-bound gates
仍需在包含本文的最终候选提交上重新运行和保留。

## 12. Readiness 结论

该实现关闭的是“session event 与 queued invocation job 分两次提交”的局部一致性缺口，并为 exact
replay 提供 durable proof。它没有关闭 invocation 生命周期其余边界，也没有让 runtime、tenant、
API、connector、deployment 或 observability 达到生产标准。

在 caller-owned claim receipt、heartbeat worker、result/artifact acceptance UoW、terminal projection、
crash/kill/restart matrix、可信 tenant scope 和 service lifecycle 完成前：

- 不得把 `append_invocation_admission` 描述为 exactly-once Agent execution；
- 不得启用真实飞书、企微或其他不可逆 connector；
- 不得用本地测试或 receipt row 代替生产批准；
- Gate A、B、C、D、E 保持全部关闭。
