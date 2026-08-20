# SQLiteEventStore 进程继承与全公开路径接入审计

## 0. 结论

`SQLiteEventStore` 不能通过“constructor 存一个 PID，然后在几个常用方法开头判断”安全完成
process-bound 迁移。当前类有 26 个公开/生命周期入口、一个可重入 transaction context manager、
一个跨 `yield` 的流式 iterator、一个 store-owned clock callback，以及多个会在拿锁前或 transaction
中执行 caller/provider 代码的路径。

共享 `process_identity` foundation 已能判断 exact PID + opaque epoch，但本报告审计时
`SQLiteEventStore` **尚未接入**。真实 fork child 仍可能先等待 inherited `RLock`、触碰 inherited
`sqlite3.Connection`、检查/回滚 parent transaction 或调用 inherited clock。当前生产 P0 保持打开。

安全接入至少需要同时完成：

1. 每个普通 public entry 的统一 pre-input/pre-lock guard；
2. iterable/callback 执行后的二次 guard；
3. `_transaction` 在 BEGIN、exception cleanup、COMMIT 前的 current-owner 检查；
4. `stream_all_page` 在 context enter 和每次 iterator resume 时重新检查；
5. mismatch 的 clean rethrow，最终异常 traceback locals 不可达 store/connection/lock/caller；
6. child 拒绝后 parent transaction、connection、lock、lease 和 iterator 全部连续可用；
7. 同一数据库文件的 fresh child connection contention/CAS 证明；
8. worker 拓扑仍坚持 fork/spawn 完成后再构造 connection，guard 不是继承 connection 的许可。

本报告只冻结设计与测试边界，不修改 store 行为，也不关闭任何 Gate。

## 1. 审计范围与证据

读取范围：

- `src/quantum_entanglement/store.py`；
- `events.py`、`delivery.py` 中的 caller value 类型；
- `migrations/__init__.py` 的 initialization callback/transaction 路径；
- event store、transaction、JSON、delivery、migration、publisher 与 recovery 现有测试；
- [`PROCESS_INHERITANCE.md`](../../docs/production/PROCESS_INHERITANCE.md)；
- [`12_process_inheritance_dependency_audit.md`](./12_process_inheritance_dependency_audit.md)。

审计口径：

- “方法入口有 guard”不等于方法运行期间永远安全；显式 iterable/provider callback 可以在同一
  调用栈内 fork，child 会从 callback 返回点继续；
- “child 的一次 SELECT 成功”不等于 SQLite connection 可继承；
- “错误文字不含 path/PID”不等于错误图安全；traceback frame locals 也必须审计；
- “parent/child 都没有报错”不是正确性；child 必须拒绝 inherited instance；
- `fork()` 发生在另一个 parent 线程时，child 只保留调用 fork 的线程；原 store-call 线程不会在
  child 继续，但它持有的 RLock 状态会被复制；
- signal handler 或任意指令间异步 fork 不属于 helper 可证明边界，部署合同必须禁止
  fork-after-initialization；代码仍要覆盖明确的 callback/iterator/open-transaction fork 点。

## 2. 当前资源与依赖图

```text
SQLiteEventStore
├── path
├── sqlite3.Connection(check_same_thread=False, autocommit)
│   ├── WAL / foreign_keys
│   ├── core events/snapshots/inbox/outbox tables
│   ├── migration ledger + migration-owned tables
│   └── registered qe_sha256 SQLite UDF
├── threading.RLock
├── clock: Callable[[], str]
├── JSON size policy
├── transaction generator
└── stream_all_page iterator closures
```

fork 后的主要分叉：

- connection 的 Python wrapper、SQLite library state、file descriptors 和 transaction flag 被复制；
- RLock 可能记录一个 child 中已不存在的 owner thread；
- clock 可能是带锁、状态或 secret/provider reference 的 inherited callable；
- iterator closure 复制 `self`、cursor、position 和 page limit；
- outbox lease/ambiguity decision 可在 parent/child 各自产生冲突；
- migration constructor failure path可能在 child 错误关闭/回滚 parent-derived connection state。

