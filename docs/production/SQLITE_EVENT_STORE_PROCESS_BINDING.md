# SQLiteEventStore 进程绑定、worker 拓扑与回滚手册

状态：**实现候选已完成本地三版本门禁；不是 Gate A–E 关闭声明，也不是 SQLite connection 可继承声明。**

本文记录 `SQLiteEventStore` 从接入前审计到 process-bound 实现的运行合同。设计输入见
[`15_event_store_process_boundary_audit.md`](../../analysis_report/research/15_event_store_process_boundary_audit.md)，
共享身份基础见 [`PROCESS_INHERITANCE.md`](./PROCESS_INHERITANCE.md)。

## 1. 唯一安全结论

- 已构造的 `SQLiteEventStore`、其 `sqlite3.Connection`、`RLock`、transaction context、stream
  context/iterator 和 clock 都只属于 constructor 捕获的 exact PID + opaque epoch。
- fork child 对 inherited instance 的 read、write、close、enter、exit、stream enter 和每次 iterator
  resume 都得到 exact `EventStoreLifecycleError("event_store_process_mismatch")`。
- child mismatch 不检查 transaction state，不执行 rollback/commit/close，不释放 inherited `RLock`，
  也不依赖 connection finalizer。
- 多进程共享同一 SQLite 文件只允许各进程在 worker 创建完成后 fresh construct store。fresh
  connections 仍由 WAL、`BEGIN IMMEDIATE` 和 durable CAS 协调。
- guard 不是 preload/fork live graph 的许可，也不能擦除 fork 前已复制到 child 的 secret/provider
  state。

## 2. 入口覆盖

统一 public wrapper 在读取 caller argument、lock、connection、clock 或 lifecycle state 前检查 owner。
下列普通入口均在同一边界内：

| 类别 | 入口 |
|---|---|
| event read | `stream_version`、`get_idempotent_event`、`read_stream`、`read_stream_page`、`read_all` |
| event write | `append`、`append_with_outbox`、`append_inbox`、`append_many` |
| outbox mutation | `claim_outbox`、`acknowledge_outbox`、`reject_outbox`、`mark_outbox_ambiguous`、`resolve_outbox_ambiguity` |
| outbox/inbox read | `read_outbox_ambiguities`、`read_outbox_ambiguities_page`、`get_outbox`、`read_outbox`、`read_outbox_page`、`get_inbox_receipt` |
| snapshot | `save_snapshot`、`load_snapshot` |
| lifecycle | `close`、`__enter__`、`__exit__` |

`stream_all_page` 不是普通函数边界：method call、context `__enter__`、iterator `__iter__`、每次
`__next__` 和 context `__exit__` 分别重新验证。parent yield 第一行后 fork，child resume 会拒绝；
parent copy 继续得到剩余 exact positions，无 gap 或 duplicate。

live store、transaction context、stream context 和 iterator 均拒绝 copy、deepcopy 和 pickle。这只
阻止显式传输；fork 拒绝仍由 owner guard 完成。

## 3. exact SQL snapshot

DB-API parameter adaptation 能在 `Connection.execute()` 内调用 caller 对象的 `__conform__` 或全局
adapter。实现不把 raw `DomainEvent`、`OutboxMessage`、mapping、string/int subclass、status-like
object、timestamp、lease token 或 iterable element 传入 SQL。

写路径在 lock/connection 外完成：

1. entry guard；
2. iterable materialization；
3. materialization 后 guard；
4. exact `DomainEvent` / `OutboxMessage` field extraction；
5. bounded JSON deep copy 和 canonical encoding；
6. exact built-in `str | int | float | bool | None` snapshot；
7. final guard；
8. owner-aware lock/transaction 和 SQL。

read path 的 ID、cursor、limit、bool 和 enum value 也先收敛为 exact built-in。hostile string/int
subclass 在 lock 前以 `TypeError` 拒绝，adapter canary 调用计数保持零。`load_snapshot` 在锁内只
复制 SQLite row scalar，JSON decode 在锁外进行。

受信任 host 仍不得注册未经审计的 process-global SQLite adapter；本实现阻止 caller object 进入
bind tuple，不宣称验证了进程全局 mutable adapter registry。

## 4. transaction、clock、migration 与 constructor

### 4.1 owner-aware transaction

`_transaction` 不使用无条件 `with self._lock` unwind。它显式执行：

1. pre-lock guard；
2. acquire；
3. post-acquire guard；
4. `BEGIN IMMEDIATE`；
5. post-BEGIN guard；
6. body；
7. pre-COMMIT guard；
8. COMMIT；
9. post-COMMIT guard；
10. 仅 current owner 可 inspect/rollback/release。

clock 在 open transaction 中调用，但 `_now()` 在 callback 前后各 guard，并在 timestamp normalize
后再次 guard。clock 内真实 fork 时，child 在第一次后续 SQL 前拒绝，parent commit exactly once。

