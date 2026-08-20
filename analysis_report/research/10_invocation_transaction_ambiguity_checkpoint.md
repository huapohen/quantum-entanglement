# Durable invocation transaction ambiguity checkpoint

更新日期：2026-08-20（Asia/Shanghai）

代码提交链：

| 阶段 | Commit | Tree | 可运行边界 |
|---|---|---|---|
| 最终事务行为与必要回归 | `554115cac9824eda8e0ba3932cf7b94b64d29f91` | `7b0ed58e764f40951b892a9701766f55ad37aea7` | 精确 lost-ACK readback、七个写 API 的异常脱敏、控制信号重签发和 quarantine；直接父提交为安全基线 `086b1e3` |
| 独立黑盒故障矩阵 | `4f7f040296ba0d513e7f48ae3a00d7fdf3925965` | `09f07f3de2dc3475d7e3dd7154e653e6ed18beb3` | 状态探测失败和伪造内部 sentinel 防回归 |
| POSIX fork 进程绑定 | `dcc274bc1b081ee9098ebcdae911bbfb795ae34a` | `970485765fe87361728dd197fc33d67084bb630c` | fork 继承的 store 在触碰锁/连接前 fail closed |

状态：单节点 SQLite attempt store 的 transaction acknowledgement、rollback ambiguity、
异常边界与 fork 误用已经形成逐阶段可运行提交。`OrchestratorKernel`、外部 effect receipt 和真实
connector 仍未接成端到端可恢复状态机，因此本 checkpoint 不是生产商用、distributed-safe 或
exactly-once 声明。

## 1. 本阶段关闭的问题

SQLite 的 `COMMIT` 可能已经 durable，但驱动、包装器、trace hook 或控制信号在返回 ACK 时抛错。
如果上层把这个错误直接当作“事务失败”并重试，会出现三类风险：

1. 已 durable 的 enqueue/claim 被重复执行或错误报告；
2. 已 durable 的 heartbeat/complete/fail 被当作失败，导致 worker 与控制面分叉；
3. rollback、transaction-state inspection 或 readback 自身失败后，半开连接继续被业务复用，
   后续出现 raw SQLite error、持锁或不确定状态。

实现现在按可证明结果处理 mutation：

| 观察结果 | 公共行为 | 自动重试结论 |
|---|---|---|
| BEGIN/body/pre-COMMIT 失败且 rollback 已确认 | 可信库内 validation/integrity 错误重建；普通错误固定为 `InvocationTransactionError`；exact control 清洗后重签发 | 数据库 mutation 未提交；是否重试 external effect 仍须由更上层 receipt 合同决定 |
| COMMIT 已结束且精确 durable readback 匹配 | 返回该次 mutation 的 durable 结果；若 ACK fault 是 exact control，则结果确认后清洗重签发该 control | 不需要重复 mutation |
| COMMIT/rollback/state/readback 无法唯一判定 | `InvocationCommitAmbiguityError`；必要时先 poison/close store | 禁止对该 invocation 盲目自动重试 |
| ambiguity 同时伴随 exact control | clean control 为顶层异常，direct cause 为无 traceback 的 `InvocationCommitAmbiguityError`；store 仍先 quarantine | 必须保留控制流，同时按 ambiguity 运维处置 |

## 2. 精确 readback 绑定

每个 public mutation 在事务内保存 candidate state，并只接受以下 durable 等价：

- `enqueue`：唯一 row 同时匹配 invocation/session/task/idempotency identity、plan、Agent、
  payload digest、priority、attempt budget 和 requested availability；
- `claim` / `claim_next`：job 与 current attempt 共同匹配生成的 attempt ID、完整
  session/plan/task/Agent/idempotency/payload binding、attempt number、max attempts、epoch、
  worker、claim time、token digest、heartbeat、job update time 和初始 deadline；
- `recover_expired`：只接受原 transaction 返回的 invocation 集合、同一 recovery time、
  EXPIRED attempt、固定 expiry reason，以及预期 QUEUED/FAILED job；
- `heartbeat`：job/attempt 仍由同一 lease 持有，且两者 heartbeat、deadline 与 job update
  time 精确等于 candidate；
- `complete`：job/attempt 都是 SUCCEEDED，result reference 和 finish/update time 精确一致；
- `fail`：attempt 是 FAILED，job 进入预期 QUEUED 或 FAILED，stored error、retry
  availability、finish/update time 精确一致。

