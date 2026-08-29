# v0版原生 IM 本地验收阶段检查点（2026-08-29）

## 结论

本阶段已把“真人在父群 `@Agent` → 平台校验 → 创建真实子群 → Agent 只在子群回复 → 父群显示受限工作卡”做成可运行的 Go/Fiber 零网络 vertical slice。当前证据能证明合同、幂等、ACL、provider 边界和本地页面可复现；不能把 fake adapter 的成功外推为真实 Clerk、融云、模型或生产网络已经接入。

## 调研依据与设计取舍

本阶段优先复用重要调研目录 `/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output` 的结论：

- `_portfolio/master_research_report.md` §2.1 把组织协作、durable runtime、identity/trust 和 marketplace 分层，避免把聊天 UI 当作全部业务真相；§7.1 要求 Task/执行对象保留 identity、mandate、capability、budget、context、plan、execution、artifact、acceptance、evidence、recovery、closure；
- `agentspace/research_report.md` §33 把 Agent Identity & Capability Passport 定义为 owner、版本、能力、来源、撤销和信誉的可验证控制面，并明确先做企业内部 registry；
- `deepseek-harness/research_report.md` §11 说明“一切皆插件”必须和 publisher、capability manifest、SBOM、版本固定、沙箱、allowlist、撤销拆开，安装/运行/持久安装不能视为同一权限；
- `near-ai-agent-market/research_report.md` §1–§2 说明真正的 Agent market 是履约、交付、验收、争议和证据系统，不是单纯目录；本分支因此先交付内部 Agent Store 安装治理，不提前假设跨组织交易；
- `protocol-a2a/research_report.md` 的 Verified Agent Registry 分析要求 provenance、签名、版本、撤销和独立验收，支持本阶段把 provider receipt 与平台授权分离。

## 已实现合同

### provider-neutral IM

`internal/im/provider.go` 定义 provider profile、能力声明、普通用户 provision、群创建、成员更新、文本发送、入站分页和 transport receipt。所有 provider conversation 都绑定 provider + realm；跨 realm 引用、错误 provider、非 UTC receipt、超长文本和危险幂等键会被拒绝。

`internal/adapters/im/fake/provider.go` 是 RongCloud-shaped zero-network adapter：

- Agent 与真人走同一个 `ProvisionUser` 普通用户路径；
- `ext_info` 必须先通过 `immetadata` 的严格 canonical JSON；
- group/member/text effect 以 operation key 幂等，参数漂移返回 conflict；
- inbound cursor 可续读，重复入站消息刻意保留，由未来 durable inbox 去重；
- outbound 默认关闭，验收 composition 只在内存 fake 内显式打开。

### provider-neutral Clerk auth

`internal/auth/provider.go` 定义只认证的 verifier port。`VerifiedIdentity` 仅有 Clerk external subject、global human principal、session 和时间边界；不携带 tenant membership、workspace、conversation ACL、Agent installation 或 capability。`internal/adapters/auth/fake/verifier.go` 仅接受本地 synthetic fixture，真实 JWKS/rotation/revoke 适配留到生产阶段。

### Agent Store

`internal/agentstore` 把以下对象分开并冻结 revision：

- definition：tenant、claim owner、publisher、显示信息与生命周期；
- release：SemVer、artifact/manifest/persona digest、requested capability、prohibition、data route、runtime isolation；
- trust passport：release 绑定及 publisher/security/data-route 三类 attestation；
- installation：workspace、Agent actor、已授予 capability 子集、绑定数据路由和生命周期；
- offboarding：必须显式撤销 provider identity、移除群成员、取消 invocation、撤销 credential lease，并声明数据处置。

`BuildProviderUserProvision` 只投影 `schemaVersion / subjectType / platformActorId / agentDefinitionId / agentVersion` 到 `ext_info`，不投影 tenant、workspace、capability、route、digest、attestation 或凭据。

### @Agent 子群

`internal/agentthread` 的 dedupe digest 绑定 tenant、workspace、父群、根消息、requesting actor、installation、release 和 Agent actor，并派生稳定 child conversation ID 与 invocation ID。`PlanMention` 显式生成：

- `ConversationAgentThread` child snapshot（parent/root/invocation lineage）；
- human owner 与 Agent member 两份独立 membership；
- human 与 Agent 两份独立 access snapshot；
- provider group create request（agent-thread metadata）；
- parent-only restricted work card。

`BuildAgentReply` 逐字段绑定 child provider reference；把父群 provider reference 传入会直接失败。工作卡是 canonical JSON stringified `ext_info`，只包含子群/调用/Agent/状态标识，不含 prompt、回复、Artifact、权限或 credential。

## 本地验收方式

```bash
./scripts/start_im_demo.sh
open http://127.0.0.1:18080/demo/im
```

页面允许任意自定义指令；内置的 `demo.local.signature` 只是本地公开 fixture，不是 API key。curl、业务 envelope、重复 message、正文漂移冲突和独立子群 reply 的期望值详见：

[`docs/wanwork_im/LOCAL_IM_ACCEPTANCE_GUIDE.md`](../../docs/wanwork_im/LOCAL_IM_ACCEPTANCE_GUIDE.md)

页面截图与独立清单：

[`analysis_report/screenshots/35_local_im_acceptance_desktop.png`](../screenshots/35_local_im_acceptance_desktop.png)

[`analysis_report/screenshots/36_local_im_acceptance_mobile.png`](../screenshots/36_local_im_acceptance_mobile.png)

[`analysis_report/screenshots/local_im_acceptance_manifest.json`](../screenshots/local_im_acceptance_manifest.json)

截图由 Playwright 访问 loopback 页面生成，桌面 viewport 为 1440×1000，移动 viewport 为 390×844；未使用外部网络或生产凭据。

## 验证结果

```text
go test -race ./internal/auth ./internal/adapters/auth/fake
go test -race ./internal/im ./internal/adapters/im/fake
go test -race ./internal/agentstore ./internal/agentthread ./internal/localdemo ./internal/app
go vet ./internal/auth ./internal/adapters/auth/fake ./internal/im ./internal/adapters/im/fake
go vet ./internal/agentstore ./internal/agentthread ./internal/localdemo ./internal/app ./cmd/im-api
```

以上命令均通过。实际运行中已观察到：

```text
GET /health/live                         -> {"status":"ok"}
GET /api/v1/demo/im                      -> code=200, networkCalls=0
POST /api/v1/demo/im/mentions            -> code=200, providerStatus=committed
same messageId + instruction             -> code=200, replayed=true
same messageId + changed instruction     -> HTTP 200, code=40902
```

## 尚未宣称完成的生产工作

- 真实 Clerk JWKS、issuer/audience、rotation、session revoke、webhook 与 action-time binding；
- 真实融云 SDK、callback signature、timestamp/nonce/replay、限流、readback 和 reconciliation；
- Agent Store、thread plan、inbox/outbox、command receipt 的 PostgreSQL durable persistence；
- crash/fence/unknown-effect recovery 与多 worker leasing；
- 完整企业 IM（离线同步、已读、多端 push、文件/音视频、搜索、群治理）；
- 真实模型 runtime、tool/connector、Artifact acceptance、成本和审计；
- Web React/Tailwind/Zustand、Tauri、Flutter 生产客户端及原生 IM 后端拆分后的协议冻结。

这些是下一阶段的明确输入，不能用本地 fake 结果替代。
