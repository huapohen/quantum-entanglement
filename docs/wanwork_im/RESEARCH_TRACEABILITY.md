# WanWork 原生 IM 研究可追溯矩阵

> 状态：W0 冻结依据
>
> 研究快照：2026-08-27
>
> 审计日期：2026-08-28
> 适用分支：`dev_wanwork_quantum_entanglement`

## 1. 这份文档解决什么

用户指定的调研不是背景阅读，而是产品合同的证据输入。本矩阵强制保留：

```text
研究结论
  -> 证据来源与快照
  -> 产品硬需求
  -> 领域模型/API
  -> 安全控制
  -> 实施阶段
  -> 可复核验收证据
```

调研中的 `[F]` 是固定源码、规范或可复现事实，`[C]` 是厂商/维护者主张，`[A]` 是分析判断，
`[U]` 是未知。产品不能把 `[C]/[A]` 改写成已经具备的能力，也不能用 Stars、下载、目录规模或
页面文案代替真实生产证据。

研究根目录：

`/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output/more/`

该目录是本机研究证据快照，未整包复制进产品仓库。全部 40 份 Markdown 的行数、SHA-256、角色
和处置见 `RESEARCH_COVERAGE.md`；下表保留直接支撑既有 RQ 的核心子集。原组合导航只包含 30 份
报告，后生成的 `agentspace/research_report.md` 是必须单独审计的 evidence delta。

## 2. 审计快照

| 来源 | 行数 | SHA-256 |
|---|---:|---|
| `agentspace/research_report.md` | 3083 | `9698be0f74d81c2078e208a3231f3e6498965fedeb3a3aba164764279bd8f0b7` |
| `_portfolio/master_research_report.md` | 1791 | `02366d7b7dcfdda96309b22142376217caff2b752e770e71a8a8e2d8cb8c2787` |
| `_portfolio/product_inventory.md` | 114 | `c47d70ff371eff0454d7ba5f046444d268294a0fafaed8831e5bf9376ded550a` |
| `floatim-floatboat/research_report.md` | 1018 | `fe239ede133ca4cc0168a20cac8e43bf0673fafb7fd1d895b69584ba82e432ac` |
| `openagents/research_report.md` | 1543 | `05f3feb8889430236e9e7211beb2004613145d8634ba5afb5840069a20a5916c` |
| `agentteams/research_report.md` | 1141 | `b9ace0fc4a8e0c8be7cf49e3a530428e097f026040fc19a6d314995e38d67a9c` |
| `clawith/research_report.md` | 1588 | `9894dbfbf6f8b1a5eca987c01d4556888160ca1a0721a4e6503ebe1d7a188bc7` |
| `deepseek-harness/research_report.md` | 1281 | `8f6cfb194a114d1b3a324db17e650a637e0962fdb1b6aefa81b84597ea0330b4` |
| `open-connector/research_report.md` | 1197 | `d43ffe461bc8362eba05bfe92f192a1218c70980d13a0350378a80ea60c41974` |
| `sandbase-harness/research_report.md` | 1702 | `03255d84e4e14694ac0018fee94db99cfac7369dd168c1fe9a4915abf145d922` |
| `holaos/research_report.md` | 1883 | `b5582eaea50c22732bd7a66ce562735e47ec80597da3038f4d595b5419601572` |
| `orca/research_report.md` | 1157 | `2d32de3d2db2eec3c9da5bec9b2ae72611aff3ceef32cb81dc4d8d80b5e5fa8f` |
| `tech-agent-security-governance/research_report.md` | 1014 | `a0c97009a04aaaaa47c5437b2d66bc8c09ef2fb7107f0af51e1dab05e64e2bb9` |
| `protocol-mcp/research_report.md` | 1199 | `6aa6bc551458c817a81dc52dbe30b5bb2589f1184f27f4831a067817d3ffd462` |
| `protocol-a2a/research_report.md` | 1024 | `21e29a0e340e74b177e975a5912c12247d71263a1e0e355d44839af27ffb8ff5` |
| `protocol-acp-dual/research_report.md` | 949 | `9541e8137812a59b8c99720fec5ccede16a36c4a2c5e2edf25642c45c4287899` |

## 3. 研究到实现的硬映射

阶段缩写：W0 需求冻结；W1 插件/事件骨架；W2 领域与持久化；W3 Clerk/融云；W4 Agent 闭环；
W5 Web/PWA；W6 Desktop/Mobile；W7 生产加固与交付。

