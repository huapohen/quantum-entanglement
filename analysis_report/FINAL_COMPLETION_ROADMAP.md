# Quantum Entanglement｜距离生产完成的最终路线图

> 路线图版本：2026-08-31-final-completion-v1  
> 评审分支：`mainline_continue_quantum_entanglement`  
> 当前代码/文档检查点：`e76a985`  
> 当前根仓目录：`main@918deae`

## 先给结论

从当前检查点到“生产 GA 完成”，还剩 **8 个必做阶段、40 个可验收工作包**。如果用户继续加入会改变
底层 event、result authority、事务边界或协议选择的参考项目，另有 **1 个条件阶段 F0** 必须先做；
没有新增参考时，F0 不阻塞，可以直接从 F1 开始。

这里的“最终完成”不是本地 demo 能启动、某个单元测试通过，或 IM 页面能显示消息，而是 Gate A–E
全部有可复现 evidence、人工批准和可回滚发布记录，并满足 [`RELEASE_GATES.md`](../docs/production/RELEASE_GATES.md)
与 [`SERVICE_BOUNDARY.md`](../docs/production/SERVICE_BOUNDARY.md)。真实飞书、企微以及任何未经单独批准的
不可逆 outbound 仍不属于本路线图的默认授权。

## 当前已经完成到哪里

- E1 provider-neutral native IM contract、strict codec、golden、zero-network fake 已完成；
- E2 offline inbound page admission、nonce/checkpoint、adapter/lifecycle、provider Mapper/Transport/Bundle
  TCK 与 durable readback 已完成；Level B 真实 provider sandbox 仍未连接；
- E3 M1–M7.5 的 Result Authority 候选、PostgreSQL migration 12 projector/reader runtime-only 闭环、
  commit ACK-loss、提交前 rollback、child-process SIGKILL 前后矩阵、partial-write rollback 和
  default-off shadow telemetry/readiness latch 已完成；
- 当前 shadow monitor 仍是进程内、无标识符、default-off telemetry；不提供跨实例指标、告警、backfill
  或 materialized primary cutover；
- 当前 Gate A、B、C、D、E 全部仍是关闭状态。关闭不等于通过，而是表示尚未获得晋级批准；
- 当前没有真实 Clerk/JWKS、生产 worker、Task/Artifact/Needs You 完整 durable projection、真实 IM
  provider、action receipt/effect reconciliation、生产 HA/DR 或 outbound。

## 阶段总览

| 阶段 | 名称 | 状态 | 主要解锁 | 依赖 |
|---|---|---|---|---|
| F0 | 新参考项目 delta 复评（条件） | 等待触发 | 冻结底层方向，避免中途改 authority | 仅当加入新参考项目时触发 |
| F1 | Result Authority 与 PostgreSQL cutover preflight | 部分完成 | 允许进入受控 materialized rehearsal | 当前检查点；不能直接切 primary |
| F2 | Trusted identity、tenant scope 与 API 安全闭环 | 未完成 | Gate A/B 的可信请求和 repository 边界 | F1 的 durable 坐标与审计合同 |
| F3 | Task/Artifact/Needs You 与 Agent 协作 durable projection | 未完成 | UI 可展示平台真相，而不是模型自报状态 | F1 + F2 |
| F4 | Worker、attempt、result acceptance 与 provider bridge | 未完成 | 受控 worker 执行和 `Accepted/Observed` 正确分类 | F1 + F2 + F3 的业务绑定 |
| F5 | 原生 IM Level B sandbox inbound 集成 | E1/E2 离线完成；Level B 未完成 | 接入独立 IM 的只读/入站沙箱链路 | provider 合同 + F2 最小 scope |
| F6 | Action receipt、`effect_unknown` reconcile 与受控 outbound | 未完成 | 可审计、可恢复的外部副作用 | F1–F5 |
| F7 | 单节点到多实例生产运营与 Gate C–E | 未完成 | 部署、备份、容量、HA/DR、告警和安全运营 | F1–F6 |
| F8 | 私有试点、RC、GA 发布与持续回归 | 未完成 | 人工批准的生产候选和最终 GA | F7 |

F1–F8 是推荐顺序，但 F5 的合同盘点、F2 的部分静态 scope 审计可以在不触碰真实网络的前提下并行；
任何并行工作都不得越过依赖阶段的 durable authority 和安全门禁。

## F0｜新参考项目 delta 复评（条件阶段）

