# M1 Canonical Stored-Event Envelope Codec 实现与验证证据

- 记录日期：2026-08-28（Asia/Shanghai）
- 执行分支：`mainline_continue_quantum_entanglement`
- 代码封板提交：`d889751e4cc3b7db548994a000a87e21688b4429`
- 代码封板 tree：`57b608ed57f47a68d1f9433104cd88d820a19929`
- 设计依据：[`ADR_0005_ATOMIC_RESULT_AUTHORITY.md`](../../docs/production/ADR_0005_ATOMIC_RESULT_AUTHORITY.md)
- 阶段结论：**M1 codec primitive 已完成；M2 reserved fence 与 M3 真实 store adapter 尚未开始**
- 发布结论：**不是生产晋级；Gate A–E 全部关闭**

## 1. 本阶段究竟完成了什么

本阶段实现了一个私有、domain-separated、capability-free 的 stored-event envelope V1 codec。
它解决的是一个窄而关键的问题：给定已经冻结的一组 event row 值，或者给定一个 exact
`sqlite3.Row` 原始投影，能否得到完全相同、可跨 Python 版本复算的 canonical bytes 与 digest。

```mermaid
flowchart LR
    A[冻结的标量与 canonical payload_json] --> C[私有 V1 codec]
    B[exact sqlite3.Row<br/>固定 11 列与顺序] --> C
    C --> D[12 字段 canonical JSON body]
    D --> E[SHA-256<br/>domain || body]
    E --> F[capability-free digest]
```

当前的两个入口都只是私有 codec primitive：

- `_stored_event_envelope_from_values(...)` 接受 exact scalar values；
- `_stored_event_envelope_from_raw_row(...)` 只接受 exact `sqlite3.Row` 和固定列顺序。

它们尚未接入 `SQLiteEventStore._EventWriteSnapshot`，也没有在真实 INSERT 所属事务中执行
write-snapshot/raw-row 双路比对。因此本阶段没有形成 durable acceptance authority，更没有开放
writer、`AcceptedV2`、worker、migration 7 或真实 IM。

## 2. 冻结的 V1 合同

### 2.1 Body 字段

V1 body 固定为 12 个字段：

```text
schemaVersion
eventId
streamId
eventType
actorId
timestamp
correlationId
causationId
idempotencyKey
payload
sequence
globalPosition
```

`schemaVersion` 由 codec 固定为 exact integer `1`；codec 没有接收任意 envelope wire bytes 的
public decoder，因此调用者不能把 future schema 注入到当前构造路径。

### 2.2 Domain 与 digest

```text
domain = quantum-entanglement.stored-event-envelope/1\n
digest = SHA-256(UTF8(domain) || canonical-json-body)
```

Golden 使用中性 `codec.golden.checked` event type，避免在 writer 尚不存在时伪装成
`task.invocation.result.accepted` 业务事实：

| 项目 | 固定值 |
| --- | --- |
| fixture | `tests/fixtures/stored_event_envelope/v1/envelope.json` |
| body bytes | 372 |
| digest | `a7a2a28ed93454fe925dbdf676acd6bf758b9c5ac7afc50eeeae867d3d08e538` |
| manifest | `tests/fixtures/stored_event_envelope/v1/manifest.json` |
| verifier | `scripts/verify_stored_event_envelope_v1_golden.py` |

### 2.3 Canonical 与 exact-type 规则

- payload storage 必须是 exact Python `str`，对应 SQLite TEXT；根必须是 exact JSON object；
- canonical JSON 使用 UTF-8、`ensure_ascii=False`、sorted keys、无额外空白；
- duplicate key、NaN、Infinity、数组/标量根、escape 差异、key reorder 和首尾空白全部拒绝；
- `sequence`、`globalPosition` 必须是 exact positive signed-64-bit `int`，bool/subclass 不可冒充；
- timestamp 必须是有效的 `YYYY-MM-DDTHH:MM:SS.ffffffZ`；
- identity text 必须是 exact UTF-8/NFC、无首尾空白、无 C0/DEL，并受 4,096-byte 上限约束；
- payload 继续按当前 EventStore 合同使用 512-character key、65,536-character string、64 层、
  10,000 nodes、4,096-bit integer 和 1 MiB encoded bytes 上限；
