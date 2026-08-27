# `2output` 调研吸收与产品合同增量审计

> 审计日期：2026-08-28（Asia/Shanghai）
>
> 证据根：`/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output/more`
>
> 开发分支：`dev_wanwork_quantum_entanglement`
>
> 审计基线：`73116ae`；Event contract 提交：`b666cbb`；当前 Plugin lifecycle 增量：`3b8e02e`

## 1. 最终结论

这批调研现在被当作产品合同的证据输入，不再只通过一份总报告间接引用。审计确认：

1. 证据目录有 40 份 Markdown、45,909 行，其中是 31 份独立产品/协议/安全报告和 9 份组合/
   方法/追踪文档；
2. 原研究导航和总报告固定的是较早的 30 份口径；晚生成的 AgentSpace 报告没有进入组合报告；
3. 原 WanWork 追踪矩阵虽然已有 38 条较强不变量，且 15 份核心源的 SHA/行数全部未漂移，但对
   17 份报告只做了间接综合、MCP 只有快照没有直接 RQ、AgentSpace 完全漏编；
4. 本轮建立 40/40 内容清单和 31/31 独立报告处置账本，并新增 RQ-039～RQ-042；
5. 新要求按 M0/W1 前置、真实 IM outbound 前置、W2+ 增量和明确延后分层，没有把所有研究机会
   机械升级成当前必做。

完整逐文件 SHA、行数和处置见 `docs/wanwork_im/RESEARCH_COVERAGE.md`；硬需求、对象、安全控制、
阶段和验收见 `docs/wanwork_im/RESEARCH_TRACEABILITY.md`。

## 2. 审计方法与证据边界

本轮只读复核本机研究快照，执行：

- 枚举全部 Markdown，核对行数和 SHA-256；
- 比较 README、product inventory、master report 与实际 `research_report.md` 数量；
- 逐份检查是否被 RQ 直接引用、仅进入快照、仅经总报告综合或完全漏编；
- 回读 AgentSpace、CodexLoom、Raft、KiroCrew、Pi、Tutti、MCP、Omnigent 等增量原文；
- 将结论写成 `研究证据 → 产品硬需求 → 领域对象/API → 安全控制 → 阶段 → 验收证据`；
- 保留 `[F]/[C]/[A]/[U]` 边界，不用产品宣传、模块名、Star、下载或协议原语证明生产能力。

本轮没有登录或操作飞书、企微、语雀，也没有向任何人、群、机器人或 webhook 发送消息。没有
使用或输出任何完整凭据。

## 3. 发现的覆盖缺口

### 3.1 31 份报告，而不是 30 份

`agentspace/research_report.md` 有 3,083 行、168,329 bytes，SHA-256 为
`9698be0f74d81c2078e208a3231f3e6498965fedeb3a3aba164764279bd8f0b7`。它生成于 2026-08-27 21:05，
晚于 README/master 约 14 小时，因此“30 份报告”是此前组合快照的准确口径，但不是当前目录事实。

正确处理不是改掉历史说法，而是：保留原 30 份组合报告时间点，并把 AgentSpace 作为独立 evidence
delta 进入 WanWork 需求和架构审计。

### 3.2 原矩阵的覆盖结构

| 级别 | 数量 | 原状态 | 本轮处理 |
|---|---:|---|---|
| 直接进入 RQ | 12 | 深度较好 | 保留并增补 AgentSpace/taint/隔离证据 |
| 只进 SHA 快照 | 1 | MCP 未被直接引用 | RQ-019/041/042 增加 MCP 原报告证据 |
| 只经总报告间接综合 | 17 | 缺逐报告处置 | 全部加入 31/31 ledger，注明直接/加强/组合/延后 |
| 完全漏编 | 1 | AgentSpace | 加 SHA、delta、RQ-040/041，并加强 6 条既有 RQ |

这说明原矩阵不是“没有参考调研”，而是核心架构已经吸收得较深、组合覆盖证明不足。修复重点是
可追溯性和几个真正改变系统边界的增量，不是推翻此前的 Task/Trust/Artifact 主线。

## 4. 四条新增硬合同

### 4.1 RQ-039：会话级代表权

证据：CodexLoom `226-234`；Raft `272-283`。

同一 Agent 在内部群、客户群、供应商空间不能使用同一份无限 representation。新增：

