# SQLiteEventStore process-bound 实现与验证报告

## 0. 结论

在独立 branch `codex/event-store-process-binding-v1`、base
`967b4364c36e84c2c54c51528ab717da615222ac` 上，`SQLiteEventStore` 已按
[`15_event_store_process_boundary_audit.md`](./15_event_store_process_boundary_audit.md) 完成实现候选：

- constructor 在任何 path/provider/connection/lock/migration 资源前捕获 exact process owner；
- 所有普通 public、lifecycle 和 deferred stream 入口均在 inherited dependency 前拒绝；
- caller inputs、iterables、event/message/JSON/timestamp/cursor/lease 参数在 lock 外转换为 exact
  internal snapshot，hostile SQLite adapter 调用数为零；
- transaction、clock、migration、constructor cleanup 和完整 live graph quarantine 均 owner-aware；
- transaction context 的 enter/exit 以及 inherited store 未经 public call 直接 GC 都重新验证 owner；
- mismatch 使用 provenance-checked exact private signal 和 fresh public-error trampoline；
- nested trusted public mismatch 会重新清洗，caller forged 同名错误不会获得 trampoline 权限；
- exact process-control signal 在 transaction、migration、constructor 与 context cleanup 前保持优先；
- fork child 不 inspect/rollback/commit/close/release inherited resources，parent 连续运行；
- fork-before-init、spawn、forkserver fresh connections 已覆盖四类 contention/CAS。

本结论尚未经过另一名未参与实现者的完整独立复核，也不是 release/promotion 决定。系统 Gate A–E
保持关闭，整体仍为 **NO-GO**。

## 1. 线性提交

| 顺序 | commit | 独立变化 |
|---:|---|---|
| 1 | `82c7760` | public/lifecycle pre-input process binding、专用 lifecycle error、private signal、clean trampoline、owner capture |
| 2 | `e12df24` | migration runner 的 pre-resource/process-aware cleanup hook |
| 3 | `f970807` | explicit owner-aware lock/transaction、clock guards、constructor migration quarantine |
| 4 | `21b80fc` | read-side exact SQL parameter snapshot 与锁外 snapshot decode |
| 5 | `3b5c470` | event/outbox/inbox/snapshot write snapshot 与 iterable post-materialization guard |
| 6 | `552774d` | outbox claim/ACK/NACK/ambiguity exact snapshots 与 provider/new-ID guards |
| 7 | `ae08d9c` | stream call/enter/iter/每次 resume/exit guards；store/context/iterator non-transferable |
| 8 | `0a68cac` | originating exact control 优先、active-exception clean graph、fork-while-parent-transaction |
| 9 | `2667eba` | fork-before-init/spawn/forkserver fresh contention/CAS matrix |
| 10 | `13dd687` | transaction context non-copy/deepcopy/pickle wrapper |
| 11 | `f7d3e8a` | 首次 mismatch 与普通 GC 均 identity-only 隔离 inherited store 完整依赖图；transaction context 重新检查 enter/exit |
| 12 | `b0cb6ed` | exact control 的 exit 优先级、可信 nested public mismatch 重清洗、fresh outer store rollback/continuity |
| 13 | `fb29fc7` | migration/constructor cleanup 保留 originating exact control，并隔离 partial constructor graph |
| 14 | `a17fc31` | ambiguity contention loser 改用 closed-row CAS，消除 probe 自身的 resolve/read 竞态 |

每笔提交前均保持默认树可运行，并执行三版本全仓 unittest、locked Ruff lint/format、strict mypy 和
dependency-lock verifier。未 reset、rebase、force-push、push 或操作 canonical/auth/backup worktree。

## 2. 实现边界

### 2.1 process identity 与 public error

`EventStoreLifecycleError.code` 和唯一 args 均为：

```text
event_store_process_mismatch
```

错误不包含 PID、epoch、path、SQL、stream/message/lease、caller/provider 或 connection。private
signal 必须同时满足 exact type、identity token 和 exact foundation tail-frame provenance；伪造、子类化
或嫁接的异常不会被翻译成可信 public mismatch。

public wrapper 在退出 signal handler、清理 internal traceback frames 并删除 store/args/kwargs 后才
调用 trampoline。普通调用、active outer `except` 和 context `__exit__` 的 public error 均满足
cause/context/notes 为空。nested public mismatch 只有在 exact type、fixed fields 和 traceback 最末
raise point 同时证明它来自可信 trampoline 时才重新清洗；第三方 provider/business/control exception
以及 caller 构造或 graft 的同名 lifecycle error 不由该 trampoline 修改。