触发条件：用户新增参考仓库、协议、框架或产品，并且它可能影响 event envelope、result authority、
事务边界、Agent 间协议或外部副作用策略。没有新增参考时记录“无触发”，不重复做历史调研。

1. 固定 URL、commit/tag、采集时间、许可证、依赖树和安全公告；
2. 运行最小示例/测试，不输入本项目凭据和真实业务数据；
3. 按 durable state、协作语义、runtime、治理、互操作、安全、产品价值、采用成本评分；
4. 输出“采用 / 适配 / 只借鉴 / 拒绝”结论和对 F1–F8 的影响；
5. 如改变底层不变量，先修订 ADR、路线图和 `SERVICE_BOUNDARY.md`，再写实现。

出口：研究证据、license/NOTICE、ADR delta、go/no-go 均进入 Git 和 Notion；未澄清影响时停止。

## F1｜Result Authority 与 PostgreSQL cutover preflight

目标：把当前 runtime-only projector 候选收口成可以被生产 preflight 审计的 durable authority，但仍不
自动打开 materialized primary。

1. 完成 Result Authority 全图的真实 process crash/kill、双连接、双 owner、reopen、restore replay
   和兼容回退矩阵；保留 COMMIT 前、COMMIT 后、ACK-loss、partial-write 的 exact evidence；
2. 在目标生产类集群生成 applied-schema digest、owner/read role、RLS/forced-RLS、函数权限和运行时
   access manifest 证明；隔离本机 PG18 证据不得替代 applied-schema 证明；
3. 完成备份、恢复、RPO/RTO、WAL/磁盘/连接断开/failover staging 演练，并记录恢复后 checkpoint、head、
   rows 的逐 scope 对账；
4. 为 replay primary → materialized candidate 制定旧 reader drain、shadow 窗口、rollback receipt、
   cutover approval 和人工撤回条件；
5. 将 projector/readiness、shadow telemetry、故障处置和回退证据汇总为独立 release evidence。

出口：所有故障 outcome 可分类、无半页状态、无重复行、无错误 Accepted；但只有人工批准后才可进入
受控 materialized rehearsal，不能直接宣布 production cutover。

## F2｜Trusted identity、tenant scope 与 API 安全闭环

目标：让每次读写都由可信认证和 action-time 授权约束，而不是依赖 caller 自报 tenant 或 scope。

1. 接入真实 Clerk/JWKS 或等价受审认证 transport，完成 key rotation、issuer/audience、时钟偏差、
   revoke 和故障关闭矩阵；
2. 建立不可伪造的 `RequestContext`、human principal、tenant membership、active Actor 和 workspace
   binding，并在 action-time 重新验证；
3. 逐 repository 强制 tenant/workspace/conversation scope，清理 legacy unscoped surface、跨租户路径
   和重复 header/ID 旁路；
4. 完成 conversation ACL、Agent installation、Task/Artifact/Needs You 读写权限和 stream/cursor
   consistency；
5. 迁移日志、异常、WAL/SHM、UI 和 telemetry 的 secret/credential/lease redaction，接入全输出
   secret-canary 与安全审计。

出口：Gate A 离线可信作用域与 Gate B authenticated loopback 证据通过；没有可信 context 时所有业务
   route fail closed，不能只凭 fake verifier 的 readiness 通过。

## F3｜Task/Artifact/Needs You 与 Agent 协作 durable projection

目标：把群聊、父群/子群工作卡、Task、Artifact、Needs You、Agent identity 和协作时间线绑定到平台
持有的 durable facts；模型输出和 LangGraph checkpoint 只能作为输入或过程证据。

1. 冻结 Task、Attempt、Artifact、Needs You、Agent installation、handoff、approval 和 conversation
   event 的 schema、版本、scope、CAS 与迁移合同；
2. 实现同事务 event/result/attempt/artifact/notification projection、幂等重放、冲突拒绝和非发射回放；
3. 为 Artifact 内容、版本、引用、下载、过期/撤回、owner transaction 和跨页面展示增加 durable readback；
4. 将父群/子群、Agent 间 handoff、shared context、interrupt、Needs You 和人工接管映射到明确的
   event/state transition，禁止 UI 自行推断完成；
5. 建立 projection lag、schema drift、reconcile-only、backfill 和 rollback runbook，并接入 F1 的
   shadow/readiness 语义。