current-process 的普通 body/driver error 保持 rollback 语义。若 originating exception 是 exact
`KeyboardInterrupt`、`SystemExit`、`GeneratorExit` 或 `asyncio.CancelledError`，rollback/close 的
cleanup control 不得覆盖 originating control。process mismatch 同样不能被 SQLite/rollback error
改写。

### 4.2 migration 与 constructor

owner 在 path、clock、directory、connection、lock 和 migration 之前捕获。migration runner 接受
store-owned private guard，在 migration text/clock/SQL/validation/commit 边界之间检查，并在任何
exception cleanup 读取 `connection.in_transaction` 前再次检查。

constructor migration clock 内 fork 时：

- parent 独自完成 migration decision；
- child 不 rollback、不 close、不 release inherited lock；
- partially initialized connection 放入 child-only module-private quarantine，保持到安全
  `_exit`/exec，避免 GC/finalizer；
- public constructor error 使用同一 fixed lifecycle code。

quarantine 不是 child 可继续服务的状态。捕获该 error 的 worker 必须进入 non-ready 并退出/exec；
不得从 private quarantine 取回 wrapper。

## 5. clean error graph

foundation 只创建 module-private `_EventStoreProcessMismatchSignal`。public wrapper 只接受同时满足：

- exact private type；
- identity-only construction token；
- traceback 尾部精确来自 store guard、`require_current_process` 和 foundation raise helper。

受信号后先 detach cause/context/traceback，并清理完成的 internal frames；退出 `except`、删除
store/caller locals 后才进入 module-level trampoline。trampoline 新建 exact
`EventStoreLifecycleError`，显式把该 fresh error 的 `__context__` 设为 `None`，再 bare re-raise。

公开错误不含 PID、epoch、path、SQL、stream/message ID、lease、provider 或 caller object；args
只有 `("event_store_process_mismatch",)`，cause/context/notes 为空。普通 provider/business/control
exception 不经过该清洗器，也不被改写或 detach。

## 6. 正向多进程证明

retained matrix 分开证明两件事：

1. inherited instance 在 fork child 必须拒绝；
2. fork/spawn/forkserver 完成后创建的 fresh instance 可共同使用同一文件。

fresh matrix 覆盖：

- 相同 expected global position：exactly one stored、one `ConcurrencyError`；
- 相同 stream-local idempotency + outbox：双方返回同一 global position 和同一 durable outbox row；
- 同一 pending message lease：exactly one claimant，attempt count 只增加一次；
- 同一 open ambiguity resolution：exactly one winner，最终为 `retry | dead_letter` 中一个；
- 最终 `integrity_check=ok`、foreign-key check 为空、migration schema version 精确。

macOS Xcode Python 3.9 的 bundled SQLite 在“当前进程曾创建 SQLite connection 后再 fork、child
重新 connect”会发生解释器外原生段错误；这是通过独立最小 raw-`sqlite3` reproduction 观察到的
运行时缺陷，不是可捕获 Python exception。fork 正向测试因此使用 clean spawned supervisor，并在
supervisor/child 任一方创建 connection **之前**真实 fork，随后双方 fresh construct。该结构也正是
生产必须采用的 fork-before-initialization 拓扑；不能以测试技巧允许 fork-after-connect。

## 7. 部署迁移

1. inventory Gunicorn/uWSGI preload、`multiprocessing` 默认 start method、自建 fork、fixture 和
   worker pool；
2. worker/process topology 在 connection、secret、issuer、clock provider、event loop 和 connector
   初始化前冻结；
3. 每个最终 worker 内 fresh construct `SQLiteEventStore`；
4. 运行 inherited rejection 和 fresh contention 两组 synthetic matrix；
5. mismatch metric 只记录 fixed code，不记录 path/PID/IDs；
6. 合法流量出现 mismatch 时停止 admission，修复 composition topology，不捕获后继续 SQL；
7. internal pilot 前仍需完成其他 store/provider/runtime 的独立 process-bound 迁移。

本变化没有 schema、event、backup manifest 或 wire-format migration；它会把过去未定义的
fork-inherited 使用收紧为稳定拒绝。

## 8. 回滚

- binary 可回到上一版本，同一数据库无需 down migration；
- 保留 fork-before-init worker topology，不把删除 guard 当作回滚；
- open transaction 只由 original owner 决定 commit/rollback；
- constructor/migration mismatch 的 child 只允许 non-ready exit/exec；
- 若 fresh worker 构造失败，停止 admission，禁止复用 parent store/connection。

## 9. 当前仍不可声明

- SQLite connection、RLock、clock 或 iterator 可安全继承；
- artifact/projection/revocation/invocation/authorization/secret/runtime/connector 已全部 process-bound；
- secret-before-fork 复制风险已解决；
- Linux production runner、容量、SLO、RPO/RTO 或 HA 已验证；
- Gate A、B、C、D、E 任一关闭。

因此该候选只关闭 `SQLiteEventStore` 自身的接入前 P0；系统级 process-inheritance P0 仍保持打开。
