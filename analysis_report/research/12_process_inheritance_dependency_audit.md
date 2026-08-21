# 进程继承、fork 与进程本地安全边界审计

审计日期：2026-08-20（Asia/Shanghai）

源码检查点：`4944a3eae137ff3b6d2574494c658b93e7904567`

状态：**只读设计与源码审计；没有关闭任何 production Gate**

## 1. 执行结论

当前代码对线程并发、SQLite 多连接竞争和部分对象复制有较强测试，但没有统一定义
“对象能否跨进程继承”。POSIX `fork()` 会复制 Python 对象、SQLite connection、锁的内部状态、
授权注册表、密钥 material、异步 event-loop 归属和一次性 handle。复制后的 parent/child 不共享
Python 内存，却可能同时操作同一外部数据库或分别接受同一授权事实。

因此，`non-copyable`、`non-pickleable`、`process-local`、`one-time` 或“SQLite 支持多进程”都不能
单独证明一个**已经构造的实例**可安全在 fork child 使用：

- 禁止 copy/pickle 不会阻止地址空间级复制；
- SQLite 支持独立进程各自新建 connection，不代表 fork 后复用同一 connection 安全；
- parent/child 各自持有一份 one-time registry 时，两边都可能消费同一 handle；
- fork 时另一个线程持锁，child 继承的锁可能永远没有原 owner 线程来释放；
- child 继承 secret buffer，即使 parent 随后擦除，也不会擦除 child 的 copy；
- `asyncio.Lock`、Task、Future 和 loop-bound adapter 不能靠 PID 不同自行恢复。

Phase 1 必须采用以下硬边界：

```text
fork/spawn/exec worker topology must be established before
SQLite connections + authorization registries + request contexts + secrets + event loops
are constructed.

Any reusable stateful instance inherited across PID/process epoch
must reject before touching an inherited lock, connection, provider, registry or handle.
```

对于 credential-bearing 或执行不可信插件的 worker，仅在方法入口拒绝还不够，因为 secret bytes
已经进入 child 地址空间。此类隔离必须使用 **spawn/exec before secret load**，或使用能够证明
child 没有继承 material 的外部 secret broker；不得把 fork child 当成安全 sandbox。

## 2. 权威证据与审计口径

本报告只计算检查点源码中的事实，并使用以下搜索和人工检查：

```text
rg "threading.RLock|threading.Lock|asyncio.Lock|sqlite3.connect|process-local"
rg "__copy__|__deepcopy__|__reduce_ex__|__enter__|__exit__|close"
```

检查点全门禁已重新执行：

- 4 个 dependency-lock target、74 package records：通过；
- Python 3.12.13：820 tests passed；
- Ruff 0.16.3 lint 与 format：通过；
- strict mypy 1.19.1：34 source files 通过；
- compileall、deterministic demo、`git diff --check`：通过。

这些门禁没有 fork/process matrix，因此不能反证本报告的缺口。

并行候选分支不计入检查点：

- durable invocation transaction 候选仍在独立复核，未获准集成；
- action-time protected-operation 候选的旧历史已因 fork duplicate-consume 反例冻结；
- backup manifest v2 codec 仍在独立开发，且必须保持 writer 不可达。

## 3. 生产不变量

### 3.1 通用 process-bound 实例

任何实例只要持有下列任一状态，就必须显式声明 `fork_safe`、`reopen_after_fork` 或
`process_bound`，默认不能沉默继承：

- mutex/RLock/Condition/Semaphore；
- SQLite connection、cursor、transaction 或 file descriptor；
- event loop、Task、Future、asyncio lock；
- one-time handle registry、nonce set、capacity reservation；
- clock/revision/high-water、key lifecycle、revocation snapshot；
- credential/secret buffer；
- plugin/connector/provider instance；
- mutable in-memory ledger、cache、session/lease owner state。

`process_bound` 的最低实现要求：

1. 构造时记录 exact creator PID 与不可伪造的当前 process epoch identity；
2. `os.register_at_fork(after_in_child=...)` 可刷新模块 epoch，PID drift 仍作为独立 fallback；
3. 每个 public path 在任何 inherited lock/connection/provider access **之前**检查 PID+epoch；
4. process mismatch 只发布固定 code 的干净异常，不携带 caller/provider/raw driver graph；
5. parent 不被 child 的拒绝、close 或 registry retire 影响；
6. child 不能通过 `close()`、context manager exit、repr/copy/pickle hook 间接触碰 inherited state；
7. 对象若可在新进程重新创建，必须从 path/config/opaque reference 创建新实例，不复用旧对象。