出口：业务 UI 展示的每个状态都能回到同一 scope 内的 durable row/event；重启、重复、乱序、冲突和
   restore 不产生第二个业务真相源。

## F4｜Worker、attempt、result acceptance 与 provider bridge

目标：把当前 private worker/rehearsal seam 接到受控 runtime，但不让 worker、Agent、插件或模型绕过
平台 authority。

1. 将 admission、queued invocation job、attempt、start event、heartbeat、fence、drain、expiry 和
   result acceptance 接入真实 composition；
2. 完成 clean shutdown、取消、lease relinquish、双连接竞争、进程丢失和子进程替换；
3. 机械区分 fresh COMMIT ACK 的 `AcceptedV2`、replay/reopen/peer 的 `ObservedV2`、未知 outcome 和
   failed/expired，禁止从“模型说完成”推导成功；
4. 通过 `AgentRuntimePort` 接入 DeepSeek Harness/Cordis 的 turn/step、session event、plugin/context/
   tool/effect pipeline、compaction、sandbox/approval；LangGraph 只拥有流程/checkpoint，不拥有业务真相；
5. 接入 provider-neutral bridge、超时/限流/凭据隔离、成本/配额和 zero-effect fake，建立 provider
   failure、断流、重试和恢复 evidence。

出口：受控 fake worker 能在 restart、ACK-loss、lease race 和 result replay 后保持 exact authority；
真实 provider 仍需 F6/F7 单独批准。

## F5｜原生 IM Level B sandbox inbound 集成

目标：把已完成的 E1/E2 离线合同接到独立 IM 后端的专用、入站、只读沙箱，不打开 outbound。

1. 固定独立 IM 后端的 provider contract、endpoint class、测试 tenant/conversation、数据等级、
   read-only SecretRef、signature/timestamp/nonce/replay 规则和截止时间；
2. 完成 provider profile、mapper、transport、exchange provenance 与真实 sandbox 的 TCK/negative matrix；
3. 将 inbound adapter、bounded parser、verified page、nonce/read/checkpoint 单事务接入 F2 trusted scope；
4. 完成 disconnect/resume、duplicate、out-of-order、conflict、provider rotate、网络断开和 lifecycle
   kill switch 的真实沙箱演练；
5. 更新 `SERVICE_BOUNDARY.md`、IM readiness、权限/数据留存说明和退出沙箱的 rollback 方案；整个阶段
   禁止 Agent/tool/browser/subprocess/outbound 副作用。

出口：只生成可回读的 inbound observation，能逐 scope 重放和恢复；Level B 不等于真实 IM 商用，也不
   授权任何消息发送。

## F6｜Action receipt、`effect_unknown` reconcile 与受控 outbound

目标：把外部动作从“调用成功/失败”提升为可审计、可查询、可恢复的 effect boundary。

1. 冻结 action intent/command/attempt/approval/capability、provider operation ID、action digest 和
   receipt schema；
2. 实现 connector acceptance 与 authorization/approval revision、outbox ACK、action digest 的同事务
   绑定，默认 fake/no-op 并保持 outbound deny；
3. 覆盖 timeout、断线、ACK-loss、post-accept exception、重复、provider 查询不可用和结果冲突，
   统一分类 `succeeded | rejected | effect_unknown`；
4. 实现带查询证据的 `effect_unknown` reconcile、人工接管、幂等重试、撤销/过期与 non-emitting replay；
5. 只有单一、批准的 sandbox 通过安全评审后，才允许最小 outbound operation；飞书/企微仍需另行授权、
   独立安全评审和人工确认，不能由本路线图自动打开。

出口：任何外部副作用都有 durable receipt、operation identity、审计和回滚/人工处置路径；未知效果
   永远不能被猜成成功。

## F7｜单节点到多实例生产运营与 Gate C–E

目标：证明系统不仅逻辑正确，而且在受控拓扑、故障和容量边界内可运营。

1. 建立可复现构建、镜像、SBOM、依赖锁、迁移 runner、配置/SecretRef、TLS、least privilege 和 clean-host
   部署；
2. 完成单节点 upgrade/rollback、备份 restore、数据校验、 measured recovery、RPO/RTO 和保留策略；
3. 接入 OTel metrics/traces/logs、SLO、quota/cost、alerts、dashboard、on-call、incident 和 evidence
   retention；shadow monitor 要有外部 adapter，但仍不得泄露业务标识符；
