# v0版本地可重启事件日志检查点（2026-08-29）

## 结论

在 W1 `VolatileMemoryStore` 合同 fake 之上，本阶段新增了一个可重启恢复的本地
`DurableFileStore`。它把每次被接受的 event batch 编码为一条 newline-delimited JSON 记录，先完整写入并
`fsync`，再发布到内存索引；重新打开同一个文件后，stream/global position、sequence、记录时间和精确
重试结果都能恢复。

这是一条本地恢复与验收路径，不是生产 PostgreSQL 的替代物。它明确不提供多进程 writer fencing、租约、
复制、备份/恢复演练、篡改证据、租户授权或真实 IM/provider 出网。生产组合仍必须使用独立的 PostgreSQL
EventStore、真实 migration、RLS/action-time PEP、crash/kill-9/restore 证据和 outbox/reconciliation。

## 实现要点

代码位于 `apps/im-api/internal/events/durable_file.go`：

- `OpenDurableFileStore` 要求绝对路径和已存在的父目录，日志文件以 `0600` 打开，不替调用方创建未知
  数据根目录；
- 每个 batch 保存 canonical append digest、请求 scope、`recordedAt`、event envelope、payload digest、
  sequence 与 global position；inline payload 保留 canonical JSON，reference payload 保留 opaque ref；
- 写入阶段在同一进程锁内完成 input snapshot、exact retry/conflict、expected revision、capacity、受信
  clock 检查；完整记录 `Write` + `Sync` 成功后才修改 volatile projection；
- 重新打开时逐行严格解码、重新验证 event/payload/append digest 和位置连续性；无换行的最终尾部视作进程
  中断并截断，已换行但格式错误的记录直接 `ErrDurableFileLog`，避免把完整损坏静默吞掉；
- Read API 委托给既有 scope/namespace-bound cursor 实现，因此跨 tenant/workspace/stream/kind 的 cursor
  仍 fail-closed；`Close` 后所有读写均拒绝。

## 测试证据

`apps/im-api/internal/events/durable_file_test.go` 覆盖：

1. 两条 event 的 append、stream/global page、close/reopen 和 exact retry；
2. 同一 event/client id 的正文漂移冲突；
3. 中断式无换行尾部恢复，以及完整换行损坏拒绝；
4. close 后的 append/read fail-closed；
5. 12 路并发 exact retry 只有一个 fresh commit，其余为 replay；
6. `StoreCharacteristics` 声明 durable/restart-persistent 但不宣称 tamper-evident，并由
   `ValidateStoreRequirements` 拒绝篡改证据要求。

本地验证命令：

```text
cd apps/im-api
go test -race ./internal/events
```

当前实现提交：`043e413dbff6d14ddfe04e78e9d35c30311a376b`（远端分支同名）。

## 下一步边界

该检查点只关闭“本地进程重启后 event 不应凭空消失”的最小证据，不关闭 IM 接入前生产门禁。下一批仍按
以下顺序推进：

1. PostgreSQL EventStore schema/function-only write surface，并与现有 authority manifest 对齐；
2. message projection 从 durable event source 重建，加入 inbox/outbox 和 provider reconciliation；
3. 成员治理、编辑/撤回、reaction、引用、附件、read cursor/unread；
4. Clerk JWKS、融云 SDK/callback authenticity、真实 adapter contract test；
5. fresh-host crash/reopen/kill-9/backup-restore 与 React/Tailwind/Zustand 客户端验收。

Notion 同步状态：`local_pending`。待本地代码、测试、报告、截图和 Git 版本全部收口后统一批量同步并逐页
回读；本检查点没有进行任何 Notion 写入。
