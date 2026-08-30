# Quantum Entanglement 原生 IM：本地验收入门

这份教程用于验收 `dev_wanwork_quantum_entanglement` 分支上的原生 IM 阶段版本。默认模式完全本地、零网络、零生产凭据：它不会读取 Clerk、融云或模型 Key，不连接飞书/企微，也不会给任何人或群发消息。只有显式选择 `openai-compatible` runtime 并提供完整模型配置时，才会向审核过的模型端点发起一次模型请求。

## 1. 一条命令启动

在仓库根目录执行：

```bash
./scripts/start_im_demo.sh
```

终端显示地址后，用浏览器打开：

```text
http://127.0.0.1:18080/demo/im
```

端口被占用时可改用：

```bash
./scripts/start_im_demo.sh --port 19080
```

停止服务：回到启动服务的终端按 `Ctrl-C`。

如果需要一次性验证 Web 构建、synthetic API、Agent Store 和子群隔离，而不打开浏览器，可在仓库
根目录执行：

```bash
./scripts/verify_web_first.sh
```

脚本会使用临时端口和临时日志，结束时自动清理；可用 `WANWORK_IM_VERIFY_PORT` 指定验证端口。
它强制 synthetic runtime，不读取模型/聊天平台凭据，也不访问外网。

## 2. 页面怎么验收

1. 在左侧父群输入任意具体任务，不是固定示例；
2. 点击“@研究 Agent”；
3. 父群出现一张受限工作卡，只含子群 ID、invocation ID、Agent ID 和状态；
4. 右侧出现新建的真实子群，显示独立 lineage 和 Agent 回复；
5. 核验 Agent 回复的 `conversationId` 等于子群 ID，并且不等于父群 ID；
6. 再输入另一条指令，会按新的消息 ID 创建另一个子群。

右侧 Agent Store 卡片不是前端固定文案，而是启动时从认证 API 读取的 definition、release、Trust
Passport 和 installation 投影。可以在页面外用下面的请求复核同一份数据：

```bash
curl --fail \
  -H 'Authorization: Bearer demo.local.signature' \
  http://127.0.0.1:18080/api/v1/demo/im/agents
```

重点检查 `requestedCapabilities`（release 声明）与 `grantedCapabilities`（租户安装决定）分开，
以及 `dataRoutes`、`attestations` 和 `installationStatus` 都来自后端投影；这些字段不会被当作
凭据或 action-time 授权。

当前 synthetic 目录同时提供预装的 `v0版研究 Agent` 和可安装的 `v0版规划 Agent`。对后者点击
“安装到当前工作空间”会调用
`POST /api/v1/demo/im/agents/agd_local_planner/install`，安装动作绑定调用方提供的
`idempotencyKey`；服务会先完成 Agent actor 的 fake provider provisioning，再把新 Agent 加入根群，
并将上一演示安装标记为 `offboarded`。重复同一安装动作返回 `replayed=true`，不会创建第二个安装。
这只证明本地安装状态机和 provider-neutral 投影，不证明真实制品、签名、组织审批、PostgreSQL
持久化或生产撤权已完成。

安装时可以显式选择最小能力集合；后端只会接受 Trust Passport 的 `requestedCapabilities` 子集，
不会因为客户端直接提交字符串就授予额外能力。例如只授予读取会话：

```bash
curl --fail \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer demo.local.signature' \
  --data '{"idempotencyKey":"manual/store/install/planner-least-privilege","grantedCapabilities":["conversation.read"]}' \
  http://127.0.0.1:18080/api/v1/demo/im/agents/agd_local_planner/install
```

响应中的 `agent.requestedCapabilities` 是 release 声明，`agent.grantedCapabilities` 是本次租户安装
实际获得的集合；前者当前包含 `artifact.read` 与 `conversation.read`，后者只会包含请求的
`conversation.read`。提交未声明能力（如 `payment.execute`）返回 HTTP 200 envelope、业务
`code=40301`；提交重复项或 `[]` 返回 `code=42201`。省略该字段保持旧客户端兼容，默认授予完整
reviewed 集合。相同幂等 key 改变授权集合会返回 `40902`，避免把已提交安装悄悄升级或降级。

安装后可在同一张 Agent Store 卡片选择 `retain`、`archive`（默认）或 `delete`，再点击“停用并撤权”。
确认框会展示本次处置策略；API 也支持同样的三种取值，例如：