4. 完成 worker/provider isolation、sandbox/SSRF、网络出口、租户隔离、密钥轮换、供应链、渗透/安全审计、
   容量/压测/长 soak 和资源上限；
5. 建立 PostgreSQL HA、连接池/failover、Kubernetes 或等价多实例拓扑、连续不可变 DR、定期 restore/
   failover rehearsal 与 Gate E 的长期回归。

出口：Gate C（受控私有试点）、Gate D（有限商用候选）、Gate E（多实例 GA 候选）逐项有 evidence、
   measured limits、reviewer、rollback trigger；不得用测试数量代替运营证据。

## F8｜私有试点、RC、GA 发布与持续回归

目标：把前七阶段证据组合成一个有人批准、可回滚、可持续验证的发布版本。

1. 生成 exact source/tree、migration/schema、镜像、SBOM、配置、connector allowlist、数据等级和支持
   窗口绑定的 release candidate；
2. 在 clean host 和批准拓扑执行全量测试、浏览器/API E2E、断流/恢复、容量/soak、备份恢复和安全门禁；
3. 进行有限私有试点，记录真实但批准的数据类别、SLO、故障、用户反馈、Needs You/人工接管和退出标准；
4. 完成 release review：P0/P1 清零或有书面例外、审计证据齐全、人工 owner/rollback approver 确认；
5. 发布后持续跑 migration/contract/fault/process/secret-canary/IM/provider/DR 回归，任何越界立即撤回
   promotion 并保留证据。

出口：只有 F8 的 release approval receipt 生效后，才能称为生产 GA；“代码已推送”“页面可打开”“本地
   测试全绿”都不等于 GA。

## 建议执行顺序与并行边界

1. **第一串行主线：F1**，先闭合 Result Authority 的全图 crash/kill/two-process/restore/compatibility
   和 production applied-schema preflight；materialized primary 继续关闭。
2. **第二串行主线：F2**，真实身份、tenant/workspace scope、repository predicate、ACL 和 redaction
   未闭合前，任何真实业务数据和真实 IM provider 都不能接入。
3. F3 与 F4 在 F1/F2 的 durable 合同稳定后推进；F5 可在 provider 合同获批后做 inbound-only sandbox，
   但不得越过 F2 scope。
4. F6 必须等 F1–F5 的 authority、身份、业务 projection、worker 和 IM observation 都有稳定绑定；
   默认 outbound deny。
5. F7/F8 只能在所有前置 evidence 通过后开始；每个阶段完成后形成独立 release evidence、备份分支和
   人工验收点。

## 每个工作包的提交和验收纪律

- 每个不变量、实现、测试矩阵、runbook 或 release artifact 单独 commit；实现与测试可以分开，但中间
  提交必须可构建、可运行、可回滚；
- 局部改动执行影响面专项门禁，阶段封板执行全量测试、静态检查、依赖/制品/安全/恢复门禁；
- 完成阶段先推送评审分支，再创建 `backup_MMDD_HHMMSS`；根仓 `BRANCH_CATALOG.md` 记录用途、尖端和
  worktree；不自动合并 `main`；
- 阶段文档、原始 Markdown、截图/证据 manifest 和 Notion 页面必须能按 commit/tree 回读；未回读只能
  说“已写入待核验”；
- 任何 P0、租户逃逸、数据丢失、credential 泄露、半页状态、错误 Accepted 或未经授权的不可逆副作用，
  立即停止晋级并进入 reconcile-only；
- 不向飞书、企微、个人、群聊、bot 或 webhook 发消息；真实 IM/outbound 需要额外的明确授权，不由本
  路线图隐含授权。

## 当前距离结论

按当前事实，不能用“还差一个 IM 接口”概括剩余工作。当前已具备很强的离线/隔离验证底座，但生产
完成至少还需要：

- F1 的完整恢复和生产 schema/运营证明；
- F2 的真实认证和全 repository scope；
- F3/F4 的 durable 业务 projection 与受控 worker/provider；
- F5 的真实 IM 入站沙箱合同与联调；
- F6 的 action receipt 和 `effect_unknown`；
- F7 的部署、容量、监控、安全、HA/DR；
- F8 的私有试点、发布审批和持续回归。

因此，当前最准确的状态是：**可以继续做受控 IM 前置联调，但距离最终生产完成仍有 8 个必做阶段；
Gate A–E 全部保持关闭。**