- `ConversationRepresentationPolicyRevision`
- `ConversationMandate`
- `DisclosureRule`
- `CommitmentLimit`
- `OutboundSpeechAct`

member 身份只能说明它在群里，不授予全部内部知识、工具、主动发送、报价或承诺权。真实 IM outbound
前必须按真实 destination、audience、purpose、data class 和承诺额度重验；错群、越额和撤权后外发
必须失败。跨组织 federation 延后，单组织内的会话代表权不延后。

### 4.2 RQ-040：Promotion Transaction

证据：AgentSpace `790-820`；YouMind `11-17,510-544`。

Runtime/provider success 只能产出候选 Artifact，不能直接变成权威文档、知识、Skill、Memory 或外部
写入。新增：

- `PromotionIntent/PromotionAttempt`
- `SourceArtifactRef/ValidationBundle`
- `PromotionReceipt/RollbackReceipt`

晋升绑定 immutable digest、MIME/magic/active-content/archive/DLP 扫描、generated Skill 的 review/sign/
sandbox、文档 base-version CAS 和外部写 exact diff。provider 成功但 promotion 失败时不能出现半权威
资产；rollback 仍保留原 Artifact 和 evidence。

它是 W2+ 产品合同。M0 只要求 accepted Artifact reference 由真人发布回父群，通用 promotion 不被
错误提升成 M0 阻断。

### 4.3 RQ-041：内容 taint 端到端传播

证据：MCP `419-423`；AgentSpace `1817-1842`；OpenWorker `63-67`；Raft `276-283`。

网页、邮件、附件、IM 消息、MCP result、Memory 和 subagent 输出默认是数据，不是授权指令。新增：

- `ContentObservation`
- `ProvenanceEdge`
- `TaintLabel`
- `AuthorityClass`
- `DeclassificationDecision`

复制、摘要、格式转换、Tool/Agent 转发不能静默去掉 taint；ActionProposal、Needs You 和 Artifact 必须
引用影响它们的来源链。只有有权主体可以做范围受限的 declassification。

M0 fake action 至少冻结 source/authority/minimal taint path，所以这是 M0 前合同阻断；完整传播、UI
和攻击语料分别进入 W4/W5/W7，不阻断当前 W1 Plugin/Event 代码。

### 4.4 RQ-042：可执行包与声明式能力分开

证据：KiroCrew `31-33`；Pi `34-39,73-79`；Tutti `41-47`；MCP `281-287,392-404`。

`SKILL.md`、Tool definition 和 App/Extension/Plugin arbitrary code 不是同一信任类。新增：

- `ExecutablePackageVersion`
- `ExecutionIsolationProfile`
- `RuntimeGrant`
- `ProcessInstance`

W1 只允许随 host 编译、平台准入的可信内建插件；第三方代码不得加载进 API/Gateway/Plugin Host
主进程。后续生态必须以独立 UID/container/microVM 限制 filesystem/network/process/env/secret/
resource，安装 lifecycle script 也属于执行。

第三方隔离实现在 W4/W7；“W1 不把任意第三方代码当普通插件加载”现在就是前置约束。

## 5. 对既有需求的加强

| 既有 RQ | 增量证据 | 加强点 |
|---|---|---|
| RQ-011 | AgentSpace 369-439 | queue 必须有 lease/fence/reclaim，claim 状态名不等于可靠 |
| RQ-012/014 | AgentSpace 579-629 | approval 是 frozen-hash transaction，decision 与 execution result 分离 |
| RQ-017 | AgentSpace 1852-1871 | App registry 需要 publisher/provenance/revoke，不只目录和 hash |
| RQ-019/033 | MCP + Omnigent | adapter 保存 capability matrix；resume/fork/approval/cost 等不能静默降级 |
| RQ-023 | AgentSpace 1748-1815 | 成本必须关联 verified outcome 和 human/retry/tool/runtime 成本 |
| RQ-026 | AgentSpace 633-674 | 高风险审计 append 失败则 dispatch 前 fail-closed；best-effort log 不是审计 |
| RQ-038 | 数据治理复核 | 组织处理批准与个人告知/确认分开，普通员工不能扩大组织路线 |

## 6. 架构修正

