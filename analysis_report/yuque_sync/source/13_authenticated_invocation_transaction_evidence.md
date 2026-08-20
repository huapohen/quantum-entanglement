# 10｜认证化 Invocation 事务恢复证据

> 🧬 **同步来源**：本页镜像本地 `analysis_report/research/10_authenticated_invocation_transaction_evidence.md`。本地证据 HEAD 为 `4538159b032d20f55d4f3bf1757589e5310fe701`，tree 为 `219fc66c8768a3a3c3508a52d6fe17bc07fb77c6`；测试数和结论只对该固定代码状态成立。

## 1. 结论

本阶段把 durable invocation attempt store 的 SQLite 写事务推进为“只根据精确 durable readback 发布结果”，并关闭两个可复现的异常边界漏洞：

- 外部异常可通过拼接可信 traceback frame 冒充库内 validation error；
- 非精确布尔 transaction state 会被 Python truth-test，进而执行攻击者控制的 `__bool__`、泄露异常或错误分类事务结果。

最终实现要求完整 traceback 的每个 Python frame 都属于启动时冻结的可信 code-object 集。validation descriptor 还必须由本次公共调用的 nonce 绑定；公共 wrapper 离开所有 catch/finally、清理调用引用后，才创建并抛出新异常。

事务状态只接受 exact `bool`：`True` 进入 confirmed-open rollback，`False` 进入 lost-ACK readback；其他值在不调用 `__bool__` 的前提下 poison/close store，并发布干净 ambiguity。

> ⚠️ 这是单节点 SQLite storage primitive 的阶段证据，不是整个人机协同产品达到 GA、distributed exactly-once、真实 IM connector 安全发送或多租户商用认证的声明。

## 2. 为什么重建提交链

最初临时实现链的早期 commit 暴露了不安全行为。即使末端 tree 后来修复，也不满足“每个阶段都可正式运行”的提交不变量。因此本次没有复用 `554115c..e05357b` ancestry，而是：

1. 从最新审计基线重放 16 枚完整依赖；
2. 在干净前置 head 上一次提交最终安全行为与必要反例；
3. 把测试隔离、资源关闭、生产合同和研究证据分别做成小提交；
4. 用 `git merge-base --is-ancestor` 证明 `554115c`、`f18ad9f`、`e05357b` 均不在当前 ancestry。

## 3. 最小前置链

早期 dry-run 从 `3eb79c1` 开始，在 `998f546` 冲突。根因是遗漏 `0daf487` 和 `fe9ccde` 提供的 receipt stream identity/limit 定义。最终最小链共 16 枚，覆盖：

- receipt stream identity 与 derived limit；
- invocation-scoped fencing epoch；
- UTC 边界与 error 长度；
- first-claim/zero-counter remnant；
- attempt/job error 与 budget/status 不变量；
- parallel dispatch drain 与 structured failure。

该序列先在 disposable worktree 的 `4944a3e`，再在正式安全分支的 `5420125` 上逐枚重放。两轮均无冲突；正式分支每枚提交后都运行 attempts + invocation recovery 专项。

## 4. 关键安全提交

| Commit | Tree | 不变量 |
|---|---|---|
| `663edd4` | `f37a0ea` | 本地 tests 包优先，CI 不受同名 site-package 污染 |
| `c5f1fb5` | `e83da87` | 认证事务结果、反例矩阵与 fork guard |
| `62a29d0` | `d1edc2f` | backup fixture 连接显式关闭，strict warning 干净 |
| `d51c5b9` | `1d8f122` | 运维合同、完整 provenance、exact bool 与测试数 |
| `4538159` | `219fc66` | 同步最新 process-inheritance readiness 缺口 |

`c5f1fb5` 的关键 blobs：

- `attempts.py`：`1068973b6491f7c35d8e6039ea92fe230cea8cfd`
- `test_attempts.py`：`c2cd73aafaf8b6feb4eca1105eb1e8292bd5c1fb`

这些 blobs 与独立审查通过的最终修复内容一致，但不复用被拒绝分支的 ancestry。

## 5. 事务结果合同

### 5.1 Confirmed rollback

BEGIN、body 或 COMMIT 在事务仍 exact-open 时失败，且 rollback 已确认：

- 普通 provider/driver error 变为固定 `InvocationTransactionError`；
- exact control 清洗后重签发；
- 合法库内 validation/integrity error 仅在完整 provenance 通过后重建；
- raw identity、args、attrs、notes、cause、context、traceback 均不越界。

### 5.2 Lost commit ACK

COMMIT 已结束但 ACK 丢失时，不根据异常类型猜测，而是按 public operation 保存的 candidate 读取 durable state。只有完整等价才返回成功；并发合法推进导致 readback 不再完全匹配时返回 safe false negative：`InvocationCommitAmbiguityError`，绝不返回 false success。

### 5.3 Ambiguous state

rollback、state inspection 或 readback 无法确认时：

