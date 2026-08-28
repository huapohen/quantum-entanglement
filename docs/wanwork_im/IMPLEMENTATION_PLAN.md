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
- lifecycle callback 在 Host mutex 外执行、starting/stopping 单 owner 与 reentrant/concurrent 快速拒绝；
- lifecycle callback panic 固定脱敏，Start/Ready 保证 rollback，shutdown panic 不跳过后续回收；
- lifecycle duration 只作为 cooperative context deadline，不冒充 goroutine/process 强制终止；
- 第三方执行隔离 data/IPC contract：host-owned refs、generation/fence、idempotent operation、
  cancel→grace→kill→wait/reap/release receipt 与 operator-visible quarantine；
- deterministic isolation fake 明确标记 `durability=volatile/isolation=none/executesCode=false`，只验证合同；
- event port、`EventToAppend/StoredEvent`、opaque cursor 与明确为 volatile 的 deterministic contract fake；
  当前没有 production projection engine；
- IM identity/conversation 纯值合同：稳定 `ActorRef/ConversationRef` 与 revision snapshot 分离、
  realm-scoped external identity、subject/prefix binding 和 `direct/group/agent_thread` topology；
- 专用 `immetadata` codec：1024-byte、flat allowlist、受限 JCS 风格 canonical bytes、零授权
  user/group projection，以及 golden/permutation/forbidden-field/Unicode/race/fuzz 负向矩阵；
- health/readiness 与 graceful shutdown；
- unit、race、lint 和 secret canary。

W1 Plugin Host 只允许随 host 编译、由平台准入的可信内建插件；不实现也不暗示第三方 App/
Extension 任意代码生态。第三方可执行包不得加载进 API/Gateway/Plugin Host 主进程。
W1 的 `SupervisorClient` 只是独立 privileged service 的 future IPC port；当前没有 production adapter、
process/container/microVM backend 或第三方 launch route。API 不持 Docker/runtime socket、raw argv/env、
host path、raw secret 或 process handle。

出口：零 credential、零网络的 fake composition 可启动，所有业务错误 HTTP 200；effective snapshot
具备 source/digest/golden/diff，manifest/package/Secret claim 均经 host-owned admission，首次扩权需新的
批准快照；raw locator 不进入 canonical/Factory，binding view 不具备 Secret 使用权；所有 required plugins
ready 前不暴露 route，半启动/ready 失败可逆序回滚；向 fresh event fake 重放相同 append fixture 可得到
相同 StoredEvent，page backfill 可驱动 test-only pure reducer，但不宣称持久化、production projection、
SSE live replay 或 Agent/model/tool 重执行。
隔离合同还要求：同一 generation 并发 launch 只有一个 owner；old fence 不得控制新 incarnation；kill ACK
没有 exact wait/reap/release 时必须 quarantine；effectful process 即使 released 也保持 unknown/reconcile。

W1 的 Secret admission 不等于 action-time credential。KMS/Keychain、跨重启持久 claim、JIT short-lived
lease/token exchange、trusted executor 和 provider receipt 仍在 W2/W4/W7 按 Action Plane 实现。
同样，W1 的 identity/conversation/metadata 纯值合同不等于 persisted binding、membership/ACL、Clerk
验证或融云兼容；真实 authority 与 provider contract 仍由 W2/W3 闭合。

### W2：IM Domain 与 PostgreSQL

交付：组织、`ActorRef/Snapshot` 持久化、human/workload/delegation、realm-scoped external binding、
conversation ref/snapshot/aggregate、独立 membership/ACL、message、reaction、read state、
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

#### W2 当前检查点（2026-08-28）

当前工程入口见 [W2_POSTGRES_AUTHORITY_CHECKPOINT.md](W2_POSTGRES_AUTHORITY_CHECKPOINT.md)，最新深度证据见
`analysis_report/research/34_postgres_function_only_writes_and_exact_access_checkpoint.md`；Topic 33
`analysis_report/research/33_postgres_authority_persistence_checkpoint.md` 是 `0001..0004` 的前序历史检查点。

当前 code baseline 为 `cd92ea5`。当前口径必须保留：

| 标记 | 状态 |
|---|---|
| `[F]` | `0001..0005`、22 张业务表、17 张 FORCE RLS 表、五个 exact `SECURITY DEFINER` 写函数、function-only repository/receipt 路径与 exact access manifest 已在 PostgreSQL 18.6 普通/race 集成测试中通过。 |
| `[C]` | 保证仅限已登记 schema、当前四类 conversation authority repository、receipt、同一 PostgreSQL transaction，以及测试 provision/access-validator fixture；这不是生产 IaC 或服务启动接线。 |
| `[A]` | persistence substrate 的结构方向正确，可作为 authenticated admission、resolver 和 event/outbox 的底座。 |
| `[U]` | 生产 IaC/角色生命周期、真实 service pool/readiness wiring、Clerk trusted tenant、action-time resolver、restore/crash 和 event/outbox 尚未完成。 |

已交付的 W2 子集：

- checksummed `0001..0005` migration catalog、精确 ledger、PG18 major gate、session advisory lock；
- 每个新 migration 提交前累计验证全部旧 postcondition；
- fixed `search_path`、有界 rollback/unlock/close；
- token-aware DDL allowlist，并拒绝 `CREATE TABLE AS SELECT`、危险 DEFAULT、`set_config`/`pg_sleep`
  等 data-executing form；该 policy 仍不是不可信 SQL sandbox；
- authority roots、identity authority、ordinary `direct/group` conversation、RongCloud group binding、
  conversation membership/access、tenant command receipt；
