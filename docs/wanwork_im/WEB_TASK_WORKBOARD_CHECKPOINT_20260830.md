# Web-first 任务工作台检查点（2026-08-30）

本检查点把当前 Web 纵切片从“聊天里出现 Agent 回复”推进到显式的
`Task → Artifact(draft) → Needs You → accepted/rejected` 生命周期。它服务于本地零网络验收，
不代表 PostgreSQL、真实认证、真实融云或生产模型治理已经完成。

## 用户可验收路径

1. 启动 `scripts/start_web_client.sh --no-open --no-install`。
2. 在普通群的“发布协作指令”输入框填写任意明确指令，点击 `@v0版 Agent`。
3. 页面自动进入 Agent 子群；右侧 Workboard 出现任务卡、草稿产物和 Needs You。
4. 点击“接受产物”：Artifact 变为 `accepted`，Task 变为 `completed`，Needs You 变为 `resolved`。
5. 重复点击或重复请求只返回 `replayed=true`，不会重复创建任务、产物或 Needs You。
6. 点击“退回”：Artifact 变为 `rejected`，Task 变为 `rejected`，同样保留审阅轨迹。

## Loopback API

| 用途 | 方法 | 路径 |
| --- | --- | --- |
| 查询任务 | GET | `/api/v1/demo/im/tasks` |
| 查询产物 | GET | `/api/v1/demo/im/artifacts` |
| 查询待确认项 | GET | `/api/v1/demo/im/needs-you` |
| 处理待确认项 | POST | `/api/v1/demo/im/needs-you/:needsYouId/resolve` |

处理请求体只接受 `{ "decision": "accept" }` 或 `{ "decision": "reject" }`。所有业务响应仍为
HTTP 200，并使用 `{code,data,message,requestId}` envelope。

## 不变量与边界

- 任务 ID、产物 ID、Needs You ID 都由 invocation 和内容摘要确定；相同 invocation 的重放不会重复建模。
- Agent 回复仍只进入 Agent 子群；父群只保留受限工作卡，Workboard 通过独立投影展示结果。
- 产物在真人处理 Needs You 前永远是 `draft`，`completed` 不等于 `accepted`。
- 当前投影是进程内内存，重启会清空；生产版本必须迁移到 tenant-bound PostgreSQL，接入 revision、
  expected-revision、outbox、crash recovery 和审计证据。
- 当前 Workboard 没有文件上传、版本比较、多人并发审阅和发布到父群动作；这些属于后续 W4/W5 增量。

## 验证证据

- `go test ./... -count=1`
- `go test -race ./... -count=1`
- `go vet ./...`
- `clients/im-web/npm run build`
- `scripts/verify_web_first.sh`

## 下一步

1. 将三类投影接入持久化事件和恢复测试，保持 API 语义不变。
2. 增加 Task recovery、Artifact 版本和父群“发布已接受引用”的显式动作。
3. 在真实 IM 接入前完成 trusted auth context、provider callback authenticity 和 outbox/reconcile 门禁。
