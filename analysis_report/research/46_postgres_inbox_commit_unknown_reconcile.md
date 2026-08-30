# 46｜PostgreSQL native IM inbox：commit-unknown 与 admission reconcile

> 状态：本地实现检查点；未同步 Notion。
> 代码分支：`dev_wanwork_quantum_entanglement`  领域提交：`2c208e6`
> 证据边界：本文件证明代码合同与离线测试结果，不宣称真实融云送达、Clerk 鉴权或生产部署已完成。

## 1. 问题与风险

`NativeIMInboxStore.Admit` 会先调用固定 PostgreSQL admission function，再提交事务。提交返回错误时，客户端无法仅凭错误判断服务器是否已经提交：

- 明确的 `pgx.ErrTxCommitRollback`、serialization failure（`40001`）或 deadlock（`40P01`）代表已知回滚/不可提交路径；
- 其他 commit 错误可能发生在服务端提交之后，若直接当作“未写入”重试，可能重复触发后续路由；
- 若把发生过写入的连接放回池，未清理的事务/会话状态可能污染下一次租户操作。

旧实现将所有 commit 错误压成 `ErrInboxStoreUnavailable`，没有给调用方可审计的 unknown/reconcile 边界。

## 2. 当前实现

### 2.1 显式 admission 结果

`events.InboxAdmission` 增加 `ResolvedAfterUnknown`。它只有在 fresh readback 找到完全相同的 `(scope,eventId,eventDigest,payloadDigest)` 时为 `true`，并且状态固定为 `replayed`；reconcile 不会伪造 `inserted`。

`events.InboxAdmissionReconciler` 是可选能力接口，使上层可以在收到 `ErrInboxCommitUnknown` 后显式调用 reconcile，而无需让所有内存 fake 假装拥有持久化证明。Memory fake 也实现同一合同，便于协议测试。

### 2.2 unknown 路径

1. `NativeIMInboxStore` 通过可注入的 `commitHook` 提交，生产构造器绑定真实 `transaction.Commit`；测试可以在提交成功后模拟 ACK 丢失。
2. 明确回滚错误保持原有不可用错误，不启动 readback。
3. 其他 commit 错误立即 `Hijack` 并关闭连接，禁止回池。
4. 在独立的 bounded background context 中通过 `Reconcile` 获取新连接，以完整 envelope 做 scope、event digest 和 payload digest 比对。
5. 找到精确行时返回 `replayed + ResolvedAfterUnknown=true`；找不到、漂移或 readback 失败时返回 `ErrInboxCommitUnknown`，不调用下游路由。

### 2.3 并发隔离

Inbox admission 不需要跨多张表的 serializable 事务。事务改用 `READ COMMITTED`，唯一键和函数内的 `INSERT ... ON CONFLICT DO NOTHING` 负责同一 scope/event 的互斥；冲突后的新 statement 可以观察已提交行，再按 digest 返回 replay 或 conflict。这样避免两个相同事件并发时把正常重放误报成 `40001`。

## 3. 验证

已通过：

```text
go test ./internal/events ./internal/platform/postgres/eventstore
```

集成测试（设置 `WANWORK_TEST_POSTGRES_ADMIN_URL` 后运行）增加了“提交成功但 ACK 丢失”的注入场景：必须从新连接 readback，返回 replayed 且 `ResolvedAfterUnknown=true`。没有该环境变量时不会把跳过误报为通过。

现有 native IM Python 专项门禁仍保持通过：23 个 golden vectors、zero-network gate 以及全部 `tests/test_native_im_*.py`。

## 4. 仍未闭合的边界

本阶段没有解决以下问题：

- `EventDigest` 仍是 provider-neutral envelope 的输入摘要，Go DB 函数还不能独立重算完整业务 envelope；
- `InboxEnvelope` 尚未携带 conversation、sender、message segments、traceparent 等 bridge 所需业务身份；
- verified envelope 到 Go `events.EventStore`/chat router 的原子 projection bridge 尚未实现；
- callback 签名、nonce、cursor、Clerk trusted context 和真实 RongCloud adapter 仍保持关闭；
- provider outbound、Action receipt、effect-unknown/reconcile 和真实网络 E2E 仍为 No-Go。

因此，本检查点只把“数据库提交 ACK 不确定”从隐式失败提升为可观测、隔离、可恢复的合同，不改变“平台事实先于 IM/Agent 路由”的总原则。