## 3. 公开路径清单

以下 26 个入口都需要明确测试。表中的“当前首次危险触点”是未接入前的现状。

| 入口 | 当前首次危险触点 | 特殊 fork 缝隙 | 必需接入 |
|---|---|---|---|
| `stream_version` | 直接拿 `_lock` | fork while other thread holds lock | outer clean guard + pre-lock guard |
| `get_idempotent_event` | input `.strip()` 后拿锁 | hostile string subclass；当前未 exact type 时可先执行 caller code | guard 必须早于 input access |
| `append` | cursor validation 后 `_transaction` | transaction 内 event payload/fields | outer guard + transaction guards |
| `append_with_outbox` | `tuple(messages)` | iterable 可 fork；transaction 内 payload/property reads | entry guard、tuple 后 guard、exact normalized batch |
| `append_inbox` | caller `.strip()` | `result` truthiness/dict copy、`utc_now` 和 event encode 在 transaction 中 | pre-input guard、pre-normalize、transaction guards |
| `append_many` | `tuple(events)` | iterable 可 fork；batch fields 在 transaction 中 | entry/tuple 后 guard、exact normalized batch |
| `read_stream` | 直接拿锁 | unvalidated caller value | outer clean guard + pre-lock guard |
| `read_stream_page` | input validation 后拿锁 | validation 当前可先运行 | entry guard + pre-lock guard |
| `read_all` | 构造/进入 stream context | context manager body是 deferred execution | entry guard；依赖 stream 自身 resume guard |
| `stream_all_page` call | 只返回 context manager | call-time guard 会在返回后失效 | 不能只装普通 method decorator |
| `stream_all_page.__enter__` | cursor validation | fork between call and enter | enter-time guard |
| returned event iterator `next()` | 拿锁并 query | fork after prior yield；child resume inherited closure | 每次 resume、每次拿锁前 guard |
| `claim_outbox` | caller `.strip()` 后 transaction | store clock callback、`new_id`、lease CAS | entry guard、clock 前后、commit 前 guard |
| `acknowledge_outbox` | transaction | store clock callback；timestamp normalize | entry/clock 后/commit 前 guard |
| `reject_outbox` | caller truthiness/status 后 transaction | store clock callback | entry/clock 后/commit 前 guard |
| `mark_outbox_ambiguous` | caller membership/digest/timestamp | trusted clock function可被替换；transaction mutation | entry guard、transaction pre-BEGIN/pre-COMMIT |
| `read_outbox_ambiguities` | 直接拿锁 | `open_only` 当前可用 truthiness | entry guard + exact bool validation |
| `read_outbox_ambiguities_page` | validation 后拿锁 | caller values先执行 | entry guard + pre-lock guard |
| `resolve_outbox_ambiguity` | caller validation/clock 后 transaction | digest iterable expression、time normalization | entry guard + before transaction guard |
| `get_outbox` | 直接拿锁 | unvalidated caller value | entry guard + pre-lock guard |
| `read_outbox` | 直接拿锁 | status property inside lock | entry guard；exact enum validation应在 lock 前 |
| `read_outbox_page` | validation 后拿锁 | status property inside SQL args | entry guard + pre-lock guard |
| `get_inbox_receipt` | 直接拿锁 | caller values未 exact normalize | entry guard + pre-lock guard |
| `save_snapshot` | transaction | JSON copy/encode 在 transaction 中 | entry guard、transaction 前 encode 或 execute 前再 guard |
| `load_snapshot` | 直接拿锁 | persisted decode 在 lock 内 | entry/pre-lock guard；decode 可在 copied row 上 lock 外完成 |
| `close` / `__enter__` / `__exit__` | lock/connection 或直接 return | child close 绝不能 best-effort；exit 不能静默 | 全部同一 stable mismatch + parent continuity |

`__init__` 不是 inherited-instance public call，但仍是 process topology boundary：owner 应在任何
connection/lock/provider 初始化之前 capture；constructor 中发生 epoch drift 时不能继续发布 store。

