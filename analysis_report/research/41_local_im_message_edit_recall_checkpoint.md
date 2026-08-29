# v0版本地 IM 消息编辑与撤回检查点（2026-08-29）

## 结论

在普通 direct/group 会话、文本发送、cursor 分页和重启可恢复的本地事件日志之后，本阶段补齐了消息
编辑与撤回的可验收 vertical slice。平台消息仍是不可变 snapshot；编辑和撤回通过新 revision 表达，
而不是静默改写已经接受的正文。provider 传输效果是可选能力和观察字段，平台 revision 才是本地业务
投影的来源。

这仍是进程内、零网络的本地 demo 组合，不是生产 IM。fake provider 只验证 adapter port 的能力声明、
sender binding、外部消息 ID 绑定和幂等 receipt；它不能证明真实融云 SDK、callback authenticity、
commit-unknown readback、跨进程 durable inbox/outbox 或生产消息投递已经交付。

## 合同与状态机

\`apps/im-api/internal/im/message.go\` 冻结了以下边界：

- \`MessageRef\` 绑定 tenant-scoped \`ConversationRef\` 与平台 \`MessageID\`；\`ClientMessageID\` 仍只用于
  客户端重试/对账，不替代平台 ID 或 provider ID；
- \`MessageStatus\` 当前为 \`active\`、\`edited\`、\`recalled\`；\`MessageSnapshot\` 通过 \`revision\` 表示
  每次接受的状态变化；
- sender 必须与 conversation 属于同一 tenant，时间必须是 UTC，正文继续执行 UTF-8、NFC、长度、
  边界空白和控制字符校验；
- \`recalled\` revision 的 text 为空，system 消息不能被撤回；已撤回消息不能再次编辑，已编辑消息可以
  幂等重放同一个编辑请求但不能漂移到另一个正文；
- ext_info 继续保持 canonical JSON stringified 约定，不把 provider metadata 变成平台授权事实。

## Provider mutation port

\`apps/im-api/internal/im/provider.go\` 新增可选的 \`MessageMutationProvider\`：

\`\`\`text
EditText(ctx, ProviderTextEdit) -> ProviderEffectReceipt
RecallMessage(ctx, ProviderMessageRecall) -> ProviderEffectReceipt
\`\`\`

请求会绑定 tenant、conversation、platform message、sender、provider external message ID 和
idempotency key。缺少能力、sender 不匹配、外部 ID 漂移、撤回后编辑或正文漂移都会 fail-closed。fake
实现声明 \`text_edit\` 与 \`message_recall\`，重复请求返回 \`replayed\` receipt；禁用能力的 provider 不会被
本地服务静默模拟为成功。

## 本地服务与 HTTP API

\`apps/im-api/internal/localdemo/basic.go\` 在 provider mutation 成功（或 local-only provider 明确不需
出网）后创建新的平台 message revision，并保存 provider receipt：

| 方法 | 路由 | 语义 |
|---|---|---|
| \`PATCH\` | \`/api/v1/demo/im/conversations/:conversationId/messages/:messageId\` | 发送者编辑 text；相同正文安全 replay，正文漂移冲突 |
| \`POST\` | \`/api/v1/demo/im/conversations/:conversationId/messages/:messageId/recall\` | 发送者撤回；相同请求安全 replay |

业务错误继续使用 HTTP 200 envelope，业务码放在 envelope 内；未认证、跨租户、ACL 失败、未知消息、
非法状态转换、provider capability/receipt 失败均不会改变平台 projection。读消息 API 返回当前最新
revision，撤回消息不再暴露正文，UI 显示“（已撤回）”。

## 测试与浏览器证据

单元和 HTTP 测试覆盖：

1. fake provider 的 edit/recall capability、sender binding、external ID binding 和 exact idempotent
   receipt；
2. localdemo 编辑/撤回 revision、重复请求 replay、正文漂移 conflict、撤回后编辑拒绝；
3. Fiber PATCH/recall route、ACL/认证、业务 envelope 和 reload 后的当前状态；
4. race/vet 仍覆盖全仓库并保留现有 durable file store 的 crash-tail/corruption 边界。

本轮真实 Playwright loopback 证据：

- [\`40_local_im_edit_recall_desktop.png\`](../screenshots/40_local_im_edit_recall_desktop.png)，1440×1690，
  SHA-256 前缀 \`508e438917dd\`；
- [\`41_local_im_edit_recall_mobile.png\`](../screenshots/41_local_im_edit_recall_mobile.png)，390×2987，
  SHA-256 前缀 \`9ad9d6c73e06\`；
- 截图只证明 \`http://127.0.0.1:19083/demo/im\` 在两个 viewport 的本地渲染和交互，不证明外部网络、
  durable storage 或生产部署；完整字节、来源、时间和限制见 [\`manifest.json\`](../screenshots/manifest.json)。

验证命令：

\`\`\`text
cd apps/im-api
go test -race ./internal/im ./internal/adapters/im/fake ./internal/localdemo ./internal/app
go test -race ./...
go vet ./...
\`\`\`

## Notion 与下一步边界

本检查点和对应截图先标记为 \`local_pending\`，没有进行 Notion 写入。阶段收口时再把 canonical Markdown、
manifest、截图和 Git HEAD 批量同步并逐页回读；在此之前不能声称 Notion 已更新。

仍未完成的生产门禁包括：

- PostgreSQL durable message/event/projection repository、inbox/outbox、provider commit-unknown
  readback 与 reconciliation；
- Clerk JWKS、tenant resolver、session revoke、action-time membership/ACL PEP；
- 真实融云 edit/recall SDK、callback authenticity、历史回填、mapping drift 与 delivery receipt；
- 成员增删/群治理、reaction、回复/引用/转发、附件、搜索、已读/未读、通知和离线多端同步；
- React/Tailwind/Zustand 正式 Web/PWA 客户端与生产部署；
- Agent runtime 的 Task/Artifact/Acceptance 闭环、工具执行、审计证据和生产 secret/IaC。

因此当前出口是“本地 IM 消息生命周期可复现”，不是“可以连接真实组织或向任何真实用户外发”。
