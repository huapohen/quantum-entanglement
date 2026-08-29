# Web 优先阶段检查点（2026-08-30）

本检查点记录 `dev_wanwork_quantum_entanglement` 分支上 Web-first vertical slice 的实际验收
结果。它是可重复的本地体验基线，不是生产发布声明，也不代表已经接入真实 Clerk、融云或模型服务。

## 本次完成范围

- React + TypeScript + Vite + Zustand Web 客户端可以从一键脚本启动；
- Go + Fiber loopback API 统一使用 `{code,data,message,requestId}` envelope，业务错误仍返回 HTTP 200；
- Agent Store 卡片来自认证后的后端投影，包含 definition、release、Trust Passport、installation、
  requested/granted capabilities 和 data routes；
- 创建群时可邀请已安装的 v0版 Agent；已有普通群可用成员动作幂等邀请；
- 当前群发布自定义指令后，后端按 `parentConversationId + messageId` 做幂等边界，创建关联的
  Agent 子群；
- 子群显式创建 human/Agent membership 与 ACL，Agent 回复只写入子群；父群只写受限工作卡；
- Web 发布成功后刷新会话投影并自动进入子群，父群仍可回看工作卡；
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
- `output/playwright/im_web_selected_parent_20260830.png`：父群工作卡和子群关联。

## 提交序列

以下提交均已推送到远端 `origin/dev_wanwork_quantum_entanglement`，但未合并到 `main`：

| Commit | 目的 |
| --- | --- |
| `2594eaf` | 按所选父群 materialize Agent 子群、回复和父群工作卡投影 |
| `1864edd` | 将 mention 的禁止/未授权等业务错误收进 HTTP 200 envelope |
| `4fc6ac1` | Web 发布成功后刷新会话并自动进入 Agent 子群 |
| `9a98d46` | Web 启动器为 Go 进程关闭 telemetry，减少冷启动外部等待 |
| `a66f0a3` | mention API 明确绑定所选父群 |

## 仍然禁止宣称完成的范围

- 真实模型 runtime、GPT/DeepSeek Harness 执行、工具调用和 Artifact 结果；
- durable PostgreSQL conversation/thread projection、outbox/inbox、恢复和 reconciliation；
- Clerk JWKS/session revoke 与 action-time tenant/Actor resolver；
- 融云真实 SDK、回调真实性、重放防护、对账和 mapping drift；
- 文件、搜索、已读、通知、reaction、离线同步和多设备能力；
- Mac/Windows/Linux、iPhone/iPad、Android、鸿蒙原生客户端；
- 生产 secret broker、观测、SLO、备份恢复、合规和发布门禁。

因此当前最合理的验收结论是：Web 端核心群聊 + Agent 子群拓扑已经具备可体验闭环，下一步应
继续补齐 durable projection、真实 runtime 和 provider adapter，再决定原生 IM 接入时点。