```bash
curl --fail \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer demo.local.signature' \
  --data '{"idempotencyKey":"manual/store/offboard-1","dataDisposition":"archive"}' \
  http://127.0.0.1:18080/api/v1/demo/im/agents/agd_local_planner/offboard
```

成功响应仍是 HTTP 200 envelope，`data.agent.installationStatus` 为 `offboarded`，并回显
`dataDisposition` 与 `removedConversationIds`。同样的 definition + idempotency key + 处置策略再次调用会返回
`replayed=true`；同一 key 改处置策略会返回业务冲突。撤权动作在 provider 侧先移除 Agent 的
parent/child 群成员，再撤销普通用户式 Agent actor，随后清除本地成员/access 投影。撤权后再次
发布 `@Agent` 指令应仍是 HTTP 200，但 envelope `code=40301`，不会创建新的 invocation、子群或消息。
这条路径是本地 fake provider 的生命周期验收，不等价于真实融云撤权、凭据租约回收或跨服务事务。

在左侧新建群时默认勾选“创建时邀请已安装 Agent”。提交后打开该群，检查其 `memberActorIds`
包含 Agent actor；取消勾选则创建只含真人的普通群。此处复用 `CreateConversation` 的成员边界，
不是把 Agent 偷塞进 UI 状态。

也可以在任意由当前用户管理的普通群中点击 Agent Store 的“邀请到当前群”。后端会校验 active
membership、`manage_members` 权限、已知 Agent actor 和 provider group，再以
`conversationId + idempotencyKey` 记录动作；重复请求返回 `replayed=true`，已存在成员不会重复
写入。生产实现必须把该记录迁移到 tenant-bound UoW 和 durable receipt。

默认回复是确定性的本地验收结果，不调用大模型；显式选择模型 runtime 后，回复文本来自 OpenAI-compatible Responses API，但仍只会发送到已授权的 Agent 子群。这里验证的是身份、Agent Store、群拓扑、ACL、幂等和 provider 边界；生产级模型治理、工具执行和真实 Clerk/融云网络接入属于后续适配阶段。

## 2.1 本地事件日志恢复验收（可选）

事件合同还提供了一个单进程、零网络的 `DurableFileStore`，用于验收“进程重启后已接受 event 不丢失”。
它不是 PostgreSQL、不是多进程锁，也没有篡改证据；不要把它用于真实组织数据或生产部署。调用方必须传入绝对
日志路径，父目录需要预先存在，文件权限由实现固定为 `0600`：

```go
store, err := events.OpenDurableFileStore(
    context.Background(),
    "/tmp/wanwork-im/events.log",
    "local-recovery-run-1",
    func(context.Context) time.Time { return time.Now().UTC() },
)
```

可验证的语义：完整记录 `Write + fsync` 后才对读者可见；相同 batch 重试返回 `Replayed=true`；重新打开
同一文件可读取原 sequence/global position；无换行的最终中断尾部会被丢弃，而已换行的完整损坏记录会直接
失败关闭。生产所需 PostgreSQL EventStore、inbox/outbox、kill-9/restore 和 provider reconciliation
仍未完成。

## 3. 用 curl 验收 API

先检查运行快照：

```bash
curl --fail http://127.0.0.1:18080/api/v1/demo/im
```

预期为 HTTP 200，业务 envelope 的 `code` 为 `200`，且：

```json
{
  "mode": "zero-network-fake",
  "networkCalls": 0,
  "authProvider": "auth.fake.clerk-shaped.v1",
  "imProvider": "im.fake.rongcloud-shaped.v1"
}
```

发送自定义指令：

```bash
curl --fail \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer demo.local.signature' \
  --data '{
    "conversationId":"cnv_local_demo_parent",
    "messageId":"msg_manual_1",
    "instruction":"比较三个 Agent 协作产品，输出证据、差异和建议"
  }' \
  http://127.0.0.1:18080/api/v1/demo/im/mentions
```

`demo.local.signature` 是代码内固定的公开合成 fixture，不是 API Key、会话凭据或可访问外部系统的 token。

成功结果应满足：

