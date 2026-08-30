# 本地 Native IM durable inbox 检查点（2026-08-30）

本检查点补齐 Web-first 验收所缺的两个本地持久化边界：经过 provider-neutral 校验的
`events.InboxEnvelope` 可以落盘并在重启后恢复；事件 `Projector` 的 checkpoint 也可以
以完整 compare-and-set 语义落盘。实现位于 `apps/im-api/internal/events/durable_inbox_file.go`
与 `apps/im-api/internal/events/durable_projection_file.go`，当前已 cherry-pick 到
`dev_wanwork_quantum_entanglement`。

## 已交付的合同

`DurableInboxFileStore` 实现 `events.InboxStore` 和 `events.InboxAdmissionReconciler`：

- 每次首次接收和重复投递都写入一条 newline-delimited JSON 记录，并在更新内存视图前执行
  `fsync`；
- key 精确绑定 `tenantId + workspaceId（nil 与空值不混淆）+ provider + channelId + eventId`；
- 同一 key 的 `eventDigest`、payload digest 或 `verificationId` 发生漂移时拒绝，原记录不被覆盖；
- 精确重试返回 `replayed`，并递增 `deliveryCount`；`Reconcile` 只返回已有 durable receipt，
  不把不确定提交伪装成新插入；
- 重启时重放完整记录，只有没有换行符的最终中断 tail 会被截断；完整损坏、未知字段、时间
  回退、状态倒退或 identity 漂移均 fail-closed；
- owner-only 文件权限（`0600`）、绝对路径和已有父目录要求；时钟必须由调用方提供且不得回退；
- `Close` 后所有读写拒绝，取消上下文和时钟异常不会产生部分 admission。

`DurableProjectionCheckpointFileStore` 补充 projection resume point：

- checkpoint key 精确绑定 `tenantId + workspaceId + projectionId`，未提交过的合法 scope 返回 zero
  checkpoint；
- commit 比较完整的 previous（position、cursor、lastEventId 和 scope），竞争提交只允许一个
  winner，冲突不会写入；
- checkpoint 更新先写并 `fsync` 再发布到内存，重启会恢复最后一个状态；中断尾部可丢弃，完整
  损坏、未知字段或位置倒退 fail-closed；
- 它只证明 resume point 已落盘，不证明 handler side effect 已持久化；仍需 at-least-once handler
  幂等和生产 receipt/reconcile。

## 明确边界

这是本地恢复/协议验证候选，不是 W2 完成声明。它没有跨进程锁、PostgreSQL 事务、RLS、复制、
加密、篡改证据链、保留/删除策略、备份恢复、provider webhook 签名验证、融云 SDK 或外部发送。
因此生产组合仍必须选择并验证 PostgreSQL `NativeIMInboxStore`/`NativeIMAtomicStore`，并补充
crash/restore、租约、审计和对账证据。本地 Web demo 仍默认使用合成 provider，禁止将此文件
适配器接到真实 provider 流量。

## 可重复验证

在 worktree 根目录执行：

```bash
(cd apps/im-api && \
  GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
  go test ./internal/events -count=1)

(cd apps/im-api && \
  GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
  go test -race ./internal/events -run DurableInboxFile -count=1)
```

测试覆盖重启后的 receipt、inline/reference payload、重复投递计数、并发 exact retry、digest
和 verification drift、时钟回退、关闭语义、中断尾部和完整损坏日志。测试只使用本地临时
目录，不访问网络，不读取或写入任何模型/provider key；projection 测试还验证 durable event
store + projector + checkpoint 的重启后不重复回放。

## 提交证据

以下提交已保留在当前 Web 主开发分支，并已推送到 `origin/dev_wanwork_quantum_entanglement`：

| Commit | 内容 |
| --- | --- |
| `d6558fb` | `feat(im): add durable local inbox file adapter` |
| `7a1d85f` | `test(im): verify durable inbox restart semantics` |
| `39b9683` | `fix(im): harden local inbox file recovery` |
| `e59cf52` | `feat(im): add durable local projection checkpoints` |
| `e92edb5` | `test(im): prove durable projection checkpoint recovery` |

