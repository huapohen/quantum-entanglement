# E3 M4 Inactive Result Schema、Artifact Owner Transaction 与备份拓扑证据

- 证据日期：2026-08-29
- 执行分支：`mainline_continue_quantum_entanglement`
- 代码封板候选：`aef5f8be8a238b94e72674a428d7aaad29b68650`
- 固定 tree：`f72cd558c1091d6294caa80fbfd4cea167876327`
- 结论：**M4 的私有候选已闭合；migration 7 注册、Atomic Result Writer、Observed、Accepted、
  worker result acceptance、真实 IM outbound 仍全部关闭。**

## 1. 本节点解决了什么

M1–M3 已经冻结 stored-event envelope、reserved vocabulary fence 与 store-owned typed/raw-row
双路重算，但还没有结果图的数据库候选、备份对象集合，也没有办法让未来 Result Writer 在自己
持有的 EventStore transaction 内原子写入 Artifact。

M4 只补这三个基础条件：

1. 一个不会被默认 bootstrap 执行的 migration 7 候选；
2. 一个不会改变 active backup registry 的私有 topology profile；
3. 一个只能由 exact EventStore owner transaction handle 调用的私有 Artifact 批写原语。

它没有实现 result receipt/event pair/job-attempt-task terminal CAS，也没有产生任何可序列化或
可伪造的“已接受”权限。

## 2. 提交账本

| 提交 | 内容 | 保持关闭的边界 |
|---|---|---|
| `34aac7f` | 定义 inactive result storage schema | 只有文档 |
| `7e2dac9` | 使 schema 与 ADR 冻结语义一致 | 只有文档 |
| `68662b7` | 打包 `0007_invocation_results` up/down SQL | 未注册 |
| `3029ac0` | 定义 disabled domain migration descriptor | 不进入 active registry |
| `aa3d55e` | 冻结空库/非空库/降级候选拓扑 | 仅 isolated rehearsal |
| `74e50d8` | legacy runner 明确拒绝 inactive migration | 默认 bootstrap 仍停在 6 |
| `7038395` | 拒绝 hostile registry entry/`__eq__` 绕过 | 不接受 duck object |
| `3f31209` | 加固 event binding、publication 与 down guard | writer 仍不存在 |
| `caf9a34` | 冻结私有 backup topology | active backup registry 不变 |
| `48b0346` | 冻结有界、调用方脱离的 Artifact batch | 不写数据库 |
| `8be0e20` | 绑定不可复制的 owner transaction handle | 无公共 handle/API |
| `6e0e7fa` | 在 owner transaction 内写入并逐行回读 Artifact | 只写 Artifact 子图 |
| `61fb84f` | 增加 `os._exit` / SIGKILL crash rollback 证据 | 不开放 worker |
| `ea5973d` | 写失败后 owner 永久 rollback-only | 内部 catch 不能提交前缀 |
| `968cbe8` | 把 BEGIN/COMMIT 结果分类为固定异常 | 私有 BaseException 不外泄 |
| `32468eb` | 扩展前验证完整 Artifact version 历史 | 损坏历史不能继续生长 |
| `7fb71e4` | 保留 transaction control 的 ambiguity cause | clean control 不掩盖 reopen/reconcile |
| `aef5f8b` | 有界流式验证完整 Artifact version 历史 | 超大 TEXT 不在先验边界前物化 |

## 3. Inactive migration 7

### 3.1 六张候选表

```text
invocation_result_manifests
invocation_result_requests
invocation_result_event_bindings
invocation_result_receipts
invocation_result_artifacts
invocation_result_publications
```

统一的 `invocation_result_event_bindings` 把 result 与 terminal 两种角色绑定到 receipt。结构约束
拒绝跨角色/跨图复用 event ID 或 global position；每个完整 receipt 必须恰好拥有两条角色不同的
binding；publication 只能复合引用同 receipt 的 terminal event。

候选包含 6 张表、8 个显式索引，共 14 个 descriptor-owned objects；SQLite 另外产生 31 个
autoindex。Down guard 在以下任一状态拒绝删除并保持原库不变：

