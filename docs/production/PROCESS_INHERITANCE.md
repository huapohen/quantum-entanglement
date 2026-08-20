# 进程身份、fork 继承与 worker 拓扑合同

状态：**基础层已实现；任何现有 store、授权器、secret provider、runtime 或 connector 均尚未因此
自动变成 fork-safe，相关生产 Gate 继续关闭。**

本文定义 `quantum_entanglement.process_identity` 的已实现合同，以及后续组件接入时必须保持的
顺序、失败语义、迁移和回滚边界。完整组件风险清单见
[`12_process_inheritance_dependency_audit.md`](../../analysis_report/research/12_process_inheritance_dependency_audit.md)。

## 1. 解决的问题

POSIX `fork()` 会复制当前地址空间。复制后的 Python 对象外观看似仍可调用，但它可能持有：

- 由已消失线程持有的 `Lock`、`RLock`、`Condition` 或 event-loop 状态；
- 不允许跨 fork 复用的 SQLite connection、cursor、transaction 或 file descriptor；
- 已分叉的 lease、nonce、revision、clock high-water 或 one-time registry；
- provider、connector、plugin、callback 和外部 runtime handle；
- 已经复制到 child 地址空间的明文 secret 或签名材料。

不可复制或不可 pickle 只能阻止显式对象传输，不能阻止 `fork()` 继承。本基础层给已构造实例
提供一个统一的、无锁的 creator-process identity，使 child 可以在触碰继承锁、connection 或
provider **之前**稳定拒绝旧实例。

它不解决已复制 secret 的回收，也不把任何底层资源改造成可继承资源。

## 2. 已实现 API

该模块故意不从 package 顶层导出。它是组件实现使用的内部安全基础，不是调用者可自行声明
ownership 的业务 API。

```python
current_process_identity() -> tuple[int, opaque_epoch]
capture_process_owner() -> opaque_owner
require_current_process(owner, error_factory) -> None
```

### 2.1 `current_process_identity`

- identity 同时包含 exact built-in `int` PID 与按 object identity 比较的 opaque epoch；
- 同一未旋转进程中返回同一 tuple 对象；
- `os.register_at_fork(after_in_child=...)` 可用时，child hook 只替换一个 module-global tuple；
- hook 不遍历实例、不拿锁、不关闭 descriptor，也不调用业务依赖；
- 即使 hook 不可用、注册失败或未运行，每次读取时的 PID drift 检查仍会独立旋转 epoch；
- import reload 和显式 epoch rotation 会使旧 owner 失效，即使 PID 未变化；
- epoch 的字符串表示固定为 opaque，占位对象拒绝 deepcopy 和 pickle。

只检查 PID 不足以表达同 PID epoch rotation；只检查 epoch 又不能覆盖 hook 失败。因此接入组件
不得删除任一比较维度。

### 2.2 `capture_process_owner`

- 组件只在自己的 constructor 内调用；
- 返回的 descriptor 不公开 PID/epoch 属性，`str`/`repr` 固定且不含 PID；
- descriptor 拒绝 copy、deepcopy 和 pickle；
- 使用 private construction token；任意普通对象、旧模块实例或绕过初始化的 descriptor 均在
  guard 中 fail closed。

这不是抵抗任意 trusted-host Python 代码 introspection 的 sandbox。已经能任意执行受信任进程
代码的攻击者不在该 helper 的保护边界内。

### 2.3 `require_current_process`

- 对 owner PID 只在确认是 exact built-in `int` 后做 primitive comparison；
- epoch 只用 `is` 比较，不调用 caller-controlled equality 或 truthiness；
- guard 自身不使用 mutex；
- current owner 不调用 error factory；
- mismatch 才调用一个无参数 factory，并抛出其新建的稳定异常；
- 非 descriptor、未初始化 descriptor、PID mismatch 和 epoch mismatch 使用同一个调用方错误，
  不提供可枚举的失败细节。

