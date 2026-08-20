# 03｜Agent 协议全景与选型

> 调研截止：2026-08-19（Asia/Shanghai）。事实优先取自官方规范、官方文档、官方仓库与正式发布页；“建议”与“协议事实”分开标注。
## 0. 先给结论
WanWork **应自研内部 Coordination Envelope（协调信封）及其持久化状态机，但不应自研一套对外通用 Agent 协议**。
应自研的，是产品不可外包的内部语义：群聊参与者与发言归属、任务/子任务依赖、并发与幂等、上下文选择与压缩、Artifact 版本链、人类审批、预算/截止时间、审计和可恢复执行。它是 WanWork 的领域内核，而不是新的公网标准。
不应自研的，是已有标准覆盖且需要生态网络效应的边界：外部 Agent 调用采用 A2A 1.x；工具与资源接入采用 MCP；Agent Card 作为基础发现格式并可映射 AGNTCY OASF；身份、OAuth/OIDC、JWS、TLS、OpenTelemetry 均复用现有标准。ANP 的 DID、端到端消息与联邦能力先保持适配器级观察，不进入 MVP 内核。
协议必须分层，不能把一个格式强行承担所有职责：
1. **产品交互层**：人、Agent、群聊、@、审批与交付体验（WanWork 自研）。
2. **协调语义层**：Command/Event Envelope、Task 状态机、Context Manifest、Artifact/Approval/Budget/Policy（WanWork 自研）。
3. **外部互操作层**：A2A（Agent↔Agent）、MCP（Agent↔Tool/Resource）、Agent Card/OASF（描述与发现）。
4. **传输与安全层**：HTTP/SSE/Webhook/消息总线、OAuth/OIDC/mTLS/JWS、trace context；均通过绑定或网关实现。
正确策略不是“选 A2A 还是自研协议”，而是：**内部 canonical envelope + A2A/MCP/OASF 边缘适配器**。外部协议升级时只改 bridge，不污染群聊和编排核心。
## 1. 名词先消歧
业界存在两个完全不同的 ACP，不能混用：
1. **Agent Communication Protocol**：IBM/BeeAI 发起的 Agent 通信协议，仓库 `i-am-bee/acp` 已归档，官方 README 明确宣布并入 Linux Foundation 下的 A2A。新项目不应再把它作为独立主协议实现。
2. **Agent Client Protocol**：源于 Zed，现位于 `agentclientprotocol/agent-client-protocol`，用于代码编辑器与 coding agent 通信；当前稳定 wire protocol 为 1。它不是 Agent↔Agent 协议。
此外：
- A2A 是 Agent 应用之间的任务/消息互操作；
- MCP 是模型应用与工具、资源、prompt 服务器之间的上下文协议；
- ANP 是身份、发现、协商、安全消息、群组和联邦的更大协议族；
- AGNTCY 是一组基础设施项目，不是一个单一 wire message 格式；
- CloudEvents 是事件外壳标准，不定义 Agent 任务语义；
- 各类框架的 handoff/team/message API 是框架内抽象，不天然是跨实现协议。
## 2. A2A 1.0：外部 Agent 互操作首选
### 2.1 已核验事实
截至 2026-08-19，官方规范页面标为 **Version 1.0**，GitHub 最新 release 为 `v1.0.1`（2026-05-28），主仓库 `a2aproject/A2A` 未归档、Apache-2.0。官方定位是让不同框架、公司和服务器上的 opaque agentic applications 协作，而不暴露内部 memory、tools 或实现。
官方 README 列出的核心能力：
- Agent Card 能力发现；
- JSON-RPC 2.0 over HTTP(S)；
- 同步请求、SSE 流式更新、异步 push notification；
- text/file/structured data；
- 长任务与企业认证/可观测性；
- Python、Go、JS、Java、.NET、Rust 等 SDK。
### 2.2 A2A 能解决什么
适合：
- 从 Agent Card 发现远程 Agent 的 skills、input/output modes、endpoint 与安全方案；
- 向 opaque remote agent 发送 message；
- 获取 task/status/artifact、流式订阅进度、取消远程任务；
- 跨框架边界调用，例如 LangGraph Agent 调企业自研 Agent；
- 对合作方隐藏内部 prompt、memory 和工具。
不解决或不应委托给它：
- WanWork 群成员、组织岗位与租户权限；
- 内部 DAG 和多个 Agent 的 ready set；
- artifact 作为公司业务对象的版本、回滚和影响图；
- 谁批准了外部副作用；
- 群聊消息投影、未读、@ 语义；
- 全局成本预算与企业审计保留策略。
A2A task 是远端交互状态，WanWork TaskAttempt 是内部责任与治理状态。二者要映射，但不能使用同一个主键或让远端 task 覆盖内部状态。
### 2.3 推荐映射