- HTTP status 始终为 `200`；
- envelope `code=200`；
- `childConversationId` 以 `cnv_at_` 开头；
- `parentConversationId` 等于请求的 `conversationId`（省略时仅兼容地使用演示父群）；
- `invocationId` 以 `inv_at_` 开头；
- `agentReply.conversationId == childConversationId`；
- `agentReply.conversationId != parentConversationId`；
- 首次 `providerStatus=committed`；
- 同一 `messageId + instruction` 重试时 `replayed=true`；
- 同一 `messageId` 改写 instruction 时，HTTP 仍为 200，但业务 `code=40902`。

## 4. 当前执行链

```mermaid
flowchart LR
    U[真人 Actor] -->|父群消息 + @Agent| P[平台控制面]
    C[Clerk-shaped fake] -->|只证明 human subject| P
    S[Agent Store] -->|definition + release + passport + installation| P
    P -->|显式创建 child snapshot| T[Agent Thread 子群]
    P -->|普通用户 provision| R[RongCloud-shaped fake]
    R -->|创建真实 provider group| T
    T -->|Agent reply 仅发子群| T
    P -->|受限 work-card ext_info| G[父群]
```

父群和子群的权限不是继承关系：

```mermaid
flowchart TB
    PA[父群 ACL<br/>human: read/send/invoke_agent] -->|只允许创建动作| PLAN[Thread Plan]
    PLAN --> HC[子群 Human ACL<br/>read/send/manage]
    PLAN --> AC[子群 Agent ACL<br/>read/send/publish artifact]
    PA -. 不继承 .-> AC
```

## 5. 已实现的安全与一致性检查

- Clerk 只做认证；verified subject 不携带 tenant、workspace、群或 Agent 权限；
- 融云只做传输；provider receipt 与 `ext_info` 都不能推进平台业务状态；
- Agent 使用与真人相同的普通用户 provision port，不存在“机器人账号”类型；
- Agent `ext_info` 只含 `schemaVersion / subjectType / platformActorId / agentDefinitionId / agentVersion`；
- Agent Store 把 definition、release、artifact/manifest/persona digest、capability、prohibition、data route、attestation、installation 和 offboarding 分离；
- 安装只能授予 Passport 声明能力的子集；禁止项不能被授予；
- `@Agent` 的 dedupe key 绑定 tenant、workspace、父群、根消息、安装、release 和 Agent actor；
- 相同 mention 重试收敛到同一子群；同消息正文漂移会冲突；
- 父群 work card 是规范化 JSON stringified `ext_info`，不含 prompt、Agent 回复、Artifact、credential、capability 或子群 ACL；
- Agent 回复构造器逐字段绑定子群 provider reference，指向父群会被拒绝；
- fake adapter 保留重复入站事件，平台 inbox 才是未来的 durable dedupe owner；
- `DurableFileStore` 仅是本地恢复证据，声明 `durability=durable`、`persistsAcrossRestart=true`，但
  `tamperEvident=false` 且不提供多进程 writer fence；
- 本地 API 的业务错误仍返回 HTTP 200，并把结果放进 `{code,data,message,requestId}`。

## 6. 自动化验证

运行本阶段相关测试：

```bash
cd apps/im-api
go test -race \
  ./internal/auth \
  ./internal/adapters/auth/fake \
  ./internal/im \
  ./internal/adapters/im/fake \
  ./internal/agentstore \
  ./internal/agentthread \
  ./internal/localdemo \
  ./internal/app
```

运行整个 Go 服务测试与静态检查：

```bash
cd apps/im-api
go test ./...
go vet ./...
```

## 7. 当前边界，不要误判为已生产接入

本页能证明本地合同和 vertical slice 可运行，不能证明以下生产条件已经满足：

- 真实 Clerk JWKS、issuer/audience、key rotation、session revoke 与 webhook；
- 真实融云 SDK、callback 签名、timestamp/nonce/replay、限流和对账；
- Agent Store 与 thread plan 的 PostgreSQL durable repository；
- provider commit-unknown readback、outbox/inbox、crash recovery 和 reconciliation worker；
- 移动/桌面 push、离线同步、多设备已读游标、文件/音视频/搜索等完整办公 IM；
- 生产级模型 runtime、工具执行、Artifact 验收和完整 Agent 回复链（当前只提供显式
  OpenAI-compatible 文本生成 adapter）；
- 生产 secret broker、IaC、观测、SLO、故障演练与数据合规。

这些边界会继续保留在阶段计划和调研报告中，不能用本地 fake 的绿色测试替代。