### 2.2 26 个普通/生命周期入口与 deferred 边界

审计表中的 event/outbox/inbox/snapshot read/write 与 `close/__enter__/__exit__` 全部由统一 wrapper
保护。`stream_all_page` 另有四层：

```text
method call
  -> context enter
    -> iterator iter/next (每次 resume)
  -> context exit
```

parent 读取 position 1 后 fork，child 的 `iter`/`next`/exit 都在 2 秒内拒绝；parent 继续得到 2、3
且随后 exact `StopIteration`。context/iterator 不跨 yield 持锁。

`_transaction()` 返回的 wrapper 同样不可 copy/deepcopy/pickle。未 enter 的 context 在 child enter
前拒绝；parent 已 enter 的 context 在 child exit 前拒绝并保持 parent transaction 未改变。child
不会 resume inherited contextmanager generator 的 cleanup。

### 2.3 SQL adaptation boundary

写路径只把 sanitized `DomainEvent`、`OutboxMessage` 和 canonical JSON 的 exact scalar fields 用作 SQL
参数。raw iterable 先 materialize，立即 guard，再读取元素字段；因此 iterable 内 fork 的 child 在
`BEGIN` 前拒绝。read 路径的 string/int/bool/enum/cursor 也 exact-check 后才拿锁。

测试使用实现 `__conform__(sqlite3.PrepareProtocol)` 的 hostile string/int subclass，覆盖 read、event、
outbox、inbox、snapshot、claim、ACK/NACK 和 ambiguity 入口；全部在 connection 前 `TypeError`，
adapter canary 为零，transaction 未打开。

### 2.4 owner-aware cleanup

`_transaction_inner` 显式 acquire/release，不依赖无条件 `with RLock` unwind。每个 BEGIN/COMMIT/clock
缝隙均重新 guard。mismatch 分支不读取 `connection.in_transaction`，不 rollback/commit/close，也不
release inherited lock。

migration runner 在 clock 返回后和 exception cleanup 前执行同一 store guard。constructor mismatch
把 partially initialized store、connection、lock、clock/provider 的完整可达 graph 放进 child-only
quarantine，避免 close 和 GC/finalizer；普通 public mismatch 会隔离 store root，stream/transaction
context mismatch 会隔离相应 suspended context root。quarantine 只做 object identity 去重，不遍历
或调用 inherited object。

body/commit/initialization 的 exact `KeyboardInterrupt`、`SystemExit`、`GeneratorExit`、
`CancelledError` 优先于 cleanup control；store/stream/transaction context 的 inherited exit 也返回
“不抑制”而不是把 exact control 改写为 lifecycle error。普通 current-process exception 仍按原合同
rollback。

### 2.5 独立 GC 探针发现并关闭的 P0

原实现候选虽然能在 child 的 public 入口稳定拒绝，却只隔离 constructor 的 partial connection。独立
探针追加 `del store` 与 `gc.collect()` 后复现了更深的 native lifetime 缺陷：

```text
完整 inherited store
→ child public 入口拒绝
→ del store + gc.collect()
→ sqlite3.Connection finalizer 运行
→ child status=11 / SIGSEGV
```

这证明“拒绝调用”不等于“继承资源不会被解释器销毁”。修复增加两条互补路径：首次 trusted
mismatch 强引用隔离完整 store/context graph；即使 child 从未调用 public 入口，store 的 owner-aware
`__del__` 也会复活并隔离 graph。修复后的 retained probe 同时要求：

```text
mismatch_then_gc: status=0, rejected:alive
gc_without_call: status=0, alive
```

该隔离只保证 graph 保持存活到安全进程替换点，不把 child 恢复为 ready。普通 `sys.exit` 或解释器
teardown 可能先清空 module globals，因此生产 mismatch worker 必须停止 admission 并直接
`os._exit`/exec。

## 3. retained fork 反例

本地真实 fork 覆盖：

