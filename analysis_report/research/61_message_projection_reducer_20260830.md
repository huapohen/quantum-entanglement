# 消息事件投影 reducer（2026-08-30）

> 实现提交：`82a6b6d`
>
> 前置事件读取合同：`1f262f0`
>
> 性质：可重放的 provider-neutral reducer；不是 PostgreSQL durable projection，也不是授权器

## 目标

将会话 stream 中的三类平台消息事件还原成 `im.MessageSnapshot`，为后续
`events.Projector` + PostgreSQL checkpoint 接线提供稳定、无 provider 依赖的业务 reducer。Reducer
本身不拥有数据库、租户权限、provider delivery 或 Agent 调度。

支持的事件类型：

```text
message.created
message.edited
message.recalled
```

事件必须位于 `StreamID == ConversationID` 的单一会话流，并与 projection 的 TenantID 完全相等。

## 不变量

- `message.created` 要求精确字段 `conversationId/messageId/clientMessageId/messageType/text`，
  `extInfo` 可选；未知字段、缺失字段、错误类型和非 text/system 类型全部拒绝；
- created 的 `ActorID` 解析成 tenant-scoped `ActorRef`，平台 MessageID 与 client message ID 分离；
- 同一 event ID 重放是幂等 no-op，不新增或修改消息；
- 未见过的 event sequence 必须严格大于上次成功 sequence；乱序不会推进 checkpoint；
- 同一 MessageID 二次 created、未知消息 edit/recall、已撤回消息再次 edit/recall 均返回 conflict；
- edit 生成 revision + 1 的 `edited` snapshot，recall 生成 revision + 1 且正文清空的 `recalled` snapshot；
- payload 只接受 inline canonical JSON object，reference payload 不可直接构造消息；
- 失败事件不会修改 message map、seen event set 或 last sequence；
- `Messages()` 返回独立快照，按 created time + MessageID 确定性排序。

## 与 durable projector 的连接

```text
EventStore.ReadStreamPage / ReadGlobalPage
  -> events.Projector checkpoint
  -> improjection.MessageProjection.Apply
  -> future durable MessageSnapshot repository
```

`MessageProjection` 可作为 `events.ProjectionApplyFunc` 的 method value 传入：

```go
projection, _ := improjection.NewMessageProjection(conversationRef)
projector, _ := events.NewProjector(eventStore, checkpoints, projection.Apply, 1)
```

示例只表达类型接线，不表示当前已有 PostgreSQL message 表或生产 runtime composition。

## 验证

```text
cd apps/im-api
go test ./internal/improjection   # PASS
go vet ./internal/improjection     # PASS
```

专项覆盖创建→编辑→撤回、重放去重、乱序、scope 漂移、重复创建、未知字段、引用 payload、已撤回编辑、
未知消息和失败不变性。测试使用零网络内存事件值；没有真实 IM、飞书、企微、模型或 outbound 调用。

## 当前缺口

- reducer 没有 durable checkpoint；进程退出后状态丢失；
- 尚无 PostgreSQL `message_heads/message_snapshots` schema、受控写函数、读取 repository、backup/restore
  或 crash/kill recovery；
- 尚无 message list/read-state/mention/thread API，也没有把事件 payload 映射成 Agent/Task authority；
- reducer 不执行 authorization。HTTP/worker 入口仍必须先做 trusted tenant + action-time ACL；
- Gate A–E、真实 IM、provider exchange、outbound 和 Agent dispatch 继续关闭。

下一步优先级：定义 durable MessageProjectionRepository 的同一事务读接口，把 event projector checkpoint、
message head/revision、inbox-to-message dedupe 绑定在一个可恢复的 PostgreSQL transaction 语义中，再开放
只读 message route。