- typed tenant store port、explicit tenant predicates、successor-only CAS、ignored-error poison；
- serializable UoW、same-command exact replay、request-digest conflict、receipt transaction、commit outcome
  unknown 的新连接 readback；
- out-of-band receipt conflict 在 pool handoff 前释放 command advisory lock；
- 五个固定 `SECURITY DEFINER` 函数冻结 exact signature/owner/body digest/security attributes/search path，
  repository 与 receipt 只经函数写入，runtime raw table mutation 被拒绝；
- exact authority access manifest 验证 database/schema/table/function/default ACL、owner、role attributes、
  membership options/grantor 与额外 relation/routine；真实 migration/runtime login 通过正负向 fixture；
- 真实 PG18 RLS、schema drift、64 路 exact retry、64 路 single CAS winner、rollback、unknown commit 和
  runtime immutable-history fixture。

当前禁止宣称：

- W2 已完成；
- 生产多租户/授权已完成；
- 测试 provision/access-validator fixture 已经是生产 IaC、生产 role bootstrap 或 service readiness；
- receipt 实现 provider ACK、完整结果或 exactly-once；
- access boolean 已构成 action-time gate；
- provider binding 证明融云群存在或消息送达；
- `agent_thread`、message、Task、Artifact、Acceptance 或 production event store 已持久化。

#### W2 接真实 IM 前的 P0 顺序

1. 把已验证的 `0005`/function-only/exact access 合同落到生产 IaC：显式 database owner、环境 migration/
   runtime login、secret 注入、角色轮换、cutover/旧 session 清理与 drift validator；
2. Clerk verified claim → realm binding → active principal/tenant membership → exact Actor → path consistency
   的 trusted request context；
3. conversation/actor/membership/access active resolver；invoke/publish 再叠加 installation/mandate/
   capability/budget/Artifact/Acceptance；
4. migration/role/function manifest 与真实服务 startup/readiness/DB pool composition；
5. dump/restore、DB/process restart、kill-9、old binary/future schema、role restoration 演练；
6. PostgreSQL event store/outbox/projection checkpoint、backfill+live 与 crash recovery；
7. 再接 `agent_thread`、message 与 provider adapter。

membership FK 只证明 head 存在，不证明 current membership active。移除成员的 use case 必须同一 UoW
写 `membership=removed` 与 all-false access；resolver 必须同时检查 active membership 和 permission bit。

command `result_sha256` 不能重建 typed result。command/digest canonicalization 必须版本化；replay 或
unknown resolution 后必须做 typed aggregate readback 与 revision/integrity 校验。

### W3：Clerk 与融云 adapter

交付：JWKS verification、identity mapping、W1 user/group strict codec 的 provider adapter 集成、fake
adapter、融云 provider profile/capability matrix、sandbox config 和 inbound-only readback；核实实际
size limit、原样保存、稳定回传、callback authenticity、dedupe/resume 和 mapping drift 行为。

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
10. `feat: add event store port envelope and opaque cursor`（`b666cbb` 起冻结 port；W1 P1-7 完成）
11. `feat/test: add volatile memory event store and prove deterministic event backfill`（`a4ac9bd`、
    `a0a8eea`、`034124f`、`b17bf1d`、`4a4aedb`、`9c6c457`、`f0040ea`、`0cec339`、`479bab5`、`51b5cb8`、
    `a472642`、`4118746`；没有 production
    projection engine）
12. `feat: define IM identity and conversation values`（`9f55b33`～`33ce779`；研究复核后拆分
    stable ref/revision snapshot，并增加 provider realm）
13. `test: freeze ext info canonical codecs`（`eeb05db`～`60ebf6a`；四种 golden shape、848 个
    非 canonical 排列、44 类 forbidden fields、Unicode/control、128 路 race 与 seeded fuzz）
14. `feat: add fake IM provider port`
15. `test: prove fake provider has no network or credentials`
16. `docs: freeze Skill activation and Tool execution binding contracts`
17. `feat: freeze isolated runtime admission contracts`
18. `feat: define fenced supervisor receipt protocol`
19. `test: add volatile isolation supervisor fake`
20. `feat: add checksummed Postgres authority root migration`（`8814e8d`）
21. `feat/fix: add fail-closed migration runner and cumulative schema invariants`
    （`9f72aa5`、`eac2cbb`、`bd60371`、`4afe28d`、`8d662bf`）
22. `feat/test: persist identity authority boundaries`（`457a0e1`、`d3915ca`）
23. `feat/test: persist ordinary conversation and provider routing boundaries`
    （`54f2ea0`、`1b9047b`、`371664e`）
24. `feat/test: persist conversation membership/access authority`
    （`c84f363`、`8a6e509`、`9c4a374`）
25. `feat: define tenant-bound IM store contracts and repositories`
    （`24561cc`、`69a3597`、`bf4c5d1`）
26. `feat/fix: add idempotent tenant UoW and safe receipt-conflict lock lifecycle`
    （`09511ef`、`d588695`）
27. `docs: checkpoint PostgreSQL authority persistence`（33 号 Markdown/HTML、SVG/PNG 图、W2 工程入口）
28. `feat: restrict runtime writes to fixed database functions`（`5fa2456`～`3fcc7ba`；`0005`、五个 exact
    function、repository/receipt wiring 与 runtime raw-write 负测）
29. `security/test: freeze exact authority access manifest`（`d911c51`～`cd92ea5`；测试 provision/
    validator fixture、真实 migration/runtime login 与 ACL/role/membership/MAINTAIN drift 矩阵；生产 IaC
    和 service wiring 仍未交付）

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