只检查 PID 不足以表达同一 PID 内的显式 epoch rotation；只检查 epoch 不足以处理 hook 注册失败。
两者必须同时绑定。

### 3.2 fork-safe immutable value

纯 immutable value 可被 fork 复制，但必须不含：

- live authority、one-time token 或“由对象身份证明可信”的语义；
- secret/private key material；
- weakref 到 issuer/registry；
- file descriptor、connection、lock、loop 或 callback；
- 能在 child 被误当成 fresh evidence 的 current-state claim。

`RequestContext`、authorized operation、lease token 等即使字段不可变，也不是普通 value；其可信性
依赖 issuer/store-owned live registry，必须随 owner instance 的 process binding 一起失效。

### 3.3 SQLite

生产支持的是：不同进程在 fork/spawn 完成后，各自打开 connection，并依赖 WAL/busy timeout/
transaction CAS 协调。明确不支持：把一个已打开的 `sqlite3.Connection` 复制到 child 后继续使用。

child 的 mismatch 检查必须早于 Python RLock；否则 fork 发生在其他线程持锁时，检查本身也可能
永远执行不到。拒绝后不得 best-effort close 继承的 connection，因为底层 descriptor 与 SQLite
library 状态也来自 parent；child 应丢弃整个实例并构造新 store。

### 3.4 Secrets 与不可信 worker

`SecretMaterial.close()` 只能擦除当前进程的 owned buffer。fork 复制发生后：

```text
parent wipe != child wipe
```

所以生产 worker 进程必须先 spawn/exec，再在各自最小权限边界内按 opaque `SecretRef` 获取所需
material。不得先在 orchestrator 读取 secret 再 fork，也不得把继承 secret 的 child 描述成隔离。

## 4. 当前组件矩阵

| 组件 | 当前持有状态 | fork 后主要风险 | 当前证据 | 要求 |
|---|---|---|---|---|
| `SQLiteEventStore` | SQLite connection + RLock + clock | inherited connection、锁停滞、parent/child 并发写、重复 command/event | 无 PID/epoch guard | P0：所有 public DB/close/context paths process-bound |
| `SQLiteArtifactStore` | SQLite connection + RLock + clock | 同上；artifact CAS/readback 可能由不受控 child 执行 | 无 guard | P0：在任何 lock/connection/clock 前拒绝 |
| `SQLiteProjectionOffsetStore` | SQLite connection + RLock + authorizer | inherited lease owner、authorizer/transaction 状态、offset 双执行 | 无 guard | P0：store 与 projector lifecycle process-bound |
| `SQLiteRevocationRevisionGuard` | SQLite connection + RLock + durable high-water | child 复用 connection；安全 revision CAS 失去可靠运行边界 | 无 guard | P0：新进程重开；旧实例 fail closed |
| `SQLiteInvocationAttemptStore` | SQLite connection + RLock + lease state | duplicate claim/terminal CAS、锁停滞 | 检查点无 guard；候选修复尚未获准 | P0：真实 fork 全 public-path matrix |
| `InvocationRecoveryCoordinator` | RLock + store/reference state | copied recovery decision、双恢复、调用 inherited store | 无 guard | P0：coordinator 与 store 同 process identity |
| `RequestContextIssuer` | RLock + live context registry + pending capacity + clock high-water + authenticator | parent/child 都可认可或签发 context；fork while locked | 检查点只禁止 copy/pickle | P0：issuer/handle 在 child 全部失效 |
| protected-operation composer/registry（候选） | RLock + one-time operation registry + current auth dependencies | parent/child duplicate consume 同一授权动作 | 旧候选已复现并冻结 | P0：PID+epoch、issuer current-process proof、real-fork tests |
| `InMemoryRevocationRevisionGuard` | RLock + revision/digest high-water | parent/child high-water 分叉，可各自接受冲突 future state | 标注 single-process，但无 fork rejection | P0：安全组合禁用继承；测试 fake 可明确降级 |
| `RotatingHMACKeyRing` | RLock + HMAC key material + status/tombstone high-water | child 继承 signing key；rotation/revocation/tombstone 分叉 | non-durable 声明，无 fork/secret custody contract | P0：生产不使用内存 signing；至少 process-bound |
| `TenantAuthorizer` 组合 | verifier + revocation guard + policy | fresh child composition 可能复用 inherited key/guard | 无统一 process proof | P0：依赖共同绑定同一 current process epoch |
| `SecretMaterial` | mutable secret bytearray | child 得到独立可读 copy，parent wipe 无效 | copy/pickle 防护不能阻止 fork | P0：fork-before-load / spawn-exec topology |
| `ArtifactLedger` | RLock + mutable versions/index/usage | parent/child 产生分叉 ledger 与冲突 version | 仅进程内实现 | P1；生产必须用 durable store/UoW，实例 process-bound |
| `EventUpcasterRegistry` | RLock + mutable registry/sealed state | fork while locked；child/parent registry seal 状态分叉 | 无 guard | P1；seal 后也需明确 immutable snapshot 或 process binding |
| `PolicyEngine` | RLock + mutable rules | policy snapshot 分叉或继承锁停滞 | 无 guard | P1/P0，取决于是否参与受保护动作 |
| `PluginManager` | RLock + plugin instances | child 继承 provider/connector、文件和网络能力 | 无 guard | P0：不可信 plugin 必须 spawn/exec 后装载 |
| `AgentRegistry` / `OrchestratorKernel` | asyncio locks + session/event-delivery locks + Agents/store references | loop affinity 失效、重复 session 调度、双 Agent invocation | 无 process contract | P0：composition root 在最终进程内创建 |
| runtime/harness adapters | asyncio locks + callbacks + external runtime handles | inherited loop/task/callback，close/drain 语义不确定 | 只有单 loop 生命周期测试 | P0：禁止 fork-after-start；spawn worker 后构造 |
| `OutboxPublisher` | lazy asyncio cycle lock + store/connector | child 继承 connector/store，重复 publish/ACK | connector 仍 fake-only | P0：publisher+connector current-process composition |
| backup 函数 | 临时 fd/SQLite connection/temp paths | fork 中途 child 继承 publication state | 不是可复用 service instance | P1：service 禁止 operation 中 fork；child 不恢复半次发布 |

