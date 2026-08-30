# 本地 Native IM durable inbox 检查点（2026-08-30）

本检查点补齐 Web-first 验收所缺的一个持久化边界：经过 provider-neutral 校验的
`events.InboxEnvelope` 可以在本地单进程日志中落盘，并在服务重启后恢复。实现位于
`apps/im-api/internal/events/durable_inbox_file.go`，分支为
`dev_im_persistence_accelerator_20260830`。

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
目录，不访问网络，不读取或写入任何模型/provider key。

## 提交证据

| Commit | 内容 |
| --- | --- |
| `234e016` | `feat(im): add durable local inbox file adapter` |
| `ddc9c5f` | `test(im): verify durable inbox restart semantics` |

两个提交均已推送到 `origin/dev_im_persistence_accelerator_20260830`，没有合并到
`main` 或 Web 主开发分支。