- value 使用 Python 3.9 可加载的手写 slots，没有 `__dict__`，repr 固定且不含正文；
- package `__all__` 不导出类型或 factory，events 表也没有新增 digest 列。

## 3. Raw SQLite row 独立重算

raw-row 路径不使用 `StoredEvent.to_dict()` 或其他 read model。它要求下面 11 列按 exact 顺序出现：

```text
global_position, stream_id, sequence, event_id, event_type, actor_id,
timestamp, payload_json, correlation_id, causation_id, idempotency_key
```

测试使用内存 SQLite 的明确 `SELECT ? AS ...` 投影生成真正的 `sqlite3.Row`。缺列、增列、交换
顺序、dict/tuple/duck mapping、TEXT→BLOB/INTEGER、INTEGER→TEXT/REAL 和 hostile SQLite converter
返回的 `str` subclass 都在读取其自定义方法前失败关闭。11 个 raw column 分别做合法 tamper 后，
digest 均与 Golden 不同。

这证明“codec 可以独立处理 raw row”，不等于“M3 已接入生产 store”。M3 仍必须在 owning
transaction 内使用同一明确列清单执行 SELECT，并和真实 store-owned write snapshot 比对。

## 4. 对抗审查发现与修复

### 4.1 Python 3.9 超长数字 CPU 风险

最初实现让 `json.loads` 先把任意长度十进制文本转换成 `int`，再检查 4,096-bit 上限。Python
3.9 没有新版本解释器的全局十进制位数保护，接近 1 MiB 的整数可能先消耗大量 CPU。

修复后，`parse_int` 在 `int()` 前限制最多 1,234 个十进制数字；`parse_float` 也先限制 lexeme
长度并拒绝溢出为非有限值。测试用 100,000 位整数和浮点尾数证明在 Python number conversion
前拒绝，而不依赖不稳定的计时阈值。

### 4.2 廉价字段必须先于 payload

构造顺序改为先验证 identity、timestamp、sequence 与 global position，再解析 payload。测试以
替换 `_canonical_payload` 的 forbidden callback 证明 cheap scalar 已经非法时不会进入 JSON 解析。

### 4.3 错误消息不得带出 payload key/value

递归验证不再把原始 payload key 拼进 exception path。secret-shaped key canary、value canary 和
surrogate 均不进入 `str(error)`；repr 固定为
`_StoredEventEnvelopeV1(<capability-free>)`，也不含 event、payload、credential、lease 或 narration。

### 4.4 反射篡改的诚实边界

普通赋值和 instance method shadow 被 slots/`__setattr__` 拒绝；exact class-qualified method 又会
重新 snapshot 与校验。Python 的 `object.__setattr__` 仍可把私有 slot 改成另一份合法 canonical
值，此时 digest 必须随之改变，不能维持原 digest。测试明确覆盖了这一点。

因此 envelope 的安全语义不是“进程内恶意反射不可更改”，而是“任何改变都必须重新形成另一份
capability-free digest”。未来 writer 不能信任调用者提供的 envelope/digest；它必须从 store-owned
snapshot 与 raw row 独立重算并比较，durable authority 只能来自完整 transaction 与 fresh COMMIT ACK。

## 5. 验证矩阵与结果

### 5.1 Codec 专项

两个专项文件共收集 102 tests，覆盖：

- exact body/bytes/domain/digest；
- 11 个 row 字段和三个 payload leaf 的 mutation；
- exact types、bool-as-int、signed-64、UTC 微秒、NFC/control/surrogate；
- duplicate、nested duplicate、NaN/Infinity、whitespace、ordering、escape、非 object root；
- depth/node/key/string/integer/total-byte bounds；
- exact raw SQLite row、列 inventory/order、所有 storage class drift；
- subclass、hostile converter、malformed `object.__new__`、method shadow 与 reflective mutation；
- detached `to_dict()`、safe repr、private export、只读 verifier 和无 `--write` 模式。