该表不是“给每个类机械加 PID 字段”的许可。安全依赖必须在 composition root 统一验证；否则
一个 fresh child wrapper 仍可能包住 inherited issuer、provider、key ring 或 store 并绕过外层检查。

## 5. 已确认的同类失败模式

### 5.1 fork duplicate consume

一次性授权对象和其 registry 被地址空间复制后，parent/child 的 retire 互不可见；若两边都执行
consume，各自都可能看到“尚未消费”。这直接违反不可逆 effect 的 at-most-one authorization
admission，不得以“Python 对象不可 pickle”作为缓解。

### 5.2 fork while lock held

多线程 parent 在任一线程持 `threading.RLock`/`asyncio.Lock` 时 fork，child 只保留调用 fork 的
线程。原 owner 线程不存在，child 访问继承对象可能永久等待。故 process guard 必须在锁外，并且
不能调用可能间接获取该锁的 helper/property/repr。

### 5.3 inherited SQLite connection

即使一次简单 child query 看似成功，也不是支持证据。connection 内含 SQLite library 与 Python
wrapper 状态；parent/child 并发或 transaction 中 fork 的行为不满足本项目的 CAS/reconcile
证明。child 只能新建 connection。

### 5.4 security high-water split

request clock、revocation revision、key status/tombstone 和 one-time handle set 是 monotonic safety
state。fork 把一条 high-water 线变成两条独立线；各进程可接受在另一进程看来 stale/conflicting
的状态。生产要么使用 durable shared CAS，要么让继承实例全部失效。

### 5.5 secret/address-space duplication

方法入口 mismatch error 无法回收已经复制到 child 的 bytes。这是 topology 问题，必须在读取
secret 之前完成进程创建，而不是只给 `SecretMaterial.view()` 加 PID 检查后宣称隔离完成。

## 6. 失败与威胁矩阵

