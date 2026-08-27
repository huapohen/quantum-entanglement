# 原生 IM 实施计划

## 1. 分支与提交策略

- 分支：`dev_wanwork_quantum_entanglement`；
- worktree：`execute/infinite/worktrees/quantum_entanglement/dev_wanwork_quantum_entanglement`；
- 不合并回 `main`；阶段完成后推送同名远端分支，用户人工审阅；
- 每个合同、模型、migration、adapter、页面或测试矩阵独立提交；
- 每个提交必须保持已有 Python 门禁和当前语言专项可运行；
- 凭据、构建缓存、数据库文件、截图临时文件和本地 `.env` 不提交。

## 2. 阶段

### W0：研究审计、冻结需求/架构和审阅空间

交付：31/31 独立报告覆盖账本、AgentSpace evidence delta、研究来源快照与可追溯矩阵、产品需求、
架构、API、安全、provider profile、Notion 私人顶层页。

出口：关键不变量、M0/V1 边界、Task/Thread/Attempt/Acceptance 分层、三类身份、Agent 普通用户策略、
`ext_info` schema、HTTP 200 envelope 和子群语义无歧义。

### W1：Monorepo 与 Go 基础

交付：

- `go.work`、`apps/im-api`；
- config、request ID、business envelope、error taxonomy；
- plugin lifecycle/registry；
- effective profile/bundle/tenant overlay 与 capability diff；
- host-owned manifest/admission、declarative config schema 与 Secret claim admission；
- Registry builder/freeze/runtime 分期、完整 definition graph 重验与 late registration 拒绝；
- effect scope `open/closing/closed`、Drain 前关闭注册与失败 cleanup 精确重试；
- durable event port、`EventToAppend/StoredEvent`、opaque cursor、deterministic fake 和 projection skeleton；
- health/readiness 与 graceful shutdown；
- unit、race、lint 和 secret canary。

W1 Plugin Host 只允许随 host 编译、由平台准入的可信内建插件；不实现也不暗示第三方 App/
Extension 任意代码生态。第三方可执行包不得加载进 API/Gateway/Plugin Host 主进程。

出口：零 credential、零网络的 fake composition 可启动，所有业务错误 HTTP 200；effective snapshot
具备 source/digest/golden/diff，manifest/package/Secret claim 均经 host-owned admission，首次扩权需新的
批准快照；raw locator 不进入 canonical/Factory，binding view 不具备 Secret 使用权；所有 required plugins
ready 前不暴露 route，半启动/ready 失败可逆序回滚；event fake 可确定性 replay/rebuild，但不宣称持久化。

W1 的 Secret admission 不等于 action-time credential。KMS/Keychain、跨重启持久 claim、JIT short-lived
lease/token exchange、trusted executor 和 provider receipt 仍在 W2/W4/W7 按 Action Plane 实现。

### W2：IM Domain 与 PostgreSQL

交付：组织、actor、human/workload/delegation、conversation、membership、message、reaction、read state、
Agent definition/release/installation、thread、BusinessTask、Attempt、Budget、NeedsYou、Artifact/
Acceptance、GovernedMemory、SkillPackageVersion/ActivationReceipt/MaterializationManifest、
CapabilityDefinitionVersion/AgentCapabilityAssignmentRevision/ExecutionBinding、inbox/outbox/action/evidence
models，`ContentObservation/ProvenanceEdge/TaintLabel/AuthorityClass/DeclassificationDecision`、
`ConversationRepresentationPolicyRevision/ConversationMandate/DisclosureRule/CommitmentLimit`、
`PromotionIntent/PromotionAttempt/PromotionReceipt/RollbackReceipt`、`AgentPresenceLease/RuntimeIncarnation`、
`DataRouteDescriptor/OrganizationProcessingApproval/PersonalAcknowledgement`、
`RoutineDefinition/MissedRunPolicy/ScheduleOccurrence`，以及 event stream/event/projection checkpoint migrations。

出口：空库/非空库 migration、rollback/restore、transaction、dedupe、revision 和 tenant isolation
测试通过；Task、Thread、Attempt、Action 与 Acceptance 状态不会互相冒充。

PostgreSQL event store 还必须通过 expected-revision transaction、crash/reopen、kill-9/restore 和
projection 清库重建；W1 memory fake 不能代替该门禁。

### W3：Clerk 与融云 adapter

交付：JWKS verification、identity mapping、user/group `ext_info` strict codec、fake adapter、融云
provider profile、sandbox config 和 inbound-only readback。

出口：真实 secret 不入 Git/日志/事件/Notion；fake 全矩阵通过；sandbox 只允许 health/read/dedupe/
resume；伪造 `ext_info` 不改变平台 authorization。

### W4：Agent Store 与 `@Agent` 子群

交付：Agent catalog/trust passport/install/member/offboard API、普通用户 provisioning、mention admission、
thread aggregate、Task/Attempt、子群 command、QE invocation bridge、Budget、Needs You、Artifact/
Acceptance、Action Ledger、unknown reconcile 与 Evidence Bundle。

M0 后同阶段继续交付受治理的 Skill activation 与 Tool execution binding；它们不阻塞首个零网络
垂直切片，但对应的领域对象和 port 必须已在 W2 冻结。