## 4. 不能遗漏的 deferred-execution 风险

### 4.1 Iterable 在 guard 之后 fork

危险的简单实现：

```text
guard(store.owner)
batch = tuple(caller_iterable)  # iterable 内 fork
with inherited_connection_transaction:
    ...
```

child 从 `tuple()` 返回后继续执行。若 `_transaction` 自身不重新 guard，child 会拿 inherited lock/
connection。正确边界至少是：

```text
entry guard
normalize/copy iterable
guard again
BEGIN
...
guard before COMMIT
```

如果 transaction 中仍访问 caller-controlled property/mapping，必须先 exact-normalize 为内部 primitive
snapshot，或在该访问之后、下一次 connection operation 之前重新 guard。

### 4.2 Clock callback 在 open transaction 中 fork

`claim_outbox`、ACK、NACK 有意在 `BEGIN IMMEDIATE` 后读取 store-owned clock，保证 lease CAS 使用
锁后时间。但任意 injected clock 可能带自己的锁或显式 fork：

```text
BEGIN IMMEDIATE
clock()  # parent/child 都从这里返回
UPDATE ...
```

所以 `_now()` 必须：

1. 调用 clock 前证明 current owner；
2. 获取返回值；
3. 再证明 current owner；
4. 才 normalize/返回。

若第二次证明在 child 失败，`_transaction` 的 exception path 必须先判断 process identity，不能读取
`connection.in_transaction`，更不能 ROLLBACK inherited connection。parent 仍独立持有自己的
transaction copy并继续原调用。

### 4.3 Transaction exception cleanup

当前 `_transaction` 对任意 `BaseException` 读取 `self._connection.in_transaction` 并可能 rollback。
迁移后需要区分：

```text
exception
├── owner current -> inspect transaction -> rollback -> rethrow original
└── owner mismatch -> do not inspect/rollback/close -> emit private mismatch signal
```

COMMIT 前也要重新证明 current owner。COMMIT 自身 driver error 的 current-process rollback 语义保持
现状；process mismatch 不能被包装成 SQLite/rollback error。

### 4.4 流式 context 与 iterator

`@contextmanager` 函数调用时不运行 body。只给函数最外层加普通 decorator，最多能证明“返回
context manager 的那一刻”，不能证明之后的 `__enter__`。

而 `stream_all_page` 的 event iterator 每行后 `yield`。合法调用可以：

1. parent enter context；
2. parent 读取第一行；
3. fork；
4. child 对 inherited iterator 调用 `next()`。

child 必须在拿 `_lock` 前拒绝；parent 必须能继续下一行且没有 cursor gap/duplicate。需要 enter-time
guard + iterator 每次 resume guard。只在创建 iterator 或读取第一页前检查一次不够。

## 5. Clean mismatch error graph

直接在 public method 中抛出 lifecycle error，即使 `args == ("event_store_process_mismatch",)`，
traceback frame locals 仍通常包含：

- `self → _connection/_lock/_clock/path`；
- caller 的 event/message/result/state/lease token；
- transaction connection、SQLite row/cursor；
- provider exception 或 iterator object。

因此推荐用两层机制：

1. foundation guard 只发出 module-private、不可由 caller 正常构造的 mismatch signal；
2. public wrapper 捕获该 signal，退出 `except`，清空 `self`、positional args、kwargs、result/
   connection/cursor/provider locals；
3. 在无 active exception context 的 module-level trampoline 中创建并抛出新的
   `EventStoreLifecycleError("event_store_process_mismatch") from None`；
4. wrapper 只捕获 exact private signal，不捕获 `KeyboardInterrupt`、`SystemExit`、
   `GeneratorExit`、cancellation 或业务异常；
5. signal 需有构造 token/nonce 和完整 traceback provenance 校验，防止 caller/provider 嫁接一段
   看似可信尾帧；
6. 对 context manager/iterator 另做同等 clean wrapper，因为普通 method wrapper 已经返回。

