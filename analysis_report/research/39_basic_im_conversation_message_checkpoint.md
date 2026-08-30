# v0版普通 IM 基础能力本地检查点（2026-08-29）

## 结论

在 Topic 38 的 `@Agent` 子群 vertical slice 之上，本阶段补齐了一个可实际验收的普通 IM 基础闭环：

```text
Clerk-shaped local auth
  → conversation list/create (direct / group)
  → explicit membership + access snapshot
  → text send with platform/client message IDs
  → provider receipt for ordinary groups
  → cursor-bound message pagination
```

这仍是进程内、零网络 fake 组合，不是生产 IM，也不宣称真实融云、Clerk、PostgreSQL durable message
store、离线同步或多端推送已经交付。普通 direct 会话在本地明确标记为 `local-only`；普通 group
会调用 RongCloud-shaped fake 的同一普通用户/群/文本 port，并把 transport receipt 显示为观察字段。

## 实现范围

### 平台消息合同

`apps/im-api/internal/im/message.go` 新增 `MessageRef` 和 `MessageSnapshot`：

- `MessageRef` 绑定 tenant-scoped `ConversationRef` 与平台 `MessageID`；
- `ClientMessageID` 单独保留，用于客户端重试和 provider 对账，不能与平台 ID 混为一谈；
- `MessageType` 当前冻结 `text`、`system`；状态冻结 `active`、`edited`、`recalled`；
- text 要求 UTF-8、NFC、无控制字符（保留换行/制表/回车）、边界不含空白，最大 64 KiB；
- `extInfo` 是有界、UTF-8/NFC 的字符串字段；本地 basic API 额外要求 canonical JSON object，
  让群自定义消息保持 JSON-stringified 约定；
- sender 必须与消息 conversation 同 tenant，时间必须是 UTC，revision 必须是合法持久化 revision。

### 本地普通会话/消息服务

`apps/im-api/internal/localdemo/basic.go` 在 demo composition 内维护显式的会话、成员、权限、消息和
client-id 幂等索引：

- `CreateConversation` 支持 `direct` 和 `group`；请求者自动成为 owner，其他成员必须来自已知平台
  Actor，创建群时独立写入 member/access snapshot；
- group 创建写入 canonical conversation `ext_info`，并通过 fake provider `CreateGroup` 获得 receipt；
- direct 不伪造 provider 能力，返回 `providerStatus=local-only`；
- `SendText` 计算 request digest 和稳定 platform message ID；同一 client ID + 同一正文安全 replay，
  正文或 `extInfo` 漂移返回 conflict；
- 已绑定 group 将文本发送到 fake provider，保存 provider message ID/status；direct 只保存平台本地
  message projection；
- `ListConversations` 与 `ListMessages` 的 cursor 包含 namespace、kind、scope、position 和 digest，
  交叉会话、截断、篡改、未知字段和非法位置 fail-closed；
- 所有状态修改受同一 mutex 保护，测试覆盖 concurrent replay，不把 demo fake 当 durable authority。

### Fiber API

所有业务错误继续走 HTTP 200 envelope，业务码位于 envelope `code`：

| 方法 | 路由 | 语义 |
|---|---|---|
| `GET` | `/api/v1/demo/im/conversations` | 当前请求者可见会话列表，`limit`/`after` 分页 |
| `POST` | `/api/v1/demo/im/conversations` | 创建 direct/group，会话创建幂等键绑定请求摘要 |
| `GET` | `/api/v1/demo/im/conversations/:conversationId/messages` | 受 ACL 保护的消息分页 |
| `POST` | `/api/v1/demo/im/conversations/:conversationId/messages` | 发送 text，`clientMessageId` 负责重试对账 |

映射边界：未认证 `40101`、无权限 `40301`、不存在 `40401`、幂等冲突 `40902`、cursor/参数失败
`42201`、provider fake 失败 `50301`；真正的网络/生产 adapter 尚未接入。

## 浏览器验收证据

本轮使用 Playwright 访问 `http://127.0.0.1:19082/demo/im`：

1. 创建“浏览器验收群”；
2. 选择新群；
3. 发送“普通消息验收”；
4. reload 后重新读取消息列表；
5. 在 1440×1000 和 390×844 viewport 检查响应式布局。

证据文件：

- [`38_local_im_basic_desktop.png`](../screenshots/38_local_im_basic_desktop.png)，SHA-256 前缀 `3d871740d3c0`；
- [`39_local_im_basic_mobile.png`](../screenshots/39_local_im_basic_mobile.png)，SHA-256 前缀 `249bdf2a0305`；
- [`local_im_basic_acceptance_manifest.json`](../screenshots/local_im_basic_acceptance_manifest.json)，记录
  loopback、无凭据、viewport 和交互步骤。

浏览器过程只证明当前进程内的 UI/API 交互与渲染；服务重启后内存状态会消失，不能被写成“已持久化”。

## 验证结果

```text
go test -race ./internal/im ./internal/localdemo ./internal/app
```

通过的断言包括：消息跨 tenant 拒绝、NFC/控制字符/超长文本拒绝、创建/发送 replay 与正文漂移冲突、
group provider committed、direct local-only、ACL 读写检查、cursor 篡改拒绝、Fiber 业务错误保持
HTTP 200，以及真实浏览器创建群/发送文本/reload/桌面移动渲染。

## 下一步与明确边界

仍未完成且不能用本检查点替代的部分：

- durable PostgreSQL message/event/projection repository、crash/reopen/kill-9、backup/restore 和旧/新
  schema 兼容；
- authenticated Clerk JWKS、tenant resolver、实时 membership/access action-time PEP；
- 真实融云 SDK/callback signature、mapping drift、inbox/outbox、provider commit-unknown readback 与
  reconciliation；
- 编辑/撤回、reaction、回复/引用/转发、附件/图片/链接卡片、搜索、已读/未读、通知和离线多端同步；
- 组织目录/部门/邀请/停用、群治理、公告群、权限变更、presence/routine；
- React/Tailwind/Zustand Web/PWA 生产客户端、Tauri 桌面端、Flutter 移动端；
- Task/Artifact/Acceptance/Needs You、真实模型 runtime、工具执行、审计证据和生产 secret/IaC。

因此当前出口是“本地普通 IM 基础 API 可复现”，不是“可以连接生产组织或向真实用户外发”。