并发连接若在 readback 前合法推进 heartbeat 或 deadline，当前调用得到 ambiguity，不会把旧 lease
误报为本次精确成功。这是有意接受的 safe false negative。claim 内只执行 expiry recovery、但没有
新 owner 时，会单独核验 recovery；真正无 mutation 的 claim/recovery/stale CAS 可以安全返回
`None`、empty 或 `False`，但 rollback 不确定时绝不走 no-op 快路径。

## 3. 公共异常与控制信号合同

新增或收紧的 public errors：

| Error | Stable code | 含义 |
|---|---|---|
| `InvocationTransactionError` | `invocation_transaction_failed` | write 失败且 rollback 已确认；不暴露原始 fault |
| `InvocationCommitAmbiguityError` | `invocation_commit_ambiguous` | mutation 无法唯一 reconcile |
| `InvocationStorePoisonedError` | `invocation_store_poisoned` | rollback/state/readback 未确认，旧 store 永久隔离 |
| `InvocationStoreClosedError` | `invocation_store_closed` | store 已关闭，或显式 close 未得到 ACK |
| `InvocationStoreProcessMismatchError` | `invocation_store_process_mismatch` | fork 子进程试图使用创建于其他 PID 的 store |

七个写 API（`enqueue`、`claim`、`claim_next`、`recover_expired`、`heartbeat`、`complete`、
`fail`）都有逐调用 nonce 绑定的公共边界，且不暴露 `__wrapped__` 绕过入口。raw provider/driver
error 仅存在于内部 frame，公共异常不保留其 identity、args、attrs、notes、cause/context 或原始
traceback。可信 validation/integrity 错误依赖启动时冻结的 code-object provenance；伪造模块名或
函数名不能冒充库内错误。

只把以下 exact type 当成控制流：

- `KeyboardInterrupt`；
- `GeneratorExit`；
- `asyncio.CancelledError`；
- `SystemExit`。

它们跨边界时会新建同类异常，不携带原对象图。`SystemExit` 只保留 `None`、exact `bool`、或
`0..255` 的 exact `int`；字符串、负数、超范围整数、整数子类及任意对象统一映射为 `1`。上述
控制异常的子类和 `BaseExceptionGroup` 不可信，降级为稳定 typed error。

显式 close 先固定 logical closed 状态。exact close control 清洗后重签发，direct cause 是无
traceback 的 `InvocationStoreClosedError`。context manager 已有 body exception/control 时，body
永远优先；close fault 不替换、不串联 body traceback。没有 body 时执行显式 close 合同。

生产 traceback logger 必须保持 `capture_locals=False`。异常图已经脱敏并不代表 frame locals
可以安全序列化；locals 仍可能包含调用参数、store、数据库路径、provider state 或 opaque lease
capability。不得把 raw error 重新放回 notes、日志、trace attribute 或用户响应。

## 4. Store quarantine 与 POSIX fork 边界

rollback 或 transaction-state inspection 失败时，poison flag 先于 best-effort close 设置。等待
同一锁的线程在拿到锁后只能观察 `InvocationStorePoisonedError`，不会继续操作已关闭 connection。
旧实例没有 unpoison 路径；file-backed DB 必须由新 store 做 migration/integrity/readback。

审计同时发现原实现只用 `spawn` 验证多进程竞争，没有 creator-PID binding。POSIX `fork()` 会
复制 `sqlite3.Connection`、`RLock`、thread-local control stack 和 nonce；子进程使用该对象会破坏
“每进程独立连接”前提。这是原公开多进程表述下的直接 P0，现已在 `dcc274b` 修复：

1. 构造 store 时记录 creator PID；
2. public read、write、recovery、context enter/exit 和 close 都先核验 PID；
3. mismatch 在触碰 inherited lock/connection/control state 前抛稳定 typed error；
4. 子进程丢弃 inherited reference，不调用它的 `close()`，而是在 fork/spawn 后构造新 store；
5. 父进程继续拥有并关闭自己的实例。

“支持多个本机 worker process”现在只表示它们可以打开同一个 file-backed WAL 数据库，每个
进程必须拥有独立 store/connection；绝不表示一个 Python store/connection 对象可以跨进程共享。

## 5. 直接测试与门禁证据

在代码 HEAD `dcc274b` 的等价 tree `9704857` 上复跑：