| 优先级 | 场景 | 失败结果 | 必需证明 |
|---|---|---|---|
| P0 | inherited operation 在 parent/child 各消费一次 | 未授权重复不可逆 effect | one shared durable receipt/CAS 或 child fail-before-lock |
| P0 | inherited issuer 在 child 签发/认可 context | 身份/tenant scope 绕过进程边界 | issuer owner PID+epoch；fresh composer 拒绝 inherited issuer |
| P0 | inherited key ring 在 child 继续签名 | revoked/retired key 复活 | KMS/HSM custody 或 process-bound adapter + durable tombstone |
| P0 | inherited SQLite store 执行 mutation | corruption、lost ACK、duplicate work | fresh connection per process；old instance deterministic reject |
| P0 | child 继承 plaintext secret | credential exposure | spawn/exec-before-load retained test/architecture evidence |
| P0 | fork while auth/store lock held | core request 永久 hang | pre-lock guard + bounded real-fork test |
| P0 | child 继承 runtime/connector | duplicate Agent/tool/ACK | post-spawn composition root + fenced durable state |
| P1 | mutable registry/ledger 分叉 | replay/upcast/artifact state 不一致 | immutable snapshot or durable store/process binding |
| P1 | backup publication 中 fork | orphan temp/ambiguous publish | service fork prohibition + startup cleanup ownership proof |
| P1 | spawn/forkserver serialization 偷带 live authority | authority 被当 value 传播 | pickle/copy rejection + opaque reference reconstruction |

## 7. 建议的共享 process identity foundation

避免每个模块各自发明略有差异的 PID 检查。建议先实现一个依赖-free、无锁的内部 foundation：

```text
current_process_identity() -> exact (pid, epoch-object)
capture_process_owner() -> opaque owner descriptor
require_current_process(owner, stable_error_factory) -> None
```

约束：

- epoch object 不公开序列化，不进入日志/repr/event/manifest；
- hook 只替换 module-global PID/epoch，不遍历实例、不获取锁、不 close fd、不分配复杂对象；
- PID drift fallback 在无 hook、hook 注册失败和测试替换下仍生效；
- caller 不能提供 owner descriptor；constructor 自行 capture；
- guard 只进行 exact primitive comparison，不调用 owner/provider 的 overloaded equality/truthiness；
- stable public error 不包含 PID、path、tenant、handle、secret 或原异常；
- helper 本身需要真实 fork、fork while unrelated lock held、spawn/forkserver/import-reload tests；
- 若 `os.fork` 不可用，平台测试明确 skip，不能把 skip 写成通过 fork 证明。

共享 helper 只解决“继承实例不可用”，不解决 secret 已复制问题，也不让 SQLite connection 变成
fork-safe。

## 8. 测试矩阵

每个 process-bound public API 至少覆盖：

| 维度 | 必需用例 |
|---|---|
| Fork before use | child 每个 public read/write/close/enter/exit 都返回同一 stable mismatch code |
| Parent continuity | child 失败后 parent 可继续完整读写/消费/close |
| Fork while lock held | child 在短 timeout 内拒绝，不等待 inherited lock |
| Open transaction | child 不 inspect/rollback/close inherited SQLite transaction；parent 可决定 rollback |
| One-time handle | parent/child 不可能各成功 consume；child rejection 不 retire parent handle |
| Issuer composition | fresh child composer/authorizer 也拒绝 inherited issuer/key/guard/provider |
| Control signals | mismatch check 不吞 `KeyboardInterrupt/SystemExit/GeneratorExit/CancelledError` |
| Error graph | exception cause/context/traceback/notes/args/attrs 不含 caller/provider/store object |
| Copy/pickle | copy/deepcopy/pickle 继续失败；spawn 不隐式传播 live instance |
| Spawn/forkserver | 通过 config/path/opaque refs 构造 fresh instance；不接受 serialized live authority |
| Multiple forks | 每代 child 都有新 epoch；grandchild 不能复活 ancestor instance |
| PID fallback | 模拟 hook unavailable 后 PID drift 仍 fail closed |

SQLite store 还需同一真实文件的 parent/child fresh-instance contention 测试，证明支持的是独立
connection + durable CAS，而不是完全禁止多进程。

## 9. 交付顺序与 commit 边界

建议保持每个 commit 可运行的最小序列：