- 所有普通/lifecycle public calls；
- inherited close/enter/exit；
- parent 另一线程持 open transaction/RLock；
- `BEGIN IMMEDIATE` 后 clock callback 内 fork；
- event iterable materialization 内 fork；
- constructor migration clock 内 fork；
- inherited store 在 child public mismatch 后 GC，以及未调用 public 入口直接 GC；
- transaction context 未 enter 时 child enter、parent enter 后 child exit；
- context call 后 enter、enter 后 first next、yield 后 next、active-exception exit；
- store/stream/transaction inherited exit 对四类 exact control 的不抑制语义；
- nested trusted public mismatch、fresh outer store rollback/continuity 和 forged lifecycle error；
- child lifecycle error 后 parent append/read/iterator/transaction/lease 连续。

child 结果通过 pipe 只发送固定 outcome，不 pickle live store、owner、connection 或 lease token。

## 4. fresh connection 正向矩阵

每个 start method 都只传 path、mode 和 process pipe，worker 内 fresh construct。覆盖：

| mode | durable 断言 |
|---|---|
| global position CAS | 相同 expected position：一个 stored、一个 `ConcurrencyError`，总行数只增 1 |
| idempotency + outbox | 两端返回 global position 1、一个 outbox；durable event/outbox 各 1 行 |
| lease contention | 一个 claimant、一个 empty；status `in_flight`、attempt count 1 |
| ambiguity CAS | `retry`/`dead_letter` 只有一个 resolution winner，另一端 false |

每个 mode 最终要求 `PRAGMA integrity_check = ok`、`foreign_key_check` 为空和 migration schema version
精确为 3。

macOS Xcode Python 3.9 的 bundled SQLite 在“当前进程曾 connect 后再 fork、child 再 connect”存在
可独立复现的原生段错误。测试不隐藏它，也不把它误判为业务失败：fork 矩阵由 clean spawned
supervisor 启动，supervisor 在自身或 child 任一方 connect 前真实 fork，然后两端 fresh construct。
这同时强制验证生产所需的 fork-before-initialization topology。spawn/forkserver 直接 fresh construct。

## 5. 当前可复现门禁

最近组合 checkpoint 的观测值：

| Gate | Python 3.9 | Python 3.12.12 | Python 3.13 |
|---|---:|---:|---:|
| full unittest `-W error` | 928，skip 1 | 928 | 928 |
| focused process-binding suite | pass | pass | pass |

共同静态门禁：

```text
ruff check src tests scripts examples
ruff format --check src tests scripts examples
PYTHONPATH=src mypy --strict --python-version 3.9 src/quantum_entanglement
python3 scripts/verify_dependency_locks.py --repository-root .
```

观测结果：Ruff pass、94 files already formatted、strict mypy 35 source files pass、dependency locks
4 targets / 74 records。ambiguity contention 稳定性复跑为 Python 3.9 八轮全过，Python 3.12.12 与
3.13 各四轮全过。完整 compileall、demo、secret count、external release evidence 和 clean final HEAD
仍应在文档提交完成后重新运行并记录，不能用本节的中间 checkpoint 代替最终 evidence。

## 6. 迁移、回滚与兼容

行为变化：过去未定义的 inherited-store 使用现在稳定失败。没有 schema/event/backup/wire migration。

部署必须：fork/spawn/forkserver topology 完成后，在最终 worker 内 fresh compose store/clock/provider；
forkserver 必须在任何 stateful dependency 和 secret load 前启动。合法请求命中 mismatch 时停止
admission，以 `os._exit`/exec 替换 worker 并修 topology，不捕获后继续 SQL，也不依赖普通解释器
teardown 清理 inherited SQLite graph。

binary 可回滚，但 worker topology 不回滚。禁止删除 guard、复用 inherited connection 或让 child
参与 parent open transaction recovery。

## 7. 残余风险与 NO-GO

- 尚未完成 artifact/projection/revocation 等其他 store 的同等级全入口矩阵；
- authorization、request context、secret/key registry、runtime、connector 和 composition root 尚未
  全部 process-bound；
- guard 不能清除 fork 前复制的 secret/provider bytes；
- process-global SQLite adapter registry 属于 trusted-host mutable state，未由本 helper 验证；
- 尚无独立 reviewer、Linux production runner、chaos/soak/capacity/SLO/RPO/RTO evidence；
- service auth、tenant scope、attempt/result/action receipt 和 connector effect reconciliation P0 仍在。

所以本实现只把 `SQLiteEventStore` 从“明确未接入”推进到“待独立复核的 process-bound 候选”。
Gate A–E 不变，禁止真实飞书/企微发送、真实客户数据、公网监听和不可逆 connector。