- 完整 result graph；
- 孤立 event binding；
- future sidecar/dependent object；
- 未显式清空的 publication/receipt/artifact/request/manifest。

### 3.2 默认关闭的机械证据

- active migration registry 仍只包含 1–6；
- `SQLiteEventStore` 默认启动仍停在 schema 6；
- legacy runner 对 7、duck object 和 hostile equality 全部在 mutation 前拒绝；
- old/default store reopen 隔离演练后的 schema 7 会失败关闭，不会假装兼容；
- isolated non-empty v6 → v7 rehearsal、raw reopen、完整图回读通过。

## 4. 私有备份拓扑

私有 profile 为 `qe.domain-migration-0007/1`：

| 项目 | 数值 |
|---|---:|
| 显式对象 | 14 |
| SQLite autoindex | 31 |
| profile 总对象 | 45 |
| profile digest | `402707d9ef31ce878b0556d85173de26b773b67259381ce7342298fe2ece8ffb` |
| known registry | 12 profiles / 133 objects |
| known registry digest | `2995f74bc5f5765fd4c75d283a2819c2856b191699e14c86857d146a8fb9548e` |

导入私有模块前后，active registry 始终是 11 profiles / 88 objects，digest 始终为
`39be33b24cdc79e6bd92ef4fdb5271963be724cf1a4762091d3336aa16e9a495`。包根、active backup
模块和 `__all__` 都不导入该私有 profile。

这只是未来 backup-v2 晋级所需的已知拓扑，不代表 active backup/restore 已经支持 result graph。

## 5. Artifact owner transaction

### 5.1 有界输入快照

- 最多 256 个 ordered candidates；
- 单批 Artifact content 总量最多 64 MiB；
- canonical metadata 总量最多 1 MiB；
- exact schema-2 candidate、同 scope、唯一 Artifact ID/idempotency/head coordinate；
- 内容、metadata bytes、descriptor、candidate digest 与 ordinal 全部复制后再次验证；
- raw content/metadata 不进入 batch `repr`。

### 5.2 不可伪造的 owner handle

Handle 绑定 exact store、exact `sqlite3.Connection`、process owner 和一次性 generation。它不能
copy、deepcopy、pickle 或 reduce，退出 owner context 后立即失效。手工 `BEGIN`、foreign store、
过期 generation、嵌套 owner、构造器 token 猜测和真实 fork 继承都不能获得写权限。

### 5.3 DML 与回读合同

整批 identity/head/blob 在首条 DML 前预检。每条写入随后执行：

1. blob `ON CONFLICT DO NOTHING`；
2. 回读 digest/content/size/length/SQLite storage class/UTC 微秒时间；
3. version insert；
4. 固定 16 列 exact `sqlite3.Row` 回读；
5. metadata TEXT 重新编码与 canonical bytes 精确比较；
6. `changes()` 与 `total_changes` 对账。

既有 version 历史在扩展前完整验证，但不再用无界 `fetchall()`。轻量预检 cursor 每次最多读取
64 行，只投影 `rowid`、三个整数、`typeof(...)` 与 `length(CAST(... AS BLOB))`。13 个 TEXT 字段先
在 SQLite 层验证 storage class 和 byte bound：identity 为 4,096 bytes，media type 为 255 bytes，
blob digest 为 71 bytes，canonical metadata 为 65,536 bytes，UTC 微秒时间为 27 bytes，request
digest 为 64 bytes。只有预检通过后才按 `rowid` 单行读取固定 16 列，继续验证 scope、连续
version/parent、canonical metadata、重算 request digest 与 UTC 时间。`BEGIN IMMEDIATE` 和 store
lock 消除了预检与单行物化之间的外部写入窗口。中间 lineage、gap、历史 timestamp/metadata
storage drift 与异常大 metadata 都在新 blob/version DML 前失败。

主库或 TEMP schema 中只要存在针对 Artifact 表的 trigger，就在 DML 前拒绝。SQLite progress
handler 在每条 VM 指令处复核 process owner，因此即使 process epoch 在 trace/SQL 边界切断，首条
DML 也会被 interrupt；真实 fork 继承的 active handle 在触碰 SQLite 前拒绝。