1. 先设置 poison；
2. best-effort close；
3. 当前调用发布稳定 ambiguity；
4. 后续 store API fail closed；
5. operator 只能通过新 store 做 migration/integrity/snapshot/receipt 核验。

### 5.4 Control 与 validation

exact `KeyboardInterrupt`、`GeneratorExit`、`CancelledError` 和安全 `SystemExit` 保留控制含义，但跨公共边界的一定是新实例。validation 也不复用原异常：可信 descriptor 经 exact type、完整 traceback provenance 与本次调用 nonce 三层校验，外层 wrapper 在 catch 外重新构造。

### 5.5 Process ownership

store 构造时绑定 creator PID。fork 子进程对 read/write/recovery/context/close 的调用会在读取继承锁、SQLite connection、poison flag 或 control stack 前 fail closed。child 必须丢弃继承引用并创建自己的 store，不得关闭 inherited store。

## 6. 反例矩阵

- grafted trusted traceback 不得授权外部 `ValueError`；
- validation 必须在 catch 外以干净异常图发布；
- `None`、`0`、`1`、字符串与 hostile `__bool__` state 均不得 truth-test；
- exact `True` / `False` 仍保持 rollback / lost-ACK 语义；
- 伪造 validation/control/commit/read sentinel 的 type、nonce、descriptor 与 poison nonce；
- BEGIN/body/COMMIT-open/post-COMMIT/readback/rollback/state/close 全故障点；
- 七个 public write API 的成功、no-op、异常与并发推进；
- hostile attrs、notes、cause/context、traceback、control subclass/group；
- POSIX fork 对 12 个公共入口的 process mismatch。

## 7. 同一 HEAD 的门禁

| 门禁 | 结果 |
|---|---|
| Python 3.9.6 | 883 passed，1 skipped，14.34s |
| Python 3.12.12 | 884 passed，11.68s |
| Python 3.13.9 strict warnings | 884 passed，11.41s |
| Attempts / recovery | 113 / 30 collected |
| Ruff / format | All checks passed；91 files formatted |
| Strict mypy | 34 source files clean |
| Dependency locks | 4 targets、74 package records、verified |
| Demo | 3 tasks、25 events、3 artifacts、completed=true |

Python 3.13 把 `ResourceWarning` 与 `PytestUnraisableExceptionWarning` 升级为 error。strict gate 最初发现 backup fixture 使用 SQLite context manager 但未关闭 connection；`62a29d0` 显式关闭后，全仓 warning gate 干净。

## 8. 运维处理

收到 `invocation_commit_ambiguous` 或以它为 direct cause 的 clean control 时：

1. 停止对应 invocation 的自动 mutation/effect retry；
2. 只记录 stable code、invocation ID 与受控 correlation metadata；
3. 不记录 raw SQL、数据库 path、lease token、driver exception 或 traceback locals；
4. quarantine 旧 store，用同进程的新 store 只读核验 schema、integrity、attempt/job 与 receipt；
5. 只有 trusted durable receipt 或 connector exact idempotent acceptance 查询能解除 effect-unknown；
6. 无法证明时维持 operator quarantine。

收到 `invocation_store_process_mismatch` 时，不做 transaction retry，也不在 child 关闭继承对象；修正 process 启动顺序。

## 9. 仍阻断整体商用的 P0/P1

- Gate A operation authorization 仍在修复独立审查发现的历史窗口、异常图和 `SystemExit` 语义问题；
- backup manifest v2 的 topology registry、完整 DDL evidence、count invariants 与 bounded integer parsing 仍在重建；
- Orchestrator 尚未把 durable attempt、Agent 调用、artifact/result、task projection 与 trusted external-effect receipt 串成端到端可恢复状态机；
- 普通 attempt read API 仍没有统一 provider-error firewall；
- 单机 SQLite/WAL 不是多机数据库、HA 或 distributed fencing；
- 尚缺真实 connector 幂等 acceptance、kill-point chaos、容量、soak、RPO/RTO、immutable CI artifact、正式安全评审与运营验收；
- 任何真实飞书/企微发送都不在本阶段授权范围。

## 10. 集成与回滚

- 集成目标必须包含 `5420125` 或其仅文档后继，并保持经验证的提交顺序；
- 不得把旧 `554115c..e05357b` 链作为祖先；
- 集成后必须在目标 HEAD 复跑三版本全仓测试、strict warnings、Ruff、mypy、compileall、dependency locks 与 demo；
- 数据库 schema 无新增 migration；但发生 ambiguity 的 store 不得通过软件降级恢复使用，必须继续 quarantine 并做 durable reconciliation；
- 本页是本地阶段证据的私有 Notion 镜像，不是 promotion approval。

## Sources

- [07｜当前实现证据与生产边界](https://app.notion.com/p/3c1ead4b996e81669cefcf330b894853)
- [Quantum Entanglement｜人 + 多 Agent 协同办公](https://app.notion.com/p/3c1ead4b996e81e289c7dde1d597f630)

来源：https://app.notion.com/p/3c2ead4b996e8105b0cad304ef28dd38?pvs=204