### 5.2 三 Python Golden

同一个只读 verifier 在以下解释器上输出同一 372-byte body 与 digest：

| Python | 结果 |
| --- | --- |
| CPython 3.9.6 (`/usr/bin/python3`) | 1/1 vector verified |
| CPython 3.12.12 (`uv run --python 3.12`) | 1/1 vector verified |
| CPython 3.13.9 (`/Users/lwblx/anaconda3/bin/python3.13`) | 1/1 vector verified |

同时修复了两个会让 Python 3.9 正常 package import 在 codec 加载前失败的运行时 PEP 604 type alias。
该修复合理改变了受保护的 provider-bundle suite source digest，已独立刷新到
`a14ef986…a50368` 并通过 fresh-process/hash-seed verifier。

CI 新增一个无第三方依赖的 3.9/3.12/3.13 Golden job；未来 pull request 或 main push 会继续阻止
跨解释器漂移。本批三版本只声明 Golden verifier 通过，不把 3.9/3.12 扩大为本批全仓测试结论。

### 5.3 全仓与静态门禁

在代码封板提交 `d889751` 上：

| 门禁 | 结果 |
| --- | --- |
| Python 3.13 full pytest | 2,489 passed，79 个既有 fork deprecation warnings |
| Ruff 0.16.3 `check .` | passed |
| Mypy 1.19.1 `--strict src` | 66 source files passed |
| `git diff --check` | passed |

第一次 full pytest 曾出现 1 个 provider-bundle verifier 失败。原因不是网络或随机波动，而是
Python 3.9 compatibility fix 修改了其 digest-covered 源文件；刷新 exact suite digest 后 focused
复核与第二次 full pytest 全绿。该失败没有被忽略或归类成偶发。

## 6. 明确没有完成的内容

以下内容仍全部关闭：

1. **M2 reserved fence**：generic `append*` 尚未封锁 result accepted/terminal reserved payload；
2. **M3 store adapter**：尚未把 codec 接入 exact `_EventWriteSnapshot`，没有同事务 raw-row SELECT；
3. **typed result payload**：M1 只验证 generic canonical JSON；未来 adapter 必须先调用 exact
   result-evidence/terminal codec，再构造 envelope；
4. **历史兼容**：现有 generic EventStore 允许更宽的 timestamp/text；本 codec 不能直接宣称可以
   回算所有历史 event row；
5. **容量合同交叉**：EventStore payload leaf 是 512/65,536 characters，而 result metadata codec
   有自己的 UTF-8 byte bounds；M3 必须冻结 reserved event 的共同可持久化交集，不能静默截断；
6. **可配置 store 上限**：codec 固定 1 MiB；M3 必须验证 writer store 配置或为 reserved result
   path 冻结一致上限；
7. **authority**：没有 result migration 7、receipt row、Artifact same-transaction primitive、writer、
   Observed readback、Accepted mint、worker dispatch 或 production promotion；
8. **真实 IM/outbound**：没有连接真实 endpoint，没有读取 credential material，没有发送消息。

## 7. 下一步与停止线

M1 是一个可以安全停下评审的节点。下一步只能进入 M2：先在任何专用 writer 出现前，封锁
generic append 旁路对 reserved result/terminal 事件的伪造。M2 完成前不得把 codec 接入 public
writer；M3 完成前不得声称 write snapshot 与 durable raw row 已闭环；M7 以前不得创建
`AcceptedV2`；独立 migration/worker promotion 以前不得启用执行。

分支仍不合并 `main`。用户人工验收前，`mainline_continue_quantum_entanglement` 只作为已推送的
独立评审尖端；Notion 必须在本地/GitHub 文档封板后同步并逐页回读。