1. 文档：冻结 process inheritance threat model 与禁止 fork-after-init 的当前边界；
2. test：共享 PID/epoch helper contract；
3. feat：无锁 process identity foundation；
4. test/fix：`RequestContextIssuer` 全 public-path owner guard；
5. test/fix：protected-operation composer/registry/handle 与 issuer composition guard；
6. docs：授权/RequestContext migration、rollback、spawn/forkserver contract；
7. test/fix：`SQLiteInvocationAttemptStore` owner guard；
8. test/fix：`SQLiteEventStore` owner guard；
9. test/fix：`SQLiteArtifactStore` owner guard；
10. test/fix：`SQLiteProjectionOffsetStore` owner guard；
11. test/fix：`SQLiteRevocationRevisionGuard` owner guard；
12. test/fix：`InvocationRecoveryCoordinator` owner/dependency identity；
13. docs：SQLite per-process connection runbook 与 rollback；
14. test/fix：in-memory revocation guard 与 key ring production rejection；
15. test/fix：plugin/policy/upcaster/ledger state classification；
16. test/fix：runtime/Agent registry/adapters/publisher composition process binding；
17. docs：spawn/exec-before-secret-load service topology；
18. test：secret inheritance topology fixture，只使用 canary material；
19. feat：composition root preflight 拒绝 fork-unsafe dependency graph；
20. evidence：Python 3.9/3.12/3.13、platform/fork/spawn/forkserver retained matrix。

若 test-first commit 会使默认分支红，应在同一行为 commit 中先加入能失败的测试再完成修复；不允许
把红色提交留在正式历史。文档与实现仍分开提交，但文档不能提前宣称未实现保证。

## 10. 迁移、部署与回滚

### 10.1 首次启用

process guard 不需要数据库 schema migration，但属于行为兼容性变化：过去偶然在 fork child
“能运行”的代码将确定性失败。发布前必须：

1. inventory 当前服务/CLI/test runner 是否使用 preload + fork；
2. 将 worker 创建移动到 connection、secret、issuer、event-loop 初始化之前；
3. child 从显式 config/path/opaque reference 构造 fresh dependencies；
4. production preflight 输出固定 capability/status，不输出 PID/path/secret；
5. 先在 fake connector、synthetic tenant 下跑 fork/spawn matrix；
6. retained evidence 证明 parent continuity、fresh child store contention 与 bounded shutdown。

### 10.2 回滚

guard-only application rollback 可以回到前一 binary，但不能把“重新允许 inherited connection/
authority”当生产恢复方案。若新拓扑已切到 spawn/exec，可保持新拓扑并回滚业务 binary；不要为
兼容旧 binary 重新启用 fork-after-secret/store-init。

若发现 child 已经执行 inherited authorization/store/connector：

- 停止 admission；
- 将相关 attempt/effect 标记 unknown，而不是盲目重试；
- 轮换可能进入 child 的 credential/key；
- reconcile durable receipt、outbox、attempt、artifact 和审计记录；
- 必要时从已验证 backup restore-forward-fix；
- 在查清 duplicate effect 前不得重开真实 connector。

### 10.3 兼容声明

支持矩阵必须区分：

- POSIX fork available/tested；
- Windows/no-fork；
- Python multiprocessing `spawn`；
- `forkserver`（server 必须在 secret/store 初始化之前启动）；
- preload server model；
- container/sidecar/worker exec model。

“macOS 上 fork tests 通过”不证明 Linux production topology；“Windows 没有 fork”也不证明
spawn serialization 不会传播错误 authority value。

## 11. Gate 影响

本审计不关闭任何 Gate：

- Gate A 继续关闭，直到 request context、authorization、tenant/revocation/key state 与所有核心
  repository 有 current-process proof；
- Gate B 继续关闭，直到 composition root、fake connector、attempt/result/action receipt 和
  shutdown lifecycle 在 spawn/exec topology 下闭环；
- Gate C 继续关闭，直到实际 deployment/upgrade/rollback/restore 演练覆盖真实进程模型；
- Gate D/E 还需要 sandbox、KMS/HSM、capacity/soak、PostgreSQL/HA/continuous DR。

在这些证据完成前，禁止宣称：

- fork-safe SQLite store；
- child 进程是 credential-safe sandbox；
- process-local handle 在 fork 后自动失效；
- one-time authorization 等于 exactly-once effect；
- 多进程 Agent/runtime 已达到生产商用。

本报告关闭的是“fork/process inheritance 风险没有统一依赖图”这一设计缺口，不是对应生产风险
本身。
