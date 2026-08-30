# Agent Store 安装/撤权命令状态（2026-08-30）

## 审计发现

此前 Agent Store install/offboard 响应同时返回最终 `agent` 和 `replayed` 布尔值，但 Web 成功提示只说“已安装”或
“已停用”。验收者无法从页面或稳定的 API 字段直接区分首次命令提交与同一幂等命令的重放，也看不到本次操作的关键结果。
这种歧义在网络重试和 provider effect 对账时会误导操作判断，因此补充一个最小的显式命令状态字段。

## API 合同

`POST /api/v1/demo/im/agents/:definitionId/install` 和
`POST /api/v1/demo/im/agents/:definitionId/offboard` 的成功响应增加：

```json
{
  "commandStatus": "committed"
}
```

取值只有：

| `commandStatus` | 含义 | `replayed` |
| --- | --- | --- |
| `committed` | 本次请求首次通过 localdemo 命令边界，并完成对应本地/模拟 provider 效果 | `false` |
| `replayed` | 相同 definition + 幂等 key + 请求摘要命中已保存结果，未重复执行操作 | `true` |

`replayed` 保留作为向后兼容字段；两个字段由服务端共同产生，客户端不能根据 HTTP status 或本地猜测。错误响应不返回
成功的 `commandStatus`。offboard 还继续回显 `dataDisposition` 与 `removedConversationIds`，install 结果中的 Agent
投影继续回显最终 granted capabilities。

## Web 验收体验

安装成功提示现在同时展示命令状态和最终实际授权能力；撤权成功提示展示命令状态、数据处置策略以及移除的会话数量。
重试时页面会明确显示“幂等重放”，避免把重试误看成第二次安装或撤权。未知/缺失状态不会静默显示为已提交，而会显示为“未知”，
让不完整的后端合同尽快暴露给验收者。

## 测试

新增两个 focused tests：

- `internal/localdemo/TestAgentStoreActionsExposeCommittedAndReplayedCommandStatus`：验证 install/offboard 首次提交、精确重试、
  最终 installation 状态及数据处置；
- `internal/app/TestLocalDemoAgentStoreCommandStatusEnvelope`：验证 HTTP 200 business envelope 中的 `commandStatus` 与
  `replayed` 成对出现，覆盖 install/offboard 两条路由。

```bash
cd apps/im-api
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./internal/localdemo -run TestAgentStoreActionsExposeCommittedAndReplayedCommandStatus -count=1
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./internal/app -run TestLocalDemoAgentStoreCommandStatusEnvelope -count=1
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./internal/localdemo ./internal/app ./internal/agentstore -count=1
cd ../../clients/im-web
npm run build
```

## 生产边界

当前字段证明的是 localdemo 的进程内命令状态，不是跨重启的 durable receipt，也不是 provider ACK。进入生产前，安装/撤权必须
把同一语义接入 tenant-bound PostgreSQL command receipt、provider outbox/effect reconcile 和 commit-unknown fresh readback；
只有 durable command 和外部 effect 都达到明确可恢复状态，才能向客户端返回 `committed`。未决 provider effect 不得返回成功状态。

本文件保留在本地/Git `local_pending`，暂不上传 Notion、语雀。