`error_factory` 必须是 module-level、无状态、不捕获 `self`/request/provider/store/secret 的函数，
每次返回一个新的、只含固定 machine-readable code 的异常。不得返回缓存异常，也不得预挂
cause、context、traceback、notes 或任意业务对象。

## 3. 组件接入不变量

持有进程本地状态的类按以下模式接入：

```python
from .process_identity import capture_process_owner, require_current_process


def _store_process_mismatch() -> BaseException:
    return StoreLifecycleError("store_process_mismatch")


class ExampleStore:
    def __init__(self, path: str) -> None:
        self.__process_owner = capture_process_owner()
        self.__lock = threading.RLock()
        self.__connection = sqlite3.connect(path)

    def read(self) -> object:
        require_current_process(self.__process_owner, _store_process_mismatch)
        with self.__lock:
            return self.__connection.execute("SELECT ...").fetchone()

    def close(self) -> None:
        require_current_process(self.__process_owner, _store_process_mismatch)
        with self.__lock:
            self.__connection.close()
```

强制顺序：

1. constructor 自行 capture；不得接受 caller 提供的 owner；
2. 每个 public read/write/claim/heartbeat/commit/rollback/close/enter/exit path 第一项业务动作就是
   guard；
3. guard 必须早于 `closed` flag、锁、clock、connection、provider、authorizer、registry、callback
   或 input object 的 property/repr；
4. child mismatch 后不得 best-effort close、rollback、retire、wipe 或 drain 继承资源；
5. `__exit__` 通过受 guard 的 `close()`，不能在 child 静默成功；
6. finalizer 不得在无法证明 current owner 时触碰继承资源；生产资源不能依赖 finalizer 关闭；
7. constant `repr` 若完全不读依赖状态可以不抛 mismatch，但不得因此提供任何 lifecycle 操作；
8. wrapper/composer 不能只保护自己；所有 live authority/provider/store 依赖必须证明属于同一
   current identity，防止 fresh child wrapper 包住 inherited dependency；
9. public lifecycle mismatch 的最终异常清理仍由组件负责，必须验证 traceback locals 不可达
   caller/provider/store/secret graph。

## 4. 支持与禁止的进程拓扑

| 拓扑 | 合同 |
|---|---|
| 单进程 | owner 在整个未旋转 epoch 内有效 |
| fork 后调用 inherited instance | 必须在任何继承状态前拒绝 |
| fork 后创建 fresh instance | 允许；组件仍需满足自身 connection/provider 初始化合同 |
| spawn/exec child | 从 config、path、`SecretRef` 或其他非授权 reference 创建 fresh graph |
| forkserver | server 必须在 store/provider/secret/runtime 初始化前启动；worker 内 fresh compose |
| preload + fork live service graph | 禁止 |
| parent 先读取 secret 再 fork | 禁止；guard 不能擦除 child 已复制的 bytes |
| fork during open transaction | child 拒绝且不 inspect/rollback；parent 独自完成 recovery decision |
| nested fork | 每一代都有新 epoch；grandchild 不得复活 parent 或 child owner |

同一个 SQLite 文件可由不同进程的 fresh connections 按 WAL、busy timeout 和 durable CAS 合同
协调。这与“继承同一 `sqlite3.Connection`”是两个不同命题；后者始终禁止。

## 5. 失败语义与可观测性

组件公开的 mismatch code 必须固定，例如 `store_process_mismatch`，且不得包含：

- parent/child PID、epoch identity 或内存地址；
- tenant、workspace、request、handle、lease 或 nonce；
- 数据库路径、SQL、provider/connector 名称或配置；
- secret、credential、原 driver/provider exception；
- 调用对象的动态 `repr`。

可观测性只记录 allowlisted event name 和稳定 code。PID 可以由受控系统 telemetry 单独采集，
不能拼接进面向调用方的异常。guard 不吞 `KeyboardInterrupt`、`SystemExit`、`GeneratorExit` 或
async cancellation；组件的 clean-rethrow 层也必须保留安全的 process-control 语义。

