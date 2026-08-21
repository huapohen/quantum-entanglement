# 认证化 invocation 事务恢复：实现、反例与发布证据

更新日期：2026-08-20（Asia/Shanghai）

审计基线：`5420125200c01e9cc84063c10c86efd9af72ca00`

当前证据 HEAD：`4538159b032d20f55d4f3bf1757589e5310fe701`

当前证据 tree：`219fc66c8768a3a3c3508a52d6fe17bc07fb77c6`

## 1. 结论

本阶段把 durable invocation attempt store 的 SQLite 写事务从“异常后猜测结果”推进为
“只根据精确 durable readback 发布结果”，并关闭了两个独立审查复现的异常边界漏洞：

1. 外部异常可通过拼接一个可信 traceback frame 冒充库内 validation error；
2. 非精确布尔 transaction state 会被 Python truth-test，进而执行攻击者控制的
   `__bool__`、泄露异常或错误分类事务结果。

最终实现要求完整 traceback 的每个 Python frame 都属于启动时冻结的可信 code-object 集；
validation descriptor 还必须由本次公共调用的 nonce 绑定，公共 wrapper 离开所有
`catch/finally`、清理调用引用后才创建并抛出新异常。事务状态只接受 exact `bool`：
`True` 进入已确认打开事务的 rollback 路径，`False` 进入 lost-ACK readback；其余任何值
都会在不调用 `__bool__` 的前提下 poison/close store，并发布干净的 ambiguity。

在同一证据 HEAD 上：

- Python 3.9：883 passed、1 skipped；
- Python 3.12：884 passed；
- Python 3.13：884 passed，`ResourceWarning` 与
  `PytestUnraisableExceptionWarning` 均升级为 error；
- attempts 直接矩阵 113 项，invocation recovery 30 项；
- Ruff、format、strict mypy、compileall、dependency-lock verifier、演示与
  `git diff --check` 全部通过；
- 已知不安全的临时事务链不在当前 HEAD 的 ancestry。

这证明的是单节点 SQLite storage primitive 的阶段可运行性，不是整个人机协同产品已达到
GA、distributed exactly-once、真实 IM connector 安全发送或多租户商用认证的声明。

## 2. 为什么必须重建提交链

第一次实现把完整修复拆成了以下临时链：

- `554115cac9824eda8e0ba3932cf7b94b64d29f91`
- `4f7f040296ba0d513e7f48ae3a00d7fdf3925965`
- `dcc274bc1b081ee9098ebcdae911bbfb795ae34a`
- `57ba5d8...`
- `f18ad9fa59c041e36a6847b934a76909add19034`
- `e05357bebde6694449c5eb6361df1c7ad8ede9e8`

该历史的早期阶段会把不安全行为暴露为可检出的 commit；即使末端 tree 已修复，也不满足
“每个阶段都可正式运行”的提交不变量。因此没有 merge、rebase 或 cherry-pick 这条历史，
而是从最新审计基线重放完整依赖，再把最终安全 tree 与全部反例作为一个不可再拆的行为提交。

以下命令在当前证据 HEAD 上均返回“不是祖先”：

```bash
git merge-base --is-ancestor 554115c HEAD
git merge-base --is-ancestor f18ad9f HEAD
git merge-base --is-ancestor e05357b HEAD
```

非零退出在这里是预期结果，代表不安全临时历史被排除。

## 3. 最小完整前置链

早期 dry-run 从 `3eb79c1` 开始，在 `998f546` 发生上下文冲突。根因不是偶发 Git
冲突，而是缺少 `_SESSION_STREAM_PREFIX` 与 `_MAX_SESSION_STREAM_BYTES` 的来源提交：
`0daf487`、`fe9ccde`。最终确认的最小完整序列共 16 枚：

| Commit | Tree | 阶段 |
|---|---|---|
| `115acb2` | `07e746b2d9e7c807d848359793bbd25479d936c4` | receipt stream identity bound |
| `9597fe1` | `a47a45ca9219e8bfd01ce977f3aab31dd2012f90` | derived stream limit 文档 |
| `e380578` | `b3bd46eb3661458de5344ef1ffbb11800aaac0bc` | invocation fencing epoch 反例 |
| `a450ade` | `15074448e26715b71fce0e4cee74200e05adeb95` | fencing epoch 合同 |
| `f68a205` | `58e39b03d9c97012a165719607be2a9dcf4dc96c` | UTC 年边界规范化 |
| `1963014` | `43183d162a718d500ce984a5991fd9878b89e9bb` | UTC range 文档 |
| `dace3a9` | `27f5323f40b972dd26d7dece9f353a70919d2015` | first-claim remnant 拒绝 |
| `ae7008a` | `f814de5fcefda0b409921a4dc40405ced8df1e96` | zero-counter 合同 |
| `eba7c51` | `89d5abb00eba54d9512518daae74ff0a30446872` | attempt/job error 绑定 |
| `c8187cd` | `ef2928b8f75ee902a73047d146561e891ac99d5e` | error 一致性文档 |
| `f29be2e` | `a3af03879fe678a1bf5b9942cc74f5e1674dbe70` | recovery error 长度 |
| `ac18b1e` | `54aa278b58f599dce5816d47b9974751c322159f` | error limit 文档 |
| `9dd7d12` | `21daa545fe38679a67deb6809c3e8ff8b57e883a` | job status/attempt budget |
| `085e280` | `d5afa9f1136b858237097b607eccfd88f853ddce` | budget invariant 文档 |
| `1395386` | `4c4143247abc654cbfbd3e712883f2c2703750f4` | parallel dispatch draining |
| `1820f3a` | `3d95e57e7e6273c682729a5f0af7ffe9b5b7d3f5` | structured drain 文档 |