```text
Python 3.9.6:
  attempts 专项：Ran 109 tests ... OK (skipped=1)
  全套：Ran 760 tests ... OK (skipped=1)

Python 3.12.12:
  全套：Ran 760 tests ... OK

Python 3.13.9:
  attempts 专项 + -W error::ResourceWarning：Ran 109 tests ... OK
  全套 + -W error::ResourceWarning：Ran 760 tests ... OK
```

Python 3.13 全套仍打印两条来自既有 `tests/test_backup.py` / `backup.py` 路径的 unclosed SQLite
`ResourceWarning`；单独运行 42 项 backup tests 可稳定复现两条，进程仍为 0。本阶段 attempts
专项在同一 strict warning 模式下没有 warning。该已知项不能写成“全仓 ResourceWarning 干净”，
应由 backup owner 另行修复。

其余门禁：

```text
ruff check src tests scripts
All checks passed!

ruff format --check src tests scripts
78 files already formatted

mypy --strict --python-version 3.9 --follow-imports=skip src
Success: no issues found in 31 source files

python3 -m compileall -q src tests scripts
PASS

PYTHONPATH=src python3 examples/group_chat_demo.py --compact
completed=true; all three demo tasks completed

git diff --check
PASS
```

故障矩阵覆盖七个写 API 的 BEGIN/body/COMMIT-open、post-COMMIT exact readback、所有 no-op
return path、四类 exact control 的 hostile attrs/notes/chain/traceback、safe `SystemExit` 矩阵、
control subclass/group、module-name spoof、confirmed rollback、rollback/state/readback ambiguity、
poison/close/context body-priority、伪造内部 sentinel、claim heartbeat/deadline drift，以及真实 fork
子进程对 12 个公共入口的 fail-closed 行为。

## 6. 运维处理顺序

收到 `invocation_commit_ambiguous`，或收到 direct cause 为该错误的 clean control 时：

1. 停止该 invocation 的自动执行与 effect retry；
2. 只记录 stable error code、invocation ID 和受控 correlation metadata；不记录 raw lease token、
   SQL、path、driver exception 或 frame locals；
3. 旧 store 返回 `invocation_store_poisoned` 时，停止所有通过该实例的 admission；显式重试 parent
   process 内的 close，必要时终止持有连接的进程；
4. 用同一进程中新建的 store 打开 file-backed DB，核验 migration ledger、
   `PRAGMA integrity_check`、job/current-attempt snapshot 与 action/result receipt；
5. 只有 durable receipt 能证明 external effect outcome，或 connector 对 exact invocation 提供
   可查询幂等 acceptance 时，未来 recovery coordinator 才能决定后续动作；
6. 无 receipt 时维持 effect-unknown quarantine，交给 operator，不把“数据库里没看到 terminal”
   推断成“外部 effect 没发生”。

收到 `invocation_store_process_mismatch` 时，不做 transaction retry，也不在 child 关闭 inherited
store；修正进程启动顺序，在 child 中创建独立 store/connection。

## 7. 明确保留的 P0/P1 与残余风险

- `OrchestratorKernel` 仍直接调用 runtime，尚未接入 durable attempt worker；
- 没有可信 action/result receipt store、receipt-bound CAS 或 external-effect transaction；
- 进程在 COMMIT durable 后、Python 返回前被直接 kill 时，无法执行本阶段 readback，只能由
  restart recovery 观察 durable state；
- Agent/tool 调用抛错后写 `FAILED` 不能证明外部 effect 没发生；
- artifact/result、attempt terminal 与 task projection 尚非一个可恢复 transaction；
- `get`、`get_for_task`、`attempts`、`attempts_page`、`schema_version` 等普通读 API 会拒绝
  closed/poisoned/fork-mismatched store，但尚无通用 driver-error firewall；raw SQLite/provider
  fault 的公共脱敏是 P1，当前不得把它们直接发往用户或带 locals 的 telemetry；
- 现有 PID guard 防止 public misuse，但生产 process model 仍必须保证 fork 前不创建 store；
- session-local idempotency key 不能直接当作 connector 全局去重键；invocation epoch 也不是共享
  资源或全局 fencing token；
- 单机 SQLite/WAL、fake connector 与 deterministic fault injection 不能替代多机数据库、真实
  IM connector、chaos/kill、容量、soak、RPO/RTO 和 clean-host release evidence。

因此本阶段准确结论是：本地 durable storage primitive 的 commit/rollback/control/fork 边界已
显著收紧并有三版本直接证据；完整人机协同产品仍需完成端到端 effect/recovery、tenant admission、
真实 connector、安全隔离、可观测性和发布工程后，才可能进入生产商用评审。