## 6. 验证矩阵

基础层测试当前保留以下证据：

- current-process identity 稳定与 exact primitive 类型；
- registered at-fork hook 在 child 第一次 guard 前旋转 identity；
- 动态模块加载时注入 `register_at_fork` 注册失败，并由独立 PID drift fallback 在真实 fork child
  完成拒绝；
- 同 PID epoch rotation 与 import reload 使旧 owner 失效；
- 真实 fork child 拒绝 inherited owner，parent 随后仍可继续使用；
- fork 时另一个线程持有无关 lock，child 在两秒门限内完成拒绝；
- child 可 fresh capture，nested grandchild 同时拒绝 parent/child owner；
- owner/epoch 的 opaque representation、copy/deepcopy/pickle 拒绝；
- invalid/uninitialized descriptor 只产生无 cause/context 的稳定 mismatch；
- spawn 与可用时的 forkserver 进程 fresh capture，不传输 live owner。

平台没有 `os.fork` 时真实 fork suite 明确 skip；这不能记录成 fork 证明。每个接入组件仍需增加
自己的全 public-path、fork-while-component-lock、open transaction、parent continuity、error graph
和 fresh-instance durable contention 测试，基础层测试不能替代组件证据。

## 7. 迁移顺序

建议保持每笔提交独立可运行：

1. SQLite event/artifact/projection/revocation/attempt store；
2. recovery coordinator 与 publisher；
3. request-context issuer、operation registry 和 authorizer composition；
4. in-memory revocation/key lifecycle 与 `SecretMaterial` 的明确 process contract；
5. plugin/policy/upcaster/artifact in-memory registry；
6. agent registry、orchestrator、runtime adapter、connector 和 composition root preflight；
7. fork/spawn/forkserver retained matrix 与生产 worker topology runbook。

每一项都先提交行为与测试，再单独提交 migration/rollback 文档，最后更新 readiness/evidence。
不得用一次机械全仓加 guard 替代逐组件 public-path 审计。

## 8. 兼容、部署与回滚

基础层没有数据库 schema、event schema、backup manifest 或 wire-format 变化。未来组件接入属于
行为收紧：过去偶然能在 fork child 调用的 inherited instance 会改为固定错误。

部署前：

1. inventory 是否使用 Gunicorn/uWSGI preload、`multiprocessing` fork、forkserver 或自建 fork；
2. 将 worker 创建移动到 secret/store/provider/event-loop 初始化之前；
3. 在最终 worker 内构造 fresh dependency graph；
4. 用 synthetic tenant、fake connector 和临时数据库跑 retained process matrix；
5. 观察 mismatch 指标，确认不存在依赖旧继承行为的合法流量后再晋级。

回滚可以回到上一可运行 binary 和同一 schema，因为 foundation 不迁移数据；但回滚方案只能是
恢复到 fork-before-initialization 的旧部署拓扑。**禁止**通过删除 guard、重新允许 inherited live
authority/connection/secret 来恢复服务。若生产仍依赖 preload + fork，应停止晋级并修复拓扑。

## 9. 当前明确未完成

- 尚无现有生产组件接入 owner guard；
- 尚无 composition-root dependency graph 同 identity preflight；
- 尚无 secret-before-fork 防复制的部署实现或 retained evidence；
- 尚无 Linux production runner 的完整 fork/spawn/forkserver matrix；
- 尚无每个 SQLite store 的 fresh-connection 多进程 contention 证明；
- 尚无 plugin/runtime/connector 的隔离 worker；
- Gate A–E 没有因本基础层实现而关闭。

因此本阶段只能表述为“统一 process identity foundation 已实现并通过本地矩阵”，不能表述为
“系统已 fork-safe”“secret 已隔离”或“多进程生产可用”。