同阶段在 M0 验收后增量实现通用 Promotion Transaction 和第三方可执行包隔离；M0 本身只要求
accepted Artifact reference 由真人发布回父群。Agent fork/transfer、跨组织 federation 继续延后。

Runtime/Planner 只产出 typed ActionProposal；独立 Executor/Egress Broker 承担授权后 dispatch、
SSRF/redirect/credential forwarding 防护和 provider receipt/reconcile。

M0 后补 Routine/presence 产品面；data-route revision、组织数据处理批准与适用的个人告知/确认必须
在 Agent 首次真实数据处理前完成，不能因 Routine UI 延后而延后该安全门。

出口：重复 mention 不重复建群/Task/调用；Agent 回复只进子群；父群只出现受限卡片；execution
succeeded 不冒充 accepted；参数变化使批准失效；dispatch 故障不产生重复副作用。

### W5：React Web/PWA

交付：认证壳、会话列表、群聊、目录、Agent Store、工作子群、Needs You、设置、响应式和无障碍。

出口：mock/fake API 可完整体验 M0；1440/1024/390 截图和 Playwright E2E 通过；Needs You、Artifact
review、Task recovery 不是藏在聊天正文里的文本。

### W6：Desktop 与 Mobile

交付：Tauri 壳、签名/更新策略；Flutter 核心页面、deep link、通知和本地缓存；鸿蒙兼容评估。

出口：各平台共享 contract fixtures；至少完成桌面和移动 debug build。

### W7：受控集成与审阅交付

交付：sandbox runbook、backup/restore、threat model、SBOM、截图、启动教程、验收清单、Notion
全文/附件回读和 GitHub 分支备份。

出口：分支 clean，远端 SHA 一致；不合并；真实 outbound 仍需用户对具体 sandbox 再授权。

## 3. M0 首个不可伪造验收脚本

```text
两个 tenant / 两个真人
  -> 安装已批准且版本冻结的 Agent
  -> Agent 以普通 IM 用户投影入群
  -> 重复 @Agent webhook 只创建一个 Task/子群/Invocation
  -> membership + delegation + budget admission
  -> source + authority class + taint path admission
  -> fake runtime progress
  -> 参数 hash 绑定的 Needs You
  -> approve/edit/reject receipt
  -> Artifact v1 + independent verifier
  -> accepted
  -> 真人明确发布 Artifact reference 回父群
  -> Agent offboard 撤权，Artifact/Evidence 责任链保留
```

同一 E2E 必须注入 duplicate、out-of-order、ACK loss、worker crash、旧批准改参、跨 tenant 访问、
Agent release 撤销、预算超限、prompt injection/taint 丢失和审计暂时不可用；高风险
policy/approval/intent/receipt append 不可用时
预期结果固定为 dispatch 前 fail-closed，不能静默降级。证据包必须能解释每次最终状态。M0 不接真实 outbound。

## 4. 第一批小提交

1. `docs: define WanWork IM product and architecture`
2. `docs: freeze IM API and provider metadata contracts`
3. `docs: freeze Task action artifact and evidence contracts`
4. `build: scaffold Go IM service module`
5. `feat: define stable business response envelopes`
6. `feat: add deterministic plugin lifecycle registry`
7. `test: freeze plugin and envelope fault matrices`
8. `feat: compose immutable effective plugin configurations`
9. `test: freeze composition precedence and escalation diff`
10. `feat: add event store port envelope and opaque cursor`
11. `test: prove in-memory event projections rebuild deterministically`
12. `feat: define IM identity and conversation values`
13. `test: freeze ext info canonical codecs`
14. `feat: add fake IM provider port`
15. `test: prove fake provider has no network or credentials`
16. `docs: freeze Skill activation and Tool execution binding contracts`

任何一个条目若同时包含合同、实现、迁移、故障矩阵和 UI，应继续拆成小提交；列表是顺序约束，
不是要求把一整项压成一个大 commit。

## 5. 持续门禁

```bash
PYTHONPATH=src .venv/bin/python -m pytest
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/mypy src

go test ./apps/im-api/...
go test -race ./apps/im-api/...
go vet ./apps/im-api/...

pnpm lint
pnpm test
pnpm typecheck
pnpm e2e
```

只运行专项测试不能证明全阶段完成。阶段出口必须同时有合同 fixture、失败矩阵、运行证据和远端
回读。

## 6. 停止条件

- 融云官方合同无法证明稳定 ID、签名、cursor、幂等或 acceptance query 时，缺口标为
  `unsupported/unverified`，不由 adapter 猜测；
- provider 不支持 authoritative negative finality 时，发送超时永远进入 `effect_unknown`，不
  自动重发；
- Clerk claim 与平台 membership 漂移时失败关闭；
- 任何实现需要把 credential、endpoint 或组织权限交给 prompt/模型选择时停止；
- 任何阶段误触飞书、企微或未经授权的真实聊天写入时立即停止并审计。
- 任何实现把 RongCloud child group、HTTP 200、Attempt succeeded 或 A2A/MCP Task completed 当作
  BusinessTask accepted 时停止；
- 插件新增 capability/egress/secret 或 digest 漂移而未触发 diff/admission 时停止；
- 无法证明 tenant scope、offboarding revoke、unknown reconcile 或 Artifact acceptance 的关键路径
  不得贴生产可用标签。