必须用对象图遍历测试，而不是只断言错误字符串：从 public exception 的 `args`、attrs、notes、
cause、context、traceback frame locals、closure、generator/frame 递归检查，不得找到 store、
connection、lock、clock/provider、caller object、lease token 或 sentinel secret。

这一点会使接入明显比“一行 decorator”更复杂，但它与授权分支已经发现的 lifecycle traceback
泄漏属于同一类 P0，不应在 event store 重复引入。

## 6. Constructor 与 dependency composition

建议 constructor 顺序：

1. capture process owner；
2. exact-validate path、max JSON bytes 和允许的 clock adapter 类型；
3. guard；
4. 创建目录；
5. guard；
6. 打开 connection；
7. 创建 lock；
8. initialization/migration 每个 external callback 后 guard；
9. guard 后才把 fully initialized state 发布给实例。

若 initialization 异常：

- owner current：按现有语义关闭本进程新建 connection；
- owner mismatch：不得关闭/rollback inherited connection，只丢弃 child 中未完成实例；
- control signal：保持类型/安全 exit code，不把它转换为正常成功或普通 lifecycle error。

一个 fresh child store 不能因为自己 owner 是 current 就自动信任 inherited stateful clock/provider。
生产 composition root 只能传进程内 fresh adapter或明确 fork-safe 的纯函数；test fake 必须单独标注，
不得由 helper 推导成 production-safe。

## 7. Stable lifecycle 合同

建议新增专用异常而不是复用 `ConcurrencyError` 或 `EventStoreIntegrityError`：

```text
EventStoreLifecycleError
└── code = "event_store_process_mismatch"
```

公开错误必须：

- `args` 只含固定 code；
- 不含 PID、epoch、path、SQL、stream/message/tenant/workspace/lease；
- `cause/context/notes` 为空；
- traceback locals 只含固定 primitive/空值和 module-level code；
- read/write/close/enter/exit/stream-resume 全部一致；
- parent 不因 child error 被 close、rollback 或 retire；
- 不把 mismatch 转换成 `False`/`None`/empty page，因为那会把 lifecycle violation 混成业务结果。

## 8. 必需测试矩阵

### 8.1 全入口 fork matrix

对上表每一项至少执行：

- parent 构造 populated file-backed store；
- fork child 调用 inherited path；
- child 在 2 秒门限内得到 exact lifecycle code；
- child exit 后 parent 对应 path 成功；
- 数据库 integrity、foreign keys、migration ledger、event/outbox/inbox/snapshot heads 不变；
- `close`、`enter`、`exit` 与 stream `next()` 单独覆盖，不能由一个代表方法替代。

### 8.2 Lock 与 transaction

- parent 第二线程持 `_lock` 时 fork，child 任意 read/write/close 不等待锁；
- parent `BEGIN IMMEDIATE` 后由另一线程 fork，child 拒绝且不 inspect/rollback connection；
- clock callback 内 fork，child 在 callback 后、第一次 SQL 前拒绝；parent commit exactly once；
- input iterable 内 fork，child 在 materialization 后、BEGIN 前拒绝；
- transaction body business exception在 current process 仍 rollback 并保留原类型；
- control signal与 cancellation 不被 mismatch wrapper 吞掉或改成成功退出。

### 8.3 Stream

- fork before context enter；
- enter 后、第一次 `next()` 前 fork；
- 第一行 yield 后 fork；
- child `next()`/context exit 均有界拒绝；
- parent 继续读取 exact remaining positions，无 gap/duplicate；
- parent close 后 iterator 的原有 closed-store语义另测，不能与 process mismatch 混淆。

### 8.4 Fresh multi-process connection

这组正向测试要证明支持的是“各进程 fresh connection”：

- fork/spawn/forkserver 完成后在 child 打开同一临时数据库；
- parent/child 使用不同 event/idempotency key 并发 append；
- 相同 expected global position 只有一个 winner，另一方得到 `ConcurrencyError`；
- 相同 idempotency admission 返回同一 durable row，不重复 outbox；
- independent outbox lease token/fencing 与 ambiguity CAS 保持现有合同；
- 两边关闭自己的 connection，最终 `PRAGMA integrity_check`、FK、migration/schema validation通过。

