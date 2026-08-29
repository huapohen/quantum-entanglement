# 47｜PostgreSQL native IM inbox 与 canonical event 原子桥接

> 状态：本地实现检查点；未同步 Notion。  
> 代码分支：`dev_wanwork_quantum_entanglement`  领域提交：`f24786a`  
> 证据边界：本文件证明代码合同、离线测试与可选 PostgreSQL 集成测试路径，不宣称真实融云、Clerk 或生产部署已完成。

## 1. 为什么必须有一个原子边界

仅有 `NativeIMInboxStore.Admit` 和 `Store.AppendBatch` 两个独立调用时，下面两种中间态都可能出现：

1. inbox receipt 已提交，但 canonical event 因 revision conflict、进程崩溃或连接故障没有提交；
2. event 已提交，但 inbox receipt 没有提交，重试会重新触发上游路由。

这两种状态都不能交给 `@Agent`、message projection 或任务调度器自行猜测。transport admission 与平台
事实必须共用一个 PostgreSQL transaction，并由同一个 commit acknowledgement 决定是否向下游放行。

## 2. 当前实现

### 2.1 显式 bridge input

`events.InboxEventProjection` 要求 verified envelope 之外显式提供：

- schema version、stream、event type、actor、occurred time；
- correlation/causation、idempotency key、traceparent；
- expected stream version。

`Event()` 会重新构造完整 `EventToAppend`，重新计算 canonical digest，并要求它与
`InboxEnvelope.EventDigest` 完全相等。任一业务字段变化都会在进入数据库前失败。

### 2.2 PostgreSQL 原子适配器

`eventstore.NativeIMAtomicStore.AdmitAndAppend` 的顺序固定为：

```text
verified InboxEventProjection
  -> serializable transaction + exact tenant GUC
  -> admit_native_im_inbox()
  -> inserted: write_event() / replayed: exact event readback
  -> inbox receipt + event readback
  -> one commit acknowledgement
```

inbox 与 event 使用同一连接、同一事务，不调用两个独立 store 的公开方法。首次 admission 若 event append
失败，事务整体回滚；之后读取 inbox 必须得到 `ErrInboxNotFound`。replayed inbox 若找不到完全对应的 event，
返回 `ErrInboxEventInconsistent`，不会根据调用方传入的 revision 擅自“修复”历史状态。

### 2.3 commit-unknown

commit acknowledgement 丢失时，当前连接会被 hijack 并关闭，不回收到 pool。系统只在 fresh connection 的
read-only transaction 中同时观察到精确 inbox receipt 和精确 event 后，才返回：

```text
Inbox.Status = replayed
Inbox.ResolvedAfterUnknown = true
Append.Replayed = true
```

只找到一侧、digest 漂移、readback 失败或 fresh commit 仍不确定时，保持 unknown/inconsistent，不触发下游
副作用，也不把 unknown 伪装成 inserted。

## 3. 验证矩阵

已通过的本地验证：

```bash
cd apps/im-api
go test ./...
```

额外合同覆盖：

- projection 缺字段、digest 漂移和每个 immutable header 变更均 fail closed；
- atomic store nil/runtime-pool 构造边界；
- PostgreSQL 集成测试（设置 `WANWORK_TEST_POSTGRES_ADMIN_URL` 时）覆盖首次插入、精确 replay、
  revision failure 的整笔回滚，以及 inbox/event 同时读回；
- 原有 inbox commit-unknown fresh-readback、event store、projection checkpoint 和全量 Python native IM
  zero-network/golden 门禁保持不变。

## 4. 仍然不是生产接入许可

该桥接只闭合数据库内的 inbox→event 一致性，不代表：

- callback 签名、nonce/replay window、Clerk trusted context 已完成；
- conversation、sender、message segments、mention、child thread 或 Agent Store 已接入；
- RongCloud outbound、群组创建/邀请/发送已授权；
- PostgreSQL production ownership/cutover、restore/kill-9/HA 证据已完成。

真实 provider 仍保持关闭；下一步要在该 bridge 之上补 trusted request context、完整消息身份与 outbox/action
receipt，而不是从 transport callback 直接调用 Agent。
