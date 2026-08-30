# Web 优先阶段检查点（2026-08-30）

本检查点记录 `dev_wanwork_quantum_entanglement` 分支上 Web-first vertical slice 的实际验收
结果。它是可重复的本地体验基线，不是生产发布声明，也不代表已经接入真实 Clerk、融云或生产级模型服务。

## 本次完成范围

- 新增显式 Workboard：Task、Artifact 草稿和 Needs You 独立投影；可在 Web 页面接受或退回产物，
  生命周期不会被聊天正文冒充；详情见 [WEB_TASK_WORKBOARD_CHECKPOINT_20260830.md](WEB_TASK_WORKBOARD_CHECKPOINT_20260830.md)。

- React + TypeScript + Vite + Zustand Web 客户端可以从一键脚本启动；
- 生产构建带有 manifest 和只缓存静态 shell 的 service worker，可添加到主屏幕；`/api/`、登录态和
  聊天数据不进入浏览器缓存；
- 会话列表支持按名称/ID 筛选，运行态明确显示 synthetic 或显式模型 runtime、provider、model 和调用计数；
- 当前会话支持受 ACL 保护的消息全文搜索，搜索结果仍来自平台消息投影；
- Go + Fiber loopback API 统一使用 `{code,data,message,requestId}` envelope，业务错误仍返回 HTTP 200；
- Agent Store 卡片来自认证后的后端投影，包含 definition、release、Trust Passport、installation、
  requested/granted capabilities 和 data routes；
- 创建群时可邀请已安装的 v0版 Agent；已有普通群可用成员动作幂等邀请；
- 当前群发布自定义指令后，后端按 `parentConversationId + messageId` 做幂等边界，创建关联的
  Agent 子群；
- 子群显式创建 human/Agent membership 与 ACL，Agent 回复只写入子群；父群只写受限工作卡；
- Web 发布成功后刷新会话投影并自动进入子群，父群仍可回看工作卡；
- Agent Store 的 `available` Agent 可在当前工作空间显式安装，安装请求带 idempotency key；安装后
  生成普通成员式 Agent actor 并加入根群，原 active demo 安装受控 offboard；
- Agent Store 对已安装 Agent 提供显式“停用并撤权”：数据处置策略必须为 `retain`、`archive` 或
  `delete` 之一；provider 成员移除和普通用户撤权完成后，installation 才迁移到 `offboarded`，并
  清理 parent/child conversation 的成员与 access 投影；重复请求可安全 replay；
- Workboard 中已接受的 Artifact 才能发布到父群，父群只收到带 Artifact ID/digest 的引用消息，重复
  发布保持幂等且不复制产物正文；
- 无 Agent 的群、Agent 子群和权限不足请求不会创建新的 Agent 子群。

## 可重复验收

在本 worktree 根目录执行：

```bash
./scripts/start_web_client.sh --no-open
```

打开 `http://127.0.0.1:5173/`，按以下顺序操作：

1. 保持“创建时邀请已安装 Agent”勾选，创建一个新群；
2. 在右侧输入任意自定义指令，点击 `@v0版 Agent`；
3. 页面会出现新的 `Agent · <父群名>` 子群并自动进入；
4. 子群中只能看到 Agent 回复；切回父群可看到 `Agent 工作卡已创建`；
5. 在 Agent 子群中再次发布指令，按钮保持不可用；
6. 取消创建时邀请或在无 Agent 普通群中操作，先邀请 Agent 后才可发布指令。

API 只读复核：

```bash
curl --fail \
  -H 'Authorization: Bearer demo.local.signature' \
  http://127.0.0.1:18080/api/v1/demo/im/agents
```

## 自动化证据

可重复的一键门禁：

```bash
./scripts/verify_web_first.sh
```

该脚本强制 synthetic runtime，自动构建 Web、启动临时 loopback API，并检查 Agent Store、HTTP 200
envelope、动态 mention、子群隔离、Workboard 审阅/发布和 provider committed；最后执行 Agent Store
offboard，检查成员/访问清理、撤权幂等回放以及撤权后的 `code=40301` 拒绝；成功后清理 API 进程
与临时日志。