原组件图存在三个与正文冲突的直连：`Auth -> Clerk`、`Realtime -> RongCloud`、`Runtime -> Models`。
这会让人误以为只有有副作用的 Action 才需要统一出网控制。

修正后的职责是：

```text
Action Plane
  = 授权、批准、durable intent、idempotency、unknown/reconcile/compensation

Egress Broker
  = 所有网络的 DNS/IP/target、credential、route、data class、response budget 与审计
```

Clerk、融云 realtime、模型、只读 Tool、SSE、MCP 和 connector read 同样经 Egress Broker；Runtime/
Planner 不能直持 provider credential。Action Plane 与 Egress 互补，不能合并成一个普通 HTTP client。

## 7. 阶段门禁

### W0/M0 前必须

- 40/40 Markdown 完整 manifest 与 31/31 report disposition ledger；
- AgentSpace delta 进入 RQ 和架构；
- RQ-041 最小 source/authority/taint wire contract；
- W1 明确只允许可信内建插件；
- 修正所有网络绕过 Egress 的架构表达；
- M0 fault matrix 增加 prompt injection/taint 丢失。

### 真实 IM outbound 前必须

- RQ-039 会话级代表权、disclosure 和 commitment limit；
- Data route 的组织批准与适用的个人告知/确认；
- destination/action-time policy、unknown reconcile、wrong-group DLP 与 kill switch。

### W2+ 增量

- 通用 Promotion Transaction；
- 第三方可执行 App/Extension 的进程/UID/container/microVM 隔离；
- 全量 taint propagation 和 declassification UI；
- 跨 harness conformance matrix。

### 明确延后

- 公共 Agent marketplace、settlement、escrow、reputation 和 dispute；
- Agent fork/transfer；实施前另行冻结 provenance、license/policy inheritance、revocation notice 和
  atomic acceptance；
- 跨组织 federation/Joint Channel；
- Agent lending、HRIS 与劳动力市场。

## 8. 当前实现状态与下一步

研究审计与代码继续保持独立提交。当前已完成：

- `b666cbb feat: freeze scoped event append contracts`
- strict canonical object payload、raw payload SHA-256、immutable inline/reference payload；
- caller/store event fields 分离、scope/batch 校验、canonical event digest；
- `5b34357 fix: activate only effective plugins`：只启动 effective selection；
- `cbb7ecc fix: retain failed plugin cleanup for retry`：独立有界清理 context 与失败重试；
- `ed9a709 fix: bind effective configs to admitted manifests`：host-computed manifest digest、approved
  manifest binding、admission revision、Effective v2、frozen activation snapshot；
- `211ada7 fix: admit secret claims outside plugin composition`：raw locator 只进入准入 broker，
  tenant/row/plugin/manifest/admission/schema/purpose/audience exact bind，Effective v3、撤销和 Secret
  canary/golden/anti-replay 负向矩阵；
- `e2f82be fix: freeze plugin registry definitions before use`：完整 schema/broker/package definition graph
  重验、builder map 脱离、所有 runtime read 的 freeze 前拒绝、late registration 关闭与 concurrent race 证据；
- `0f00b47 fix: close plugin effect scopes before shutdown`：`open -> closing -> closed`、Drain 前关闭
  effect 注册、迟到/递归 cleanup 拒绝与失败项精确重试；
- `3b8e02e fix: invoke plugin lifecycle callbacks without host lock`：所有 plugin lifecycle/effect callback
  在 Host mutex 外执行，starting/stopping 可观察，reentrant/concurrent Start/Stop 快速拒绝且 owner 唯一；
- 专项普通测试、race 和 vet 通过。

Plugin Host 四个 P0 已全部完成，P1-1 Registry freeze/snapshot、P1-2 effect scope 状态与 P1-3 callback
locking/reentrancy 也已完成。
这里的“完成”不包括 action-time JIT Secret lease、第三方执行隔离或真实 connector。W1 接下来处理
callback panic/timeout honesty 等 P1，然后实现明确标记为 volatile fake 的 memory
EventStore。W2 领域建模开始前，RQ-039～RQ-042 必须进入 canonical contract/fixtures；不会用“文档
已经写了”代替测试。P0-4 的完整证据映射见
[`24_secret_claim_admission_implementation.md`](24_secret_claim_admission_implementation.md)。