该序列先在 disposable worktree 的 `4944a3e` 上逐枚重放，再在
`5420125` 上创建最终分支并再次逐枚重放。两轮均无冲突；最终分支每一步都运行
`tests/test_attempts.py tests/test_invocation_recovery.py`，并在进入新行为前通过全仓门禁。

## 4. 最终安全提交

| Commit | Tree | 不变量 |
|---|---|---|
| `663edd4f63dc299ff40a751cc7c25bd80b7e9331` | `f37a0eab4c75d283aab6ba08a2c9856fcb107309` | 本地 `tests` 包优先，CI 不受同名 site-package 污染 |
| `c5f1fb5f7ac5e606ea6fdad574447e1725ad86df` | `e83da8700aae10edeca42dbb0d1c6910edc84b34` | 完整认证事务结果、反例矩阵与 fork guard |
| `62a29d0c4a8606deebbe32c1dd645cbf5111c4f8` | `d1edc2f07d4ba24f710530bf321f7e56addbb5c8` | backup fixture 连接显式关闭，strict warning 干净 |
| `d51c5b9bdce0de413cee80fa159c2e3bcbbb798c` | `1d8f1223e6b5b37620cf746ca5195ccae90d7daa` | 运维合同、异常边界、精确 bool 与测试数 |
| `4538159b032d20f55d4f3bf1757589e5310fe701` | `219fc66c8768a3a3c3508a52d6fe17bc07fb77c6` | 同步最新 process-inheritance readiness 缺口 |

`c5f1fb5` 的代码和测试 blobs 与独立审查通过的最终修复 blobs 完全一致：

```text
attempts.py       1068973b6491f7c35d8e6039ea92fe230cea8cfd
test_attempts.py  c2cd73aafaf8b6feb4eca1105eb1e8292bd5c1fb
```

这里比较 blob，不复用被拒绝分支的 ancestry。

## 5. 事务结果合同

### 5.1 confirmed rollback

BEGIN、body 或 COMMIT 在事务仍 exact-open 时失败，且 rollback 得到确认：

- 普通 provider/driver error 变成固定 `InvocationTransactionError`；
- exact control 被清洗后重签发；
- 合法库内 validation/integrity error 仅在完整 provenance 通过后重建；
- raw identity、args、attrs、notes、cause、context、traceback 均不越界。

### 5.2 lost commit ACK

COMMIT 已结束但 ACK 丢失时，不根据异常类型猜测，而是按 public operation 保存的 candidate
读取 durable state。只有完整等价才返回成功；并发合法推进导致 readback 不再完全匹配时返回
safe false negative，即 `InvocationCommitAmbiguityError`，绝不返回 false success。

### 5.3 ambiguous state

rollback、state inspection 或 readback 无法确认时：

1. 先设置 poison；
2. best-effort close；
3. 当前调用发布稳定 ambiguity；
4. 后续所有 store API fail closed；
5. operator 只能通过新 store 做 migration/integrity/snapshot/receipt 核验。

### 5.4 control 与 validation 重签发

exact `KeyboardInterrupt`、`GeneratorExit`、`CancelledError` 和安全
`SystemExit` 保留控制含义，但跨公共边界的一定是新实例。validation 也不复用原异常：
可信 descriptor 经 exact type、完整 traceback provenance 与本次调用 nonce 三层校验，
外层 wrapper 在 catch 外重新构造。这样不会把原始 `__context__`、frame locals 或 provider
对象带回调用方。

### 5.5 process ownership

store 在构造时绑定 creator PID。fork 子进程对 read/write/recovery/context/close 的调用会在
读取继承锁、SQLite connection、poison flag 或 control stack 之前 fail closed。子进程必须丢弃
继承引用并创建自己的 store；不得在 child 调用 inherited store 的 `close()`。

## 6. 反例矩阵

新增或保留的关键反例包括：