### 5.4 失败与崩溃

- 第二个 Artifact 已写 blob、尚未写 version 时注入失败，即使内部调用者吞掉固定错误，owner
  也被标成 rollback-only，正常退出会强制 rollback；
- BEGIN 失败、COMMIT 失败且 rollback 已确认，固定重发 `_ResultArtifactTransactionError`；
- COMMIT/ROLLBACK 都无法确认时，固定重发 `_ResultArtifactCommitAmbiguityError` 并 poison store；
- body 控制信号在 rollback 已确认时按原 exact 类型干净重发且无 cause；rollback 无法确认时仍按
  原 exact 控制类型干净重发，但直接 cause 固定为 `_ResultArtifactCommitAmbiguityError`，提醒调用方
  必须关闭、重开并 reconcile；
- 私有 `_EventStoreAdmissionTransactionSignal` 不会越过 owner API；
- 固定错误重发前清除 content-bearing internal traceback frames；
- 后续 owner body 抛错会回滚整批；
- 独立 spawn process 在写后直接 `os._exit(73)` 或由父进程 SIGKILL，重开数据库均为 0 blob / 0
  version，`PRAGMA integrity_check=ok`。

## 6. 验证结果

在 macOS、CPython 3.12.12、本仓锁定环境执行：

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_inactive_invocation_results_migration.py \
  tests/test_inactive_invocation_results_backup_topology.py \
  tests/test_migrations.py \
  tests/test_result_artifact_transaction.py
# 82 passed

PYTHONPATH=src .venv/bin/pytest -q
# 2639 passed

.venv/bin/ruff check .
.venv/bin/mypy src
git diff --check
# 全部通过
```

全仓只出现已有且预期的 macOS multi-threaded `fork()` DeprecationWarning，没有测试失败、未捕获
线程/进程异常或 secret 输出。

## 7. 独立复核

只读 reviewer 首轮复现了三个高风险，而不是仅依赖绿色测试：

1. owner 内吞掉第二项错误后可提交 `2 blobs / 1 version`；
2. authorizer 拒绝 COMMIT 时私有 BaseException signal 可外泄；
3. v1 `created_at=not-a-timestamp` 后仍可追加 v2。

三个问题分别由 `ea5973d`、`968cbe8`、`32468eb` 修复，并形成精确反例回归。复核随后又发现两处
高风险：ambiguous transaction 携带控制信号时丢失 ambiguity cause，以及完整历史使用无界
`fetchall()` 并在 Python 物化后才检查 metadata 大小。它们分别由 `7fb71e4` 与 `aef5f8b` 修复。

最终只读 reviewer 在代码封板 `aef5f8b` 上复跑 42 项 Result Artifact 专项、Ruff、Mypy 与
`git diff --check`，结论为 **0 blocker**。复核明确确认：

- confirmed rollback 的 clean control 无 cause，ambiguous control 以
  `_ResultArtifactCommitAmbiguityError` 为直接 cause；
- history preflight 每批硬上限为 64，超限 TEXT 不会先进入 raw-row materialization；
- `BEGIN IMMEDIATE` 与 store lock 排除外部 writer 的 TOCTOU；
- 游标正常/异常清理没有阻断性泄漏，process mismatch 时不触碰继承 SQLite 对象是既有刻意边界。

这只证明 M4 私有候选达到进入 M5 的安全停点，不把 M4 描述为 active production capability。

## 8. 仍未实现

- migration 7 active registration；
- result request/receipt/event pair 的同事务 writer；
- job/attempt/task terminal CAS；
- result publication/outbox 写入；
- complete graph read、replay、ACK-loss recovery；
- `ObservedV2`；
- fresh-COMMIT-only `AcceptedV2`；
- worker result acceptance；
- UI/API result authority 分类；
- 真实 IM provider/outbound。

下一唯一串行节点是 Phase 5：Atomic Result Acceptance Writer。它必须复用这里的 owner transaction
和 rollback/ambiguity 分类，不能注册 migration 7，也不能直接开放 worker。