| ID | 研究结论与证据 | 产品需求 → 领域模型/API | 安全控制 | 阶段 | 验收证据 |
|---|---|---|---|---|---|
| RQ-001 | **Task 而非 chat/session 是权威对象。** `_portfolio/master_research_report.md:46-50,128-143,635-654` | 每次正式工作建立 `BusinessTask`，包含 identity、mandate、capability、budget、context、plan、execution、Artifact、acceptance、evidence、recovery、closure；API 使用 create/plan/authorize/start/deliver/verify/close 命令。 | 消息、子群、session、MCP/A2A Task 只保存引用/投影；所有命令校验 tenant、revision、actor chain。 | W0/W2/W4 | 删除/编辑聊天、重建融云群或更换 runtime 后，Task ID、revision、状态和 evidence 不变。 |
| RQ-002 | **Task、Thread、Attempt、Action、Acceptance 状态不可混用。** `_portfolio/master_research_report.md:656-671,689-700`; `clawith/research_report.md:204-226,493-503` | 分离 `WorkConversation`、`BusinessTask`、`InvocationAttempt`、`ActionIntent`、`ArtifactAcceptance` 状态机；execution succeeded 只能进入 delivering/verifying。 | revision/CAS、非法跳转拒绝、批准参数冻结、cancel propagation、compensation。 | W0/W2/W4 | 并发推进、旧 revision、Attempt succeeded 但 verifier failed、取消后已有副作用等 fixture 均给出诚实状态。 |
| RQ-003 | **human、workload、Agent delegation 三类身份必须分离。** `_portfolio/master_research_report.md:47,673-687,909-921`; `tech-agent-security-governance/research_report.md:189-231` | `HumanPrincipal`、`WorkloadPrincipal`、`AgentIdentity`、`DelegationGrant`、`CapabilityLease`；Clerk 只认证 human，平台做 membership/authorization。 | 委托只能缩权；task/audience/purpose/TTL/PoP 绑定；离职、取消、撤销使新 lease 失效。 | W0/W2-W4 | receipt 能还原 human→Agent→workload→tool；伪造 service/Agent 身份或请求父级全权限均失败。 |
| RQ-004 | **持久 Agent 身份与 runtime session 解耦，并可 offboard。** `_portfolio/master_research_report.md:254-262,1031-1038,1185`; `agentteams/research_report.md:113-123,192-201` | 分离 `AgentDefinition/Release/Installation/Identity/RuntimeInstance`；提供 suspend/offboard/reassign API。 | 回收群 membership、claim、schedule、delegation、credential、provider projection；Memory/Artifact 按 retention 处理。 | W2/W4 | offboard 后不能读新增内容或执行；历史 Artifact 保留原 producer/version，撤权和删除证明可导出。 |
| RQ-005 | **Agent-native IM 需要一等 Agent 身份与群级触发规则。** `floatim-floatboat/research_report.md:152-163,229-240`; `clawith/research_report.md:198-226` | 人与 Agent 共享 `Actor/Participant/GroupMember/MentionIntent`；Agent 是融云普通用户；群策略默认 `mention_only`。 | `ext_info` 仅显示映射；平台回查绑定并校验 tenant、membership、Agent status、policy、budget。 | W2-W4 | 人和 Agent 使用同一成员 API；非 mention 不触发；篡改 `ext_info` 不提升权限；UI/事件/审计 actor 一致。 |
| RQ-006 | **`@Agent` 子群是 WorkConversation/Handoff 投影，不是 Task 本身。** `clawith/research_report.md:204-226,493-503`; `_portfolio/master_research_report.md:220-226` | `MentionAdmission`、`Invocation`、`WorkConversation`、`HandoffContract`；幂等键 `(tenant,parentGroup,rootMessage,agentInstallation)`。 | durable inbox、membership/object auth、context cutoff、deadline/cycle/budget guard；父群默认不继承子群 ACL。 | W3/W4 | duplicate/reorder/resume 只产生一个 Task/子群/Invocation；Agent 只在子群输出，父群仅有受限卡片和人工发布的 Artifact ref。 |
| RQ-007 | **通用群聊易复制，首版必须交付窄而完整的不可伪造结果。** `_portfolio/master_research_report.md:1040-1047,1125-1158,1427-1433` | M0 冻结“安装→入群→mention→唯一 Task/子群→Needs You→Artifact→accept→publish ref→offboard”闭环；V1 再补企业完备性和原生端。 | M0 fake provider、零真实 outbound；功能页面不能代替 fault/evidence gate。 | W0-W5 | 单条 E2E 加 9 类故障注入全部通过，并输出 evidence bundle、截图和 runbook。 |
| RQ-008 | **Everything-is-a-plugin 是受不变量约束的 capability seam。** `deepseek-harness/research_report.md:23-32,207-216,236-251`; `_portfolio/master_research_report.md:338-348` | `PluginManifest/Definition/Instance/ProviderBinding/ConsumerPort`；确定性 discover→validate→configure→start→ready→drain→stop；终态统一为 stopped/failed，最终资源清理由 host effect registry 持有。 | package trust/approval 与 manifest 分离；依赖 DAG/循环拒绝；all-ready barrier、drain 拒绝新工作、逆序清理；插件不能直写核心表或绕过 Action Plane。 | W1 | fake 替换 Clerk/融云后领域测试不变；validate/configure/start/ready/drain/stop 任一点失败及 concurrent/repeat lifecycle 后 route/listener/timer/lease 为零残留。 |
| RQ-009 | **最终组合树才是部署真相。** `deepseek-harness/research_report.md:218-234,404-414`; `agentspace/research_report.md:1852-1861`; `tech-agent-security-governance/research_report.md:239-276` | `profile -> ordered bundles -> tenant overlay -> EffectiveConfiguration`；相同 row ID 后层整行替换，显式 tombstone 删除；Attempt 保存 source revisions 与 effective digest。 | 同层冲突/跨租户 overlay/prompt/CLI/home patch 拒绝；schema/version/artifact pin；host 重算完整 manifest digest，PackageRecord 以 approved manifest digest + admission revision 绑定批准；Secret raw locator 先经 broker admission，配置只引用 claim digest/revision，Effective/Factory 只保存非 bearer binding view；capability/egress/secret/artifact/schema/binding/manifest/admission diff 交给 host-owned admission，配置不能自证批准。 | W1/W7 | manifest/config-schema/broker/claim/effective v3 golden canonical bytes/digest、bundle 优先级、整行替换、timeout/admission/Secret retarget diff、跨 scope anti-replay、撤销与 Host 漂移拒绝；首次启动和未批准扩权均阻断自动晋升。 |
| RQ-010 | **Durable event spine 是恢复/审计/流式 UI 的共同底座。** `deepseek-harness/research_report.md:255-312`; `_portfolio/master_research_report.md:183-190` | W1 冻结 `EventToAppend/StoredEvent/EventStore` port、append/read-after/project/opaque cursor 与 deterministic fake；W2 实现 PostgreSQL stream/event/checkpoint。 | store 独占 seq/globalPosition/recordedAt；单 stream expected revision 事务、strict exact retry、unknown event 保留；durable/live 分域；正文/secret 用受控 ref。 | W1/W2 | W1 fake replay/rebuild/paging 无 gap/dup且不宣称 durable；W2 crash/reopen/kill-9、projection 清库重建、backfill+live 与模型 source evidence 通过。 |
| RQ-011 | **商用 long-running 需要持久 lease/heartbeat/reclaim，不是进程内 Jobs。** `deepseek-harness/research_report.md:543-601`; `sandbase-harness/research_report.md:453-468,598-615`; `agentspace/research_report.md:369-439` | `WorkItem/Attempt/Lease/Fence/Heartbeat/RetryPolicy/DeadLetter/ScheduleOccurrence`。 | DB atomic claim/CAS、bounded retry、deadline/budget、orphan reconciler；仅当前 fence 可写终态。 | W2/W4 | claim 后 crash、双 worker、过期 owner、重复 complete、多实例 schedule 均无双终态和幽灵执行。 |
| RQ-012 | **外部写操作只能诚实建模为 at-least-once + unknown/reconcile。** `open-connector/research_report.md:441-449`; `clawith/research_report.md:421-452`; `protocol-a2a/research_report.md:307-320`; `agentspace/research_report.md:579-629` | `ActionIntent/Attempt/ExternalOperationRef/Receipt/ReconciliationCase/Compensation`；prepare/authorize/dispatch/reconcile API。 | canonical hash、稳定 idempotency key、fencing、read-after-write、provider acceptance query；无 negative finality 不盲重试。 | W2/W4/W7 | dispatch 前/中/远端成功后本地落库前杀进程；effect_unknown 可收敛；故障矩阵重复副作用为 0。 |
| RQ-013 | **HTTP 200、business accepted、provider delivered、Attempt succeeded、Artifact accepted 互不等价。** `open-connector/research_report.md:846-850`; `protocol-a2a/research_report.md:242-257` | `ApiEnvelope`、`DeliveryStatus`、`ActionStatus`、`AttemptStatus`、`AcceptanceStatus` 各自版本化。 | 网络中断不伪装为业务成功，COMPLETED 不触发发布/付款，需 readback/verifier。 | W0-W4 | HTTP 200 业务拒绝、HTTP 中断但远端成功、Attempt succeeded 但验收失败都能区分和恢复。 |
| RQ-014 | **HITL 是 Human Attention OS，不是确认弹窗。** `_portfolio/master_research_report.md:50,730-743,990-998`; `clawith/research_report.md:513-541`; `agentspace/research_report.md:579-629` | `NeedsYou/ApprovalRequest/Decision` 含 risk、loss、deadline、reversibility、confidence、parameter hash/diff、options、assignee、SLA、reason。 | trusted UI 从真实参数渲染；TTL/nonce/audience/single-use；四眼、代理审批、超时 fail-closed；改参重批。 | W2/W4/W5 | 改 recipient/amount/resource 后旧批准失效；过期/重放/跨 workspace 拒绝；最终 receipt 引用 decision。 |
| RQ-015 | **Agent 提交/完成不等于 Artifact 被接受。** `_portfolio/master_research_report.md:56,745-758`; `agentteams/research_report.md:256-283`; `protocol-a2a/research_report.md:267-281` | `Artifact/Version/Scan/AcceptanceContract/VerificationRun/Decision/PublishReceipt`；deliver/verify/accept/request-changes/publish-ref。 | immutable hash/lineage；producer/verifier 分权；URL safe fetch、AV/zip bomb/CSP/DLP/隔离预览。 | W2/W4/W5 | 恶意 HTML/URL/文件 quarantine；hash 篡改失败；未 accepted 不可发布回父群。 |
| RQ-016 | **Memory 是受治理资产，不是向量库 feature。** `_portfolio/master_research_report.md:48,715-728`; `holaos/research_report.md:365-444`; `tech-agent-security-governance/research_report.md:337-358` | `MemoryRecord/Admission/Conflict/Promotion/DerivedIndex/DeleteProof`；ingest/retrieve/use/quarantine/correct/promote/delete API。 | provenance/owner/scope/purpose/TTL/taint；先授权后检索；组织经验候选→eval→人审→发布→退休。 | W2/W4，V1 深化 | 跨群/跨 tenant 泄漏为 0；污染内容不晋升；derived index 可重建；删除传播到 cache/index/backup expiry 并留 proof。 |
| RQ-017 | **Agent Store 必须是 Trust Passport，不是图标目录。** `floatim-floatboat/research_report.md:647-681,943-962`; `_portfolio/master_research_report.md:58,923-939`; `agentspace/research_report.md:1852-1871` | `AgentRelease/TrustPassport/Publisher/CapabilityBOM/Attestation/InstallSnapshot/Verdict/Waiver/Revocation`；publish/admit/install/upgrade-diff/revoke。 | digest/signature/SBOM/provenance、static+isolated dynamic scan、data/auth/egress disclosure、quarantine/canary/rollback。 | W2/W4/W7 | 篡改包拒装；扩权需重新批准；撤销后不可新装/新 Task，在用实例受控停止。 |
| RQ-018 | **local/self-hosted/sandbox 不等于无出网或隔离成立。** `_portfolio/master_research_report.md:51-52,592-617`; `holaos/research_report.md:611-679`; `sandbase-harness/research_report.md:432-442` | `DataFlowDeclaration/ProcessingRoute/RuntimeProfile/EgressPolicy/RetentionPolicy`；按 deployment/model/embedding/telemetry/provider 披露。 | mount/process/network/secret/browser/clipboard/cleanup 逐项 profile；analytics/relay 分级开关；默认零网。 | W0/W3/W7 | 各模式抓包与声明一致；关闭 telemetry/relay 零对应出站；egress canary、mount escape 与清理测试。 |
| RQ-019 | **MCP、A2A、Agent Client Protocol 解决不同边界，adapter 存在不等于语义无损。** `_portfolio/master_research_report.md:520-527,529-580`; `protocol-mcp/research_report.md:23-65,160-202,645-664`; `protocol-acp-dual/research_report.md:13-31,138-165,595-605`; `omnigent/research_report.md:30-35,45-53,84-90` | MCP=tool seam；A2A=未来跨 Agent/组织委托；Agent Client Protocol=客户端/IDE↔coding Agent；内部使用 canonical adapter，并为 resume/fork/stream/approval/interrupt/cost/images/compaction 保存 capability matrix。 | 文档/API 禁止只写歧义 `ACP`；version/wire/schema/draft 分开；unknown 或无法保真的 capability 明确失败，禁止静默降级。 | W0，W4/W7 扩展 | 协议名称 lint、adapter golden/round-trip/differential/conformance tests；不把 Registry/Card 当信任证明；不能保真时返回 unsupported。 |
| RQ-020 | **协议互操作不等于信任互操作。** `protocol-a2a/research_report.md:579-593`; `tech-agent-security-governance/research_report.md:362-374` | `CanonicalPrincipal/Mandate/Task/ActionIntent/EvidenceGraph` 横跨 IM、MCP、A2A、browser、connector。 | 每跳传播 tenant/actor/subject/task/purpose/audience 并取 capability 交集；所有 side effect 汇入同一 Action Plane。 | W0/W2/W4 | 同一动作从不同 adapter 进入得到一致 allow/deny/approval；语义丢失 fail-closed，causal graph 连续。 |
| RQ-021 | **长期凭据不进入 Agent/model/message/ext_info/event。** `holaos/research_report.md:889-924`; `tech-agent-security-governance/research_report.md:315-333`; `deepseek-harness/research_report.md:433-459`; `sandbase-harness/research_report.md:470-504` | W1 `SecretClaimRequest -> SecretClaimReference -> SecretBindingView`；W2/W4 再实现 `SecretLease/TokenExchangeRecord/BrokeredExecution`，运行时按 typed ActionIntent JIT 解析。 | W1 raw locator 仅进 trusted admission broker，tenant/row/plugin/artifact/manifest/admission/schema/logical-name/broker/purpose/audience exact bind，error/panic 脱敏与 revoke；后续 KMS/Keychain、persistent tenant key、short TTL、audience/PoP/scope、rotation、backup/export denylist。 | W1 admission 已实现；W2-W4/W7 action-time 仍待 | W1 raw/material canary 不进入 canonical/getter/diff/Factory/error，跨 scope/漂移/伪造/撤销拒绝；全系统 Git/Notion/DB/日志/模型/IM canary 与过期/PoP/rotation/执行 receipt 仍是后续门禁。 |
| RQ-022 | **Webhook/push 是 durable delivery service，不是 Agent 任意 POST。** `protocol-a2a/research_report.md:323-342`; `sandbase-harness/research_report.md:576-596` | `DeliveryIntent/Subscription/Attempt(deliveryId,nonce,digest)/DeadLetter`；outbox delivery worker。 | HTTPS、DNS/redirect/private-IP 重验、challenge、签名/timestamp/nonce、owner binding、backoff/max-age。 | W3/W4/W7 | SSRF/rebinding/replay/duplicate/out-of-order/remote-success-local-timeout 故障测试；receiver 只消费一次。 |
| RQ-023 | **Budget 和 verified outcome 指标必须是一等对象。** `_portfolio/master_research_report.md:644,702-713,1000-1008`; `openagents/research_report.md:1113-1183`; `agentspace/research_report.md:1748-1815` | `TaskBudget/Reservation/UsageLedger/OutcomeEvaluation/ReliabilitySLO`；token、money、compute、time、attempt、attention hard limit。 | 每事件归因；并行/重试预留；超限停止并创建 Needs You；防负账/重复计费。 | W2/W4/W7 | verified task success、human minutes、duplicate side-effect、recovery completion、evidence completeness、cost/accepted outcome 可复算。 |
| RQ-024 | **Direct Reply、Quick Task、Project Task 要分层。** `agentteams/research_report.md:250-255` | `WorkMode=direct|quick|project` 与可解释 `WorkAdmissionDecision`；普通问答不强制创建 Agent swarm。 | 按风险/成本/复杂度/验收需要 admission，模型不能自行升级预算与权限。 | W2/W4/W5 | 三类请求确定路由；低复杂请求不创建冗余子群/Task；单/多 Agent 成本质量可对照。 |
| RQ-025 | **worktree 只是文件并发边界；断网只能判 runtime unverifiable。** `orca/research_report.md:205-221,239-255` | `ExecutionWorkspace/ResourceLease/ExternalNamespace/CleanupPlan`；`RuntimeLiveness=live|unverifiable|exited` 与 incarnation。 | 端口/DB schema/测试账号/云 namespace/outbound task-scoped；只有 execution host 正面证据可判 exited。 | W4/W7 | 并发 Run 不共享外部资源；断网不启动第二实例；失败清理无幽灵 worktree，未合并工作不丢失。 |
| RQ-026 | **消息透明/trace 不是完整证据。** `_portfolio/master_research_report.md:57,647-652,760-766`; `agentteams/research_report.md:614-693`; `agentspace/research_report.md:633-674` | `EvidenceNode/Edge/Chain/Bundle/RedactionManifest`；按 Task 导出 mandate→action→Artifact→acceptance。 | hash chain/签名、字段级 redaction、ACL、retention/legal hold；trace sampling 不影响审计；高风险审计 append 失败则 dispatch 前 fail-closed。 | W2/W4/W7 | 任一 accepted Artifact 可离线验签；篡改/删除/乱序能定位断点；bundle 不含 secret；best-effort 日志不能补写冒充审计。 |
| RQ-027 | **组织知识必须候选→审阅→发布→退休。** `clawith/research_report.md:307-352` | `ExperienceDraft/Revision/PublicationDecision/Citation/AdoptionFeedback/Retirement`。 | 未发布不进入权威检索；source 撤回使经验失效；promotion 需 eval/reviewer/version/rollback。 | V1 后半（W4/W5 后） | AI 提议不自动发布；每次引用回溯 source/version/reviewer；撤回/退休版本不再影响新 Run。 |
| RQ-028 | **RongCloud 是 transport/projection，不是 business source of truth。** `openagents/research_report.md:341-363,466-528`; `holaos/research_report.md:595-607` | `InboundChannelBinding/ExternalIdentityMapping/ProviderProjection/TransportReceipt`；平台持有 organization、ACL、admitted message、Task、Artifact。 | webhook authenticate/dedupe、成员映射、附件 malware/prompt-injection、wrong-group/output DLP、kill switch。 | W0/W2/W3 | provider history/group 被删除或伪造时平台权限不变；fake↔RongCloud adapter 不改变 canonical contract。 |
| RQ-029 | **移动审批应签结构化 capability，不搬运泛化终端权限。** `orca/research_report.md:319-325,667-669` | `AttentionPresentation/ApprovalCapability/DeviceSession`；移动端展示 diff、风险、资源、可逆性和测试证据。 | device revoke、短 session、生物/step-up auth、task/action scope；高风险独立 capability。 | W5/W6 | 丢失/撤销设备不能批准；一次审批不能换成文件、Git、shell 或其他资源泛权限。 |
| RQ-030 | **不自造新的公网 Agent wire 协议；自行设计平台内部信任合同。** `_portfolio/master_research_report.md:516-580`; `openagents/research_report.md:466-528` | 自研 canonical mandate、Task、Action Ledger、Artifact Acceptance、Evidence/Recovery；外部以 MCP/A2A/Agent Client Protocol adapter 接入。 | adapter 只翻译，不拥有 authority；版本/conformance/peer trust 单独准入。 | W0，W4+按需 | 内部合同不依赖任一 provider/protocol；真实跨组织需求出现前不发布伪标准。 |
| RQ-031 | **Skill 必须渐进披露，并把一次激活冻结到不可变包快照。** `clawith/research_report.md:356-401` | `SkillPackageVersion/ActivationReceipt/MaterializationManifest`；catalog 只投影轻量索引，完整读取 `SKILL.md` 后才激活，辅助文件按 manifest 物化。 | receipt 绑定 `packageVersion + objectVersion + contentDigest`；Run 中途升级不漂移；部分物化、摘要不符、未审核包 fail-closed。 | W2 建模；M0 后 W4/W7 实现 | 同一 Run 激活后修改上游 Skill，后续 step 仍读取原字节；缺任一必需文件不能执行；未批准包不能产生 activation receipt。 |
| RQ-032 | **Tool Definition、Agent Assignment 与一次 Execution Binding 是三种授权对象。** `clawith/research_report.md:403-419` | `CapabilityDefinitionVersion/AgentCapabilityAssignmentRevision/ExecutionBinding(routeDigest,credentialRef,policyRevision)`；执行前重验 assignment/route/credential/membership。 | secret 仅保存 opaque ref；stale assignment、route drift、跨 Agent 绑定、未批准参数在 action-time fail-closed。 | W2 建模；W4/W7 实现 | 运行前禁用 assignment 可阻断旧 Run；route 漂移触发重新准入；checkpoint/event/模型/IM 中 secret canary 为零。 |
| RQ-033 | **长期单位是 Environment；每次 Run 只消费受治理的 projection。** `holaos/research_report.md:215-255,461-488`; `omnigent/research_report.md:30-35,45-53,84-90` | `EnvironmentRevision/RunCompilation/RunCapabilitySnapshot/ProjectedContextManifest`；Attempt 冻结 environment/effective config/model/runtime/capability/egress digests。 | hot/warm/cold 分层；projection compiler 最小化可见/可做范围；跨 harness resume/fork/stream/approval/interrupt/cost/images/compaction 缺能力时 fail-closed；promotion boundary 阻止未审核内容进入 durable state。 | W1 effective config；W2 建模；W4 runtime | 换模型/runtime 后从 bounded canonical state 恢复；未投影能力从任何 adapter 入口都失败；无法保真不迁移；未 promotion 内容不进入长期环境。 |
| RQ-034 | **Planner/model 不能持有 privileged actuator 权限。** `agentteams/research_report.md:373-404,431-450` | `ActionProposal -> PolicyDecision/Approval -> ExecutorCommand -> ActionReceipt`；Runtime 只提 typed proposal，Executor 独立。 | Runtime 无 provider credential/Docker socket/宿主 home；执行器按 tenant/task/capability/参数 action-time 重验。 | W2 Action port；W4 实现；W7 硬化 | prompt injection 不能从 Runtime 直达副作用；绕过 Action Plane 的 Tools/Peers/RongCloud 路径为零。 |
| RQ-035 | **所有 MCP/connector 出网必须经过统一 Egress Broker。** `clawith/research_report.md:439-452`; `holaos/research_report.md:571-587` | `EgressIntent/ResolvedTarget/ConnectionLease/ResponseBudget`；per-capability `effect/dataClass/approval/retry/idempotency/reconcile`。 | DNS/IP pin、private/metadata 拒绝、redirect/SSE 每跳重验、跨 origin 剥离认证、domain/port allowlist、响应 byte/time/schema 上限。 | W2 port；M0 可 fake；W4/W7 实现 | SSRF/rebinding/redirect/credential forwarding/oversize/slow stream 全拒绝；MCP 写响应丢失进入 unknown 而非换 transport 重放。 |
| RQ-036 | **Routine/Timer 是独立产品对象，不只是底层 schedule occurrence。** `openagents/research_report.md:174-190` | `RoutineDefinition/TriggerPolicy/MissedRunPolicy/ScheduleOccurrence`；冻结 owner、timezone、Task template、budget、approval 和 pause state。 | stable occurrence key、scheduler lease/fence、DST/错过边界、bounded backfill；offboard/预算耗尽停止新 occurrence。 | W2 建模；M0 后 W4/W5 实现 | scheduler 重启/重复 tick/DST 不重复 Task；skip/run-once/backfill 可预测；预算耗尽和撤权后零新执行。 |
| RQ-037 | **Agent Presence、provider online 与 runtime liveness 是三种状态。** `floatim-floatboat/research_report.md:896-913`; `orca/research_report.md:239-255` | `AgentPresenceLease(online/working/waiting/human_takeover/unverifiable)`、`RuntimeIncarnation/Liveness`；群/目录只投影。 | lease expiry 停止新 admission；unverifiable 不判 exited/不重跑；human takeover 推进 fence，旧 runtime 不能写终态。 | W2 建模；W4 实现 | 断网显示 unverifiable 且不启动第二实例；过期 Agent 不接任务；takeover 后旧 incarnation 恢复也无法产生副作用。 |
| RQ-038 | **每条 Agent 数据路线必须版本化披露并在变化时重新批准/告知。** `floatim-floatboat/research_report.md:502-517`; `holaos/research_report.md:595-607` | `DataRouteDescriptor/ProcessingRoute/OrganizationProcessingApproval/PersonalAcknowledgement`；installation/Attempt 冻结 route revision，成员卡可回看 operator/host/region/model/retention/training。 | unknown host/route 默认拒绝；组织 allowlist/DLP；路线扩大或 host/model/region/training/retention 变化重新 policy；普通员工不能扩大组织处理路线，也不能把组织处理的全部合法基础伪装成个人 consent。 | W2 模型；W3/W4 接入 | 未知第三方 host 不能交互；route revision 变化使旧组织批准/个人告知按适用规则失效；抓包/evidence 与披露一致。 |
| RQ-039 | **同一 Agent 在不同会话中有不同代表权、披露边界与承诺额度。** `codexloom/research_report.md:226-234`; `raft-slock/research_report.md:272-283` | `ConversationRepresentationPolicyRevision/ConversationMandate/DisclosureRule/CommitmentLimit/OutboundSpeechAct`；内部群、客户群、供应商空间分别编译。 | membership 不授予全部内部知识/工具；按 audience/purpose/data class/主动发送/承诺额度缩权；撤权立即阻断新外发。 | W2 建模；W3/W4 接入；跨组织 federation 延后 | 同一 Agent 在内部/外部群得到不同 context/tool/knowledge/commitment；错群输出、越额承诺和撤权后发送均失败。 |
| RQ-040 | **Runtime 输出晋升为权威文档、知识、Skill 或外部写入必须是独立事务。** `agentspace/research_report.md:790-820`; `youmind/research_report.md:11-17,510-544` | `PromotionIntent/Attempt/SourceArtifactRef/ValidationBundle/PromotionReceipt/RollbackReceipt`；provider completion 只产生候选 Artifact。 | immutable digest；MIME/magic/active-content/archive/DLP；generated Skill review/sign/sandbox；文档 base-version CAS；外部写 exact diff；可 rollback。 | W2 建模；M0 后 W4/W7 实现 | provider 成功但 promotion 失败无半权威资产；并发人工编辑冲突；恶意产出隔离；crash resume；rollback 保留原 Artifact/evidence。 |
| RQ-041 | **不可信内容不能产生 authority，taint 必须经过复制、摘要、Tool 和 Agent 链端到端传播。** `protocol-mcp/research_report.md:419-423`; `agentspace/research_report.md:1817-1842`; `openworker/research_report.md:63-67`; `raft-slock/research_report.md:276-283` | `ContentObservation/ProvenanceEdge/TaintLabel/AuthorityClass/DeclassificationDecision`；ActionProposal 与 Artifact 保存来源/taint path。 | 数据与指令分离；摘要/转发/tool result 不自动提升信任；approval 展示来源链；只有显式授权主体可 declassify。 | W2 建模；W4 enforcement；W5 UI；W7 攻击语料 | 网页/邮件/附件/MCP result 注入不能授予权限或外传 secret；复制/摘要后 taint 不丢；删除标签导致 admission 失败。 |
| RQ-042 | **可执行 App/Extension/Plugin 与声明式 Skill/Tool definition 是不同信任类。** `kirocrew/research_report.md:31-33`; `pi-agent/research_report.md:34-39,73-79`; `tutti-vm/research_report.md:41-47`; `protocol-mcp/research_report.md:281-287,392-404` | `ExecutablePackageVersion/ExecutionIsolationProfile/RuntimeGrant/ProcessInstance`；W1 只允许 host 编译并准入的内建插件。 | 不可信包不进 API/Gateway/Plugin Host 主进程；独立 UID/container/microVM；限制 fs/network/process/env/secret/resource；安装 lifecycle script 也属于执行。 | W1 冻结边界；第三方实现 W4/W7 | 恶意包不能读宿主 home/env/其他 tenant/metadata，不能撞垮核心 API；撤销/结束后零残留。 |