| WanWork | A2A | 规则 |
| --- | --- | --- |
| AgentRegistration | Agent Card | 保留 card 原文和 digest；平台另存组织授权 |
| TaskAttempt assign | `message/send` | Envelope 放在 data part 或明确 extension；不只发送自然语言 |
| progress event | task/status update | 远端状态归一化为内部事件，保留 remote task id |
| ArtifactRef | A2A artifact/part | 大文件传 URI + digest + media type；内部版本仍由平台分配 |
| cancel command | `tasks/cancel` | 记录请求与远端 receipt；取消不等于已停止 |
| correlation/trace | metadata/HTTP trace context | 不能依赖 remote 原样返回，gateway 维护映射 |
| authority | 安全凭证 + internal policy | 不把完整企业策略泄露给远端，只给最小 capability |


### 2.4 实现注意
- Agent Card 是外部输入，需 SSRF 防护、URL allowlist、schema/签名/缓存与变更审计。
- `message/send` 的网络重试必须绑定稳定 request/idempotency；不能每次生成新业务任务。
- SSE 断线后要按 task id 重连或回查，不能把 transport 断线判为任务失败。
- push webhook 要校验签名、去重、处理乱序。
- A2A 1.0 与早期 0.2/0.3 字段存在变化；adapter 必须按 protocol version 选择 codec。当前本仓库 adapter 是无依赖的稳定子集，仍需官方 SDK 合同测试。
## 3. MCP：工具和数据边界，不是多 Agent 编排
官方仓库当前主 schema 位于 `schema/2025-11-25`，规范使用日期版本。MCP 基于 JSON-RPC 的 client/server 生命周期，典型 server 能提供 tools、resources、prompts；client 侧还可协商 roots、sampling、elicitation 等能力。
适合：
- 文件、数据库、搜索、SaaS API、知识资源的统一接入；
- tool schema、resource URI、prompt template 的发现；
- 把外部能力放进 DSH/LangChain/自研 Harness 的工具管线；
- 对每个 workspace/Agent 注入不同 server 和访问政策。
不适合：
- 表达“研究员完成后架构师才能开始”的 DAG；
- 表达 Agent 团队成员关系、handoff 验收或 artifact version；
- 让一个 MCP server 充当拥有自主责任的群聊 Agent；
- 代替平台的用户批准、审计或任务恢复。
“Agent 通过 MCP 调另一个 Agent”技术上可以包装成 tool，但会把长任务、身份、进度、取消和双向交互压扁成工具调用。对简单同步能力可接受；对真正远程 Agent 应用优先 A2A。
安全建议：server 配置按租户和 Agent 隔离；OAuth/token 留在 connector vault；工具调用先进入统一 PolicyEngine；resource 内容视为不可信输入；sampling/elicitation 不得绕过平台对模型调用和用户交互的记录。
## 4. 两种 ACP 的决策
### 4.1 Agent Communication Protocol：迁移到 A2A
旧 ACP 有 Agent Manifest、Run、Message/MessagePart、Await、Session，曾支持同步/流式/后台执行和分布式 session。其思想仍可参考，尤其是 Await 和多模态 part；但官方仓库已经归档并明确并入 A2A。WanWork 只做旧系统迁移 adapter，不对其新增核心能力。
### 4.2 Agent Client Protocol：桌面 Coding Agent 插件边界
Agent Client Protocol 标准化 editor/client 与 coding agent。它通过 initialize 协商 `protocolVersion` 和 capabilities，当前稳定 protocolVersion 为 1；schema release 版本与 wire version 分离。
它适合未来 WanWork 桌面端嵌入 Coding Agent：编辑器提供文件/终端/权限 UI，coding agent 提供 session、plan、tool 等交互。它不负责多个业务 Agent 的组织协作，因此放在 `CodingAgentClientAdapter`，不要与 A2A adapter 共用名称或状态机。
## 5. ANP：值得跟踪的 Agent 网络协议族
官方仓库把 ANP 1.1 描述为身份、命名、描述、发现、安全消息和应用协议套件，核心包括：
- `did:wba` 身份与认证；
- WNS handle 与 DID 解析/轮换；
- Agent Description 与主动/被动 Discovery；
- 基于 JSON-RPC 的 direct/group messaging；
- E2EE、附件、联邦与 mention profiles；
- AP2 payment；
- meta-protocol 仍是 draft；Messaging 1.2 multi-device 仍是 draft。
它比 A2A 更接近“Agent 互联网 + 群聊联邦”，尤其 P4 group、P8 federation、P9 mention 与 WanWork 长期形态相关。但 MVP 不应直接采用 DID、MLS、多域联邦和支付作为内部前提，原因是复杂度和生态成熟度不匹配。
建议：
- 把 ActorRef、GroupRef、Mention、Attachment、Receipt 设计为可映射 ANP；
- 在协议网关做实验性 adapter；
- 不让 DID 成为企业内唯一 identity，内部先接现有 IAM/OIDC；
- 等跨组织协作、E2EE 和联邦成为真实需求后再引入；
- 生产采用前核验 spec draft/released 状态和 AgentConnect SDK 覆盖度。
## 6. AGNTCY：发现、目录与安全传输基础设施
AGNTCY 不是单一协议，至少包含三块对 WanWork 有价值的能力：
### 6.1 OASF
Open Agentic Schema Framework 用 skills、domains、modules 描述 Agent record，支持私有扩展和 schema version immutability。官方 `agntcy/oasf` 最新 release 为 `v1.1.0`（2026-07-10），Apache-2.0。
适合把 WanWork Agent Card 扩充为企业可搜索的能力目录，也可与 A2A card 相互转换。OASF 是描述/发现 schema，不是运行协议。
### 6.2 Directory
Directory 使用 OASF record 做分布式发布、发现和匹配，提供 verifiable claims、content addressing、DHT、签名和版本关系。适合未来跨组织 Agent registry；MVP 中 PostgreSQL/搜索服务足够，不必先部署分布式目录。
### 6.3 SLIM
Secure Low-Latency Interactive Messaging 是可承载 A2A/MCP 的 transport：数据平面、可靠 session/MLS E2EE/group membership、控制平面分离，并提供 A2A/MCP/OTel integration。
适合大规模跨域 Agent 网络或严格 E2EE；不替代 A2A application semantics，也不替代 WanWork 内部 EventStore。MVP 先用 HTTP/SSE/Webhook/消息队列，transport port 保持可换。
## 7. 框架内部“协议”能借鉴什么
### 7.1 OpenAI Swarm / Agents SDK handoff
handoff 通常被呈现为一个特殊 tool call，把 control 交给另一个 Agent；Agents SDK 还提供 session、guardrail 和 tracing。这种 UX 简洁，适合单进程 Agent 协作，但不是跨厂商 wire protocol。
可借鉴 handoff input filter、handoff description 和 trace；WanWork 必须额外有显式 acceptance criteria、deliverable、authority、deadline/budget 和平台任务状态。
### 7.2 AutoGen Core / AgentChat
AutoGen Core 的 routed agents、message handlers、topic/subscription 和 distributed runtime 体现 actor/event-driven 设计；AgentChat 的 teams 提供 SelectorGroupChat、RoundRobin 等上层模式。
可借鉴 typed message、actor address、topic、termination condition；但框架 runtime state 不应成为群聊产品的跨版本真相源。
### 7.3 CrewAI / CAMEL / LangGraph / Deep Agents
CrewAI 的 Crew/Flow、CAMEL 的 role-playing society、LangGraph 的 Command/Send/interrupt、Deep Agents 的 subagent task tool 都是编排 API，而非组织级协议。
它们验证不同协作模式，但 adapter 的正确层级是 `AgentRuntimePort` 或 `WorkflowEnginePort`。不要强迫外部 Agent 安装同一框架才能加入 WanWork。
### 7.4 FIPA ACL 与 Contract Net
传统 FIPA ACL 的 performative（request/inform/propose/accept/reject）和 Contract Net 的招标/投标/授予/结果对长期开放任务市场仍有参考价值。现代实现可把这些语义放进 Envelope kind 与 task negotiation，不必复活完整旧协议栈。
## 8. CloudEvents、OpenTelemetry 与安全标准
内部事件总线可采用 CloudEvents v1.0.2 的通用属性思想（id/source/type/subject/time/datacontenttype/specversion），但领域 payload 和 stream revision 仍由 WanWork 定义。CloudEvents 解决“事件如何被通用基础设施识别”，不解决“task.completed 是否合法”。
追踪使用 W3C Trace Context/`traceparent` 与 OpenTelemetry；correlation id 关联业务目标，causation id 关联直接原因，trace id 关联一次分布式执行，三者不能混为一个字段。
认证授权复用 OAuth 2.1/OIDC/mTLS/JWS/SPIFFE 等现有机制。Coordination Envelope 的 `Authority` 是已委托能力的业务表达，不是 bearer credential；credential 只能存在 vault/connector，不进入事件 payload。
## 9. WanWork Coordination Envelope v0.x 建议
### 9.1 为什么需要
A2A/MCP 都故意不拥有 WanWork 的内部组织语义。如果不定义 canonical envelope，这些关键字段会散落到 IM webhook、LangGraph state、prompt、数据库列和 vendor metadata，最终无法统一审计和迁移。
因此“自研”的本质是稳定领域语言和兼容策略，不是再发明 HTTP 或 JSON-RPC。
### 9.2 Header
```plain text
schema_version
message_id
tenant_id / workspace_id / session_id / thread_id
sender / on_behalf_of / recipients / audience
kind + payload_schema
timestamp / ttl / priority
correlation_id / causation_id / idempotency_key / traceparent
authority_ref / policy_version / data_classification
context_manifest_ref / artifact_refs / reply_to
signature_ref（只在跨信任域需要）
```
当前代码已实现其中稳定子集，下一版本应增加 tenant/workspace、on_behalf_of、payload schema、policy version 和 data classification，而不是把所有东西塞入自由 metadata。
### 9.3 Command 与 Event 分开
推荐在语义上区分：
- Command：请求某 actor 做事，可被拒绝，使用 imperative kind；
- Event：已经发生的事实，不可被“撤销”，只能用后续补偿事件；
- Query：无副作用读取，不改变领域状态；
- Signal：进度/typing/heartbeat 等可丢失瞬时信息，不必全部写主事件流。
对外可以共享同一个 Envelope header，但 store 和 handler 要按类别执行不同的不变量。
### 9.4 核心 payload
- `TaskAssigned/Accepted/Started/Waiting/Completed/Failed/Canceled/Superseded`；
- `HandoffOffered/Accepted/Rejected`，含目标、验收、输入、deliverable、权限、预算；
- `ArtifactVersioned/Restored`，含 digest、parent、producer、task attempt；
- `ApprovalRequested/Decided/Expired`，绑定 action digest；
- `ContextCompiled`，含 selected/omitted refs、budget、compiler version、digest；
- `AgentRegistered/VersionActivated/Suspended`；
- `ChatMessageReceived/Projected/DeliveryFailed`；
- `ExternalActionProposed/Dispatched/Receipted/Compensated`。
### 9.5 状态机与幂等
- 每个 stream 有单调 revision 和乐观并发；
- idempotency scope 至少是 tenant + operation + external request；
- 合法回到同一状态用 transition revision 区分，不能只用 target status；
- handler 先检查当前 projection 和 command precondition；
- event + outbox 在同一事务；外部 receipt 单独记录；
- artifact 与 task completion 要么原子，要么以 Saga/outbox 明确补偿；
- 重放只重建状态，不自动重发外部副作用。
## 10. 版本与兼容策略
1. Envelope schema 使用明确 major/minor；新增 optional 字段向后兼容，改变含义升 major。
2. 每种 payload 有独立 schema id/version，避免整个 envelope 随业务字段频繁升版。
3. consumer 声明支持范围；未知 event 持久保存并进入 dead-letter/upgrade 路径，不静默丢弃。
4. 外部 adapter 固定 A2A/MCP/ACP/ANP 版本并做 golden fixtures/官方 SDK contract tests。
5. Agent Card/OASF record 保存原文、fetch time、ETag、digest 和验证结果。
6. 事件日志保留 upcaster；不原地重写历史事件。
7. 跨信任域签名覆盖 canonical bytes 和关键 header，内部数据库则依赖访问控制与 tamper-evident audit。
## 11. 最终决策表