正向 fresh-connection 测试不能与 inherited-instance拒绝测试混写，否则一次成功查询容易被误读成
“connection 已 fork-safe”。

### 8.5 Error graph 与序列化

- public mismatch exception 全图扫描；
- hostile error/caller/provider对象带 `__repr__`、`__eq__`、`__bool__`、notes、traceback graft；
- store、transaction context、stream context/iterator copy/deepcopy/pickle policy明确；
- spawn/forkserver 不可隐式传 live store；只传 path/config 后 fresh construct。

### 8.6 Python/平台

- Python 3.9、3.12、3.13；
- macOS 本地真实 fork retained；
- Linux CI 真实 fork、spawn、forkserver retained；
- 无 `os.fork` 平台明确 skip fork反例，但仍跑 spawn和serialization；
- Python 3.12+ 多线程 fork warning是测试有意覆盖风险，不得隐藏成普通无警告生产模式。

## 9. 建议提交序列

保持每笔默认分支可运行，不用一个大提交跨越全部边界：

1. `test`：冻结当前 inherited read/write/close/stream反例与 parent continuity；
2. `feat`：专用 lifecycle error、private signal、clean ordinary-method wrapper；
3. `feat`：constructor owner capture 与 initialization failure ownership；
4. `feat`：普通 read path pre-lock guards；
5. `feat`：普通 write path与 `_transaction` BEGIN/rollback/commit guards；
6. `feat`：clock callback前后 guard和 open-transaction fork反例；
7. `feat`：iterable/input normalization后的 guard与 exact internal snapshot；
8. `feat`：stream context-enter和 iterator-resume clean guards；
9. `test`：完整 public lifecycle error-graph、control-signal、nested fork matrix；
10. `test`：fresh file connection多进程 contention/CAS matrix；
11. `docs`：event store worker topology、迁移和回滚 runbook；
12. `docs`：CURRENT_READINESS/THREAT_MODEL/CHANGELOG；
13. `evidence`：3.9/3.12/3.13 + macOS/Linux retained results。

测试先行提交若会让默认分支红，必须与最小实现放在同一提交；不得提交故意失败的正式历史。
每一笔行为提交都重新跑全量 unit、Ruff、strict mypy、compileall、dependency locks和 demo。

## 10. 部署迁移与回滚

该接入没有 schema 变化，但属于运行行为收紧：过去依赖 preload + fork 的部署会开始收到
`event_store_process_mismatch`。

迁移：

1. inventory Gunicorn/uWSGI preload、multiprocessing start method、自建 fork和 test fixture；
2. 在 connection/clock/provider/secret 初始化之前创建 worker；
3. 每个 worker内 fresh构造 store；
4. synthetic database跑 inherited拒绝和 fresh contention两组 matrix；
5. shadow观察 mismatch计数，任何合法流量命中都先修 worker topology；
6. 再晋级到内部 pilot，仍禁止真实飞书/企微发送和不可逆 connector。

回滚：

- 可以回滚到上一 binary，因为没有 schema/event/backup manifest迁移；
- 保留 fork-before-init worker topology；
- 不允许用删除 guard、允许 inherited connection 或捕获 mismatch 后继续 SQL 来“恢复”；
- 若 fresh worker构造失败，停止 admission并修复 composition，不复用 parent store；
- open transaction只由 original parent owner决定 commit/rollback，child不参与恢复。

## 11. 当前可声明与不可声明

可声明：

- 已完成 `SQLiteEventStore` 全公开路径、deferred iterator、transaction callback与 clean error graph
  的接入前审计；
- 已冻结可分阶段实现和验证的 migration plan。

不可声明：

- `SQLiteEventStore` 已 process-bound；
- SQLite connection 可在 fork 后继续使用；
- event/outbox/inbox/snapshot 已多进程生产安全；
- secret/provider复制风险已解决；
- Gate A–E 任一关闭；
- 已有 Linux production fork证据。

下一步只能按上述序列实现并独立复核；报告本身不是安全控制。