## 4. 明确吸收与明确拒绝

| 对象 | 吸收 | 不照搬 |
|---|---|---|
| FloatIM/Floatboat | 一等 Agent 身份、群规则、`mention_only`、Agent-native messaging | 只有头像和群聊；把第三方 Agent host/数据路线藏起来；群消息冒充任务真相 |
| Clawith | Participant、Handoff、Needs You、Experience/Skill/Tool 分层、Action unknown | Agent 自安装能力、best-effort 高风险审计、host fallback、把内部协议宣传成标准 A2A |
| DeepSeek Harness | capability seam、可逆插件生命周期、profile/bundle、durable event spine | 同 UID secret、MCP 裸宿主、process-local Jobs、未认证插件即信任 |
| OpenAgents | workspace 控制面、Task/Routine/Inbox、canonical protocol adapter | owner-equivalent machine token、URL token、legacy fail-open、把浏览器代理当 sandbox |
| AgentTeams | 透明 room、对象化 HITL、submission/acceptance 分离 | 自然语言 Manager 持 Docker/socket/full access、挂宿主 home、Matrix timeline 冒充审计 |
| holaOS | Environment、canonical memory、run capability projection、Data Nutrition Label | `local=offline/private`、依赖 harness 原生审批、unsigned grant/raw token、全局 cookie/明文 secret |
| Orca | worktree fleet、structured mobile approval、evidence bundle、unverifiable liveness | worktree=完整隔离、看不到进程=退出、remote/read-mostly=安全、热度=accepted outcome |
| Open Connector | 大规模 schema/executor 组织、OAuth gateway、连接器维护知识 | 目录数量=真实 E2E；开发友好 fail-open；只声明 scope 却不在执行点强制 |
| MCP/A2A/Agent Client Protocol | 标准化能力/Agent/客户端边界，建立 adapter 与 conformance | Registry/Card/Task state=信任/身份/验收；三个协议混成“Agent 协议” |
| AgentSpace | 数字员工控制/执行面分离、approval transaction、output promotion、outcome ledger | 把 Runtime output 直接写成知识/Skill/文档；无 lease queue；`LocalSandbox` 名称冒充隔离 |
| CodexLoom / Raft | 会话级 Agent 代表权、跨边界 disclosure/commitment、身份连续性 | organization/membership 声明冒充资源授权；activity/transcript 冒充审计 |
| KiroCrew / Pi / Tutti | 小内核、App/Extension/Room 的真实信任边界、完整性与隔离分层 | 用 Tool gate、UI sandbox 或 package hash 为同进程第三方代码背书 |
| Omnigent | 跨 harness capability matrix 与 conformance regression | adapter 能启动就宣称 resume/fork/approval/cost 等语义无损 |