| 能力 | 选型 | 时机 |
| --- | --- | --- |
| 外部 Agent 调用 | A2A 1.x | MVP |
| 工具/数据 | MCP 2025-11-25 兼容层 | MVP |
| 内部协作状态 | WanWork Envelope + event store | MVP，核心 |
| Coding editor ↔ Agent | Agent Client Protocol v1 adapter | Coding 场景 |
| 旧 BeeAI ACP | 只做迁移，不新增 | 有存量时 |
| 能力目录 | Agent Card；可映射 OASF 1.1 | Phase 2/3 |
| Agent 网络 transport | HTTP/SSE/queue；保留 SLIM port | Phase 3/4 |
| DID/联邦/E2EE 群组 | 跟踪 ANP 1.1/1.2 draft | 跨组织需求出现后 |
| 通用事件外壳 | CloudEvents 思路/绑定 | 内部总线规模化时 |
| Trace | W3C Trace Context + OTel | MVP |


## 12. 一手来源（核验于 2026-08-19）
- A2A 官方规范：[https://a2a-protocol.org/latest/specification/](https://a2a-protocol.org/latest/specification/)；仓库：[https://github.com/a2aproject/A2A](https://github.com/a2aproject/A2A)
- MCP 规范与 schema：[https://modelcontextprotocol.io/specification/](https://modelcontextprotocol.io/specification/)；仓库：[https://github.com/modelcontextprotocol/modelcontextprotocol](https://github.com/modelcontextprotocol/modelcontextprotocol)
- 旧 Agent Communication Protocol：[https://github.com/i-am-bee/acp](https://github.com/i-am-bee/acp)
- Agent Client Protocol：[https://agentclientprotocol.com/](https://agentclientprotocol.com/)；仓库：[https://github.com/agentclientprotocol/agent-client-protocol](https://github.com/agentclientprotocol/agent-client-protocol)
- ANP：[https://github.com/agent-network-protocol/AgentNetworkProtocol](https://github.com/agent-network-protocol/AgentNetworkProtocol)
- AGNTCY OASF：[https://github.com/agntcy/oasf](https://github.com/agntcy/oasf)
- AGNTCY Directory：[https://github.com/agntcy/dir](https://github.com/agntcy/dir)
- AGNTCY SLIM：[https://github.com/agntcy/slim](https://github.com/agntcy/slim)
- CloudEvents：[https://github.com/cloudevents/spec](https://github.com/cloudevents/spec)
这里的版本状态是截至核验日的快照；协议仍在快速演进，上线前必须重新跑 adapter contract suite。

---

来源：https://app.notion.com/p/3c1ead4b996e81c49a7ac20751c89cc7?pvs=204