- grafted trusted traceback 不得授权外部 `ValueError`；
- validation 必须在 catch 外以干净异常图发布；
- `None`、`0`、`1`、字符串、bool subclass 替代物与 hostile
  `__bool__` transaction state 均不得 truth-test；
- exact `True`、`False` 仍分别保留 confirmed rollback 与 lost-ACK 语义；
- 伪造 validation/control/commit/read sentinel 的 type、nonce、descriptor、poison nonce；
- BEGIN/body/COMMIT-open/post-COMMIT/readback/rollback/state/close 全故障点；
- 七个 public write API 的成功、no-op、异常与并发推进；
- hostile attrs、notes、cause/context、traceback、control subclass/group；
- POSIX fork 对 12 个公共入口的 process mismatch。

## 7. 本地门禁证据

| 门禁 | 结果 |
|---|---|
| Python 3.9.6 + pytest 8.4.2 | 883 passed，1 skipped，14.34s |
| Python 3.12.12 + pytest 8.4.2 | 884 passed，11.68s |
| Python 3.13.9 strict warnings | 884 passed，11.41s |
| attempts direct collection | 113 |
| invocation recovery collection | 30 |
| Ruff check | All checks passed |
| Ruff format | 91 files already formatted |
| strict mypy | 34 source files clean |
| compileall | `src tests scripts examples` PASS |
| dependency locks | 4 targets、74 package records、verified |
| compact demo | 3 tasks completed、25 events、3 artifacts、`completed=true` |
| whitespace/source status | `git diff --check` PASS；worktree clean |

核心复核命令：

```bash
PYTHONPATH=src python -m pytest -o addopts='' -q
PYTHONPATH=src PYTHONWARNINGS=error::ResourceWarning python3.13 -m pytest -o addopts='' -q -W error::pytest.PytestUnraisableExceptionWarning
ruff check src tests scripts examples
ruff format --check src tests scripts examples
PYTHONPATH=src mypy --strict src
PYTHONPYCACHEPREFIX=/tmp/qe-pycache python3.13 -m compileall -q src tests scripts examples
python3 scripts/verify_dependency_locks.py --repository-root .
PYTHONPATH=src python3 examples/group_chat_demo.py --compact
git diff --check
```

## 8. 运维决策

收到 `invocation_commit_ambiguous` 或以它为 direct cause 的 clean control 时：

1. 停止对应 invocation 的自动 mutation/effect retry；
2. 只记录 stable code、invocation ID 与受控 correlation metadata；
3. 禁止记录 raw SQL、数据库 path、lease token、driver exception 或 traceback locals；
4. quarantine 旧 store，用同进程的新 store 只读核验 schema、integrity、attempt/job 和 receipt；
5. 只有 trusted durable receipt 或 connector 的 exact idempotent acceptance 查询能解除
   effect-unknown；
6. 无法证明时维持 operator quarantine，不把“本地未见 terminal”推断成“外部未发生”。

收到 `invocation_store_process_mismatch` 时，不做 transaction retry，也不在 child 关闭
继承对象；修正 process 启动顺序。

## 9. 残余 P0/P1 与范围边界

以下边界仍然开放，因此整体产品当前不能宣称生产商用：

- Gate A operation authorization 的独立实现仍在修复审查发现的历史窗口、异常图和
  `SystemExit` 语义问题，尚未集成；
- backup manifest v2 的 topology registry、完整 DDL evidence、count invariants 与 bounded
  integer parsing 仍在重建，旧分支不得集成；
- `OrchestratorKernel` 尚未把 durable attempt、Agent 调用、artifact/result、task projection
  和 trusted external-effect receipt 串成一个端到端可恢复状态机；
- 普通 attempt read API 仍没有统一 provider-error firewall；
- 单机 SQLite/WAL 不是多机数据库、HA 或 distributed fencing；
- 尚缺真实 connector 的幂等 acceptance 查询、kill-point chaos、容量、soak、RPO/RTO、
  immutable CI artifact、正式安全评审和运营验收；
- 任何真实飞书/企微发送都不在本阶段授权范围，测试只使用 fake、no-op 或只读证据。

## 10. 集成与回滚

- 集成目标必须包含 `5420125` 或其仅文档后继，并保持本报告列出的 21 枚提交顺序；
- 不得把旧 `554115c..e05357b` 链作为祖先；
- 集成后必须在目标 HEAD 复跑三版本全仓测试、strict warnings、Ruff、mypy、compileall、
  dependency locks 和 demo；
- 回滚应用代码时，数据库 schema 无新增 migration；但对已发生 ambiguity 的 store 不得
  通过软件降级恢复使用，必须保持 quarantine 并做 durable reconciliation；
- 本报告是本地阶段证据，不是 promotion approval。正式发布还要满足
  `docs/production/RELEASE_GATES.md` 的相应 runtime gate。