## 5. 延后但不遗忘

以下不是 M0 首发内容，只冻结扩展点；它们需要真实客户、跨组织需求或失败数据才能进入实施：

- 公共 Agent marketplace、自动结果计费、escrow、dispute、reputation 和跨组织 federation；
- Agent HRIS、绩效、劳动力市场和跨公司 Agent lending；
- Agent fork/transfer；进入实施前必须冻结 provenance、license/policy inheritance、revocation notice 和原子接收流程；
- 全量组织 Memory 因果学习、FinOps 自动路由、跨 harness 无损迁移认证；
- 生产 E2EE、跨区域 active-active、公开 A2A gateway 和第三方插件市场；
- 把所有飞书/企微/钉钉连接回产品。当前任务明确保持飞书/企微零发送。

## 6. 冻结与变更纪律

1. 改动 RQ-001 至 RQ-042 任一结论，必须同时修改 PRD/Architecture/contract/test，并单独 commit；
2. 研究源文件变化时先比对 SHA-256，再进行 evidence delta review，不能静默沿用旧行号；
3. 若 provider/协议官方合同不支持稳定 ID、acceptance query、签名、cursor 或幂等，标记
   `unsupported/unverified`，不得让 adapter 猜测；
4. 实现“看起来能工作”但无法给出本矩阵验收证据时，该条仍是未完成；
5. Git/本地文档是 canonical source，Notion 是稳定节点镜像；两者不一致时停止标记已同步并修复。