```text
GOTOOLCHAIN=local GOPROXY=off go test ./apps/im-api/internal/localdemo ./apps/im-api/internal/app -count=1
GOTOOLCHAIN=local GOPROXY=off go test -race ./apps/im-api/internal/localdemo -count=1
(cd apps/im-api && GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go test ./... -count=1)
(cd apps/im-api && GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go vet ./...)
npm run build                         # clients/im-web
git diff --check
```

上述命令在本检查点均通过（包括完整 Go 模块测试和 `go vet`）。浏览器实际验收截图（本地 Git
忽略目录，不会进入业务仓库）为：

- `output/playwright/im_web_agent_store_20260830.png`：Agent Store 动态投影；
- `output/playwright/im_web_agent_invited_group_20260830.png`：创建群时邀请 Agent；
- `output/playwright/im_web_selected_parent_20260830.png`：父群工作卡和子群关联；
- `output/playwright/im_web_webfirst_20260830.png`：Web-first 当前完整页面和 Agent 子群回复。
- Playwright network 记录只出现 `127.0.0.1:5175` 页面/API 请求，无飞书、企微、融云或其他外部 host；
- 生产 preview 已验证 `/manifest.webmanifest` 与 `/sw.js` 返回 HTTP 200，service worker controller 生效。

## 提交序列

以下提交均已推送到远端 `origin/dev_wanwork_quantum_entanglement`，但未合并到 `main`：

| Commit | 目的 |
| --- | --- |
| `2594eaf` | 按所选父群 materialize Agent 子群、回复和父群工作卡投影 |
| `1864edd` | 将 mention 的禁止/未授权等业务错误收进 HTTP 200 envelope |
| `4fc6ac1` | Web 发布成功后刷新会话并自动进入 Agent 子群 |
| `9a98d46` | Web 启动器为 Go 进程关闭 telemetry，减少冷启动外部等待 |
| `a66f0a3` | mention API 明确绑定所选父群 |
| `3743faf` | 旧 IM demo 强制 synthetic，修正零网络边界文案 |
| `42ea9ec` | 显式 modelruntime port、OpenAI-compatible Responses/SSE 适配和安全边界 |
| `5b440d5` | Web/PWA 静态 shell、manifest、service worker、会话筛选和 runtime 展示 |
| `6c394a6` | 将 Web 页眉准确标记为 loopback app |
| `aa1daf4` | 将 Web-first 固化为后续多端和真实 provider 的交付门禁 |
| `857eb08` | Agent Store 本地 catalog 安装闭环 |
| `8bda56a` | Web 暴露 Agent Store 安装动作 |
| `8095c3b` | Agent Store 安装/回放门禁 |
| `4babd88` | 接受后的 Artifact 引用发布 |
| `a99338a` | Workboard 暴露 Artifact 发布按钮 |
| `536395c` | Artifact 发布/回放门禁 |
| `6e039d2` | Agent Store 安装与 invocation 的 action-time Trust Passport 准入加固 |
| `0427a8c` | provider user revoke/member removal 合同与 fake provider 效果语义 |
| `d63ae39` | Agent Store 幂等 offboard、数据处置与本地投影清理 |
| `62a5ca0` | Web Agent Store 停用并撤权动作与端到端门禁 |

## 仍然禁止宣称完成的范围

- 生产级模型治理、完整 GPT/DeepSeek Harness 执行、工具调用和 Artifact 结果（当前仅有显式
  OpenAI-compatible 文本生成 adapter）；
- durable PostgreSQL conversation/thread projection、outbox/inbox、恢复和 reconciliation；
- Clerk JWKS/session revoke 与 action-time tenant/Actor resolver；
- 融云真实 SDK、回调真实性、重放防护、对账和 mapping drift；
- 文件、搜索、已读、通知、reaction、离线同步和多设备能力；
- Mac/Windows/Linux、iPhone/iPad、Android、鸿蒙原生客户端；
- 生产 secret broker、观测、SLO、备份恢复、合规和发布门禁。

Offboard 当前是 synthetic/fake provider 纵切片，生产仍需真实 provider callback/readback、durable
UoW、审计事件、credential lease revoke、reconcile worker 和跨服务一致性处理。

因此当前最合理的验收结论是：Web 端核心群聊 + Agent 子群拓扑 + PWA shell 已具备可体验闭环；
下一步仍需在 Web/API 合同上补齐 durable projection、真实 runtime 治理和 provider adapter，再决定
原生 IM 接入时点。当前不宣称生产商用完成。
