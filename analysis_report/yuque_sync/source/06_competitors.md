# 04｜多 Agent 竞品全景

> **同步状态**：2026-08-20 已从本地单一真相源 [`analysis_report/research/04_competitor_landscape.md`](https://github.com/huapohen/quantum-entanglement/blob/main/analysis_report/research/04_competitor_landscape.md) 全量更新。竞品事实已按官方资料、固定源码和许可证核验修订。

| 维度 | 要回答的问题 |
| --- | --- |
| 协作层级 | 单 Agent、对话关系、组织关系，还是跨网络互操作？ |
| Agent 身份 | 独立成员、后台 worker，还是主 Agent 的角色扮演？ |
| 编排 | 线性链、LLM 自主、确定性 DAG、任务市场还是组织岗位？ |
| 通信 | 群聊、私聊、任务事件、A2A、共享文件或黑板？ |
| 上下文 | 全量会话、检索、artifact 引用、预算压缩、岗位知识？ |
| 状态 | 谁持有任务/进度/重试/失败/版本？能否恢复？ |
| 人机边界 | 是否支持审批、暂停、修订、接管和责任可见？ |
| 开放性 | 源码、协议、插件、外部 Agent、自部署分别开放到什么程度？ |
| 企业性 | 权限、审计、合规、数据域、SLA、身份和成本是否可治理？ |


## 3. 竞品总表

| 产品 | 层级 | 核心形态 | 最值得借鉴 | 主要缺口/风险 | 对 WanWork 的启示 |
| --- | --- | --- | --- | --- | --- |
| YouMind | 1 | AI 创作工作台 | 从资料收集到多格式成品的 artifact 工作流 | 更像单人创作空间，缺少 Agent 间责任与依赖 | 把“最终可见成果”作为协作主对象 |
| 千问 | 1 | 企业模型与应用平台 | 丰富模型/API/应用供给，统一企业服务入口 | 模型平台不等于协作平台；状态和交接偏弱 | 模型必须可替换，协作状态不能绑在模型供应商 |
| FloatIM | 1 | Agent 原生群聊 | First-Class Agent、本地 runtime、自组织团队是官网宣称 | IACT/Selfware 的 MIT 声明尚无公开规范仓库/LICENSE；产品开源与任务治理未知 | 独立身份和原生发言是必选项，但协议依赖必须有机器可读规范 |
| Multica | 1 | Coding Agent 项目管理 | task/claim/run、lease/retry、并发 CLI、worktree 与实时投影有源码证据 | 自定义 Multica License 限制第三方托管、商业嵌入和品牌移除；跨业务 artifact 有限 | 借鉴运行状态和隔离工作区，不复制许可证或把 branch 当通用 artifact |
| Todos | 1 | 自然语言任务分解与执行 | Chief/Agent、版本化 plan/diff、分支并行、AI/human review、Machine/MCP 有官方文档 | 精确 DAG 依赖语义仍待实测；未见产品源码许可证，Terms 保留软件权利 | “对话→可审阅计划→可见执行”比连续聊天重要 |
| NEAR AI Agent Market | 2 | Agent 任务市场 | 悬赏、执行、验证、结算/争议形成任务闭环 | Web3/TEE/市场机制较重，不适合直接做内部协同默认层 | 可借鉴可验证交付和能力发现，不照搬经济层 |
| Raft（原 Slock） | 2 | 人 + Agent chat workspace | 长期身份/记忆、long-running Agent、本地 daemon 与人类 steer 有官方文档 | 公开仓库主要是文档/外部 Agent，完整产品核心和治理实现未知 | 群聊之外还需要稳定身份、职责和本地 runtime |
| Mindra | 2 | 企业多 Agent 云平台 | 3,000+ 集成、RBAC/SSO、审计、HITL、SOC 2/GDPR 为官方声明 | 尚无源码或独立审计报告正文验证；可能偏传统自动化 | 工具集成、组织权限重要，但必须区分厂商声明与实现证据 |
| OpenWorker | 2 | 本地优先任务执行 Agent | MIT 源码可核到 risk class、Inbox 幂等 resolve、audit redaction、MCP approval metadata | action 参数变化是否重批、进程隔离和托管边界仍需实测 | 借鉴本地执行、action-level 审批和工具风险分级 |
| Pi | 2 | 极小开源 Agent runtime/toolkit | 刻意保持小核心，通过 extension/package 组装能力 | 核心明确不内置 MCP、sub-agent、permission popup、plan mode、todo；默认继承启动者权限 | 学习可扩展极小核心，但 sandbox 和平台授权必须外置 |
| Gotaa Pi.Agent（历史） | 2 | 历史材料中的岗位 Agent 平台 | 语雀表曾记录 SOP、知识库、岗位和审批命题 | 子域失效、根域停放，当前产品状态/功能均不可验证 | 只保留为产品假设，不把旧截图写成当前事实 |
| CodexLoom | 2 | 多 Codex thread 编排 | 稳定 Agent ID、Profile/Topic/Message/Artifact/Needs You 对象有源码 | Elastic License 2.0，source-available 非 OSI 开源；组织声明不自动产生权限 | 研究 thread 编排和对象边界，但权限/恢复必须由平台强制 |
| Coze / Coze Studio | 3.5 | 托管办公产品 + 开源 Agent 开发平台 | 学习托管办公 UX；Studio 的 Agent/workflow/app 核心 Apache-2.0 | 未验证所谓“Agent Team”当前官方边界；托管能力不能归给开源 Studio | 拆开云产品体验与可自部署引擎评价，避免能力偷换 |
| OpenAgents | 3.5 | Workspace + Launcher + Network SDK | 多 runtime 汇聚、共享 thread/file/browser、跨机器 daemon、A2A adapter 有源码 | action approval、结构化 handoff、版本 artifact 和企业治理仍需上层 | 外部 runtime/网络互操作做适配层，不做内部唯一状态源 |


## 4. 分产品分析
### 4.1 YouMind：结果空间优先
YouMind 的价值不在“有一个 AI 对话框”，而在从资料采集、理解、组织到文章/视频/幻灯等成品的连续工作区。它提示我们：用户最终购买的是产出物和完成感，而不是 Agent 调用次数。
可借鉴：
- 每个协作任务应有 artifact 首页，而不是只留下聊天记录。
- 模板应描述输入、结构、验收标准和输出格式，不只是 prompt。
- 同一来源可以被多个 Agent 消费，但引用和版本必须稳定。
不足：群聊成员关系、任务依赖、Agent 责任归属和企业审批不是其核心。WanWork 需要把结果工作台与多 Agent 协作状态合并。
### 4.2 千问：模型供给不是护城河
千问代表模型/API/企业应用供给层。其模型和服务规模说明接入供应商的重要性，也反向说明 WanWork 的核心不能是某个模型列表。
设计含义：
- 模型通过适配器和策略选择，不能渗透到任务状态模型。
- Agent 能力卡声明需求（上下文、工具、延迟、成本），调度器选择实现。
- 同一任务应允许模型故障切换，且不丢失平台持有的上下文与进度。
### 4.3 FloatIM：Agent 必须像群成员一样存在
FloatIM 官网把 Agent 描述为 First-Class Agent，并声明本地运行、自组织团队及
IACT/Selfware 的 MIT 方向；本轮没有找到可下载协议规范、仓库或 LICENSE 文件。因此
“Agent 原生群聊”可写为官方产品命题，但不能进一步写成产品开源或协议已可互操作。
必须进一步超过它的地方：
- 独立发言之外，要有任务、handoff、artifact、审批和审计。
- 群消息必须与内部事件一一关联，不能只靠聊天平台 history 恢复。
- Agent 身份、版本、能力和权限随组织治理，而不是只有头像与名称。
### 4.4 Multica：多 Coding Agent 的工程化经验
Multica 关注 Claude Code/Codex/Cursor 等 Coding Agent 的项目管理、task claim/run、
并发与团队协作。monorepo 可核到 task lifecycle、lease/retry migration、失败分类、
orphan recovery 与实时投影，工程执行状态证据较强。它是源码公开且可自托管，但自定义
Multica License 不是 OSI 常见开源许可证，限制第三方托管、商业嵌入和品牌移除。
可借鉴：
- 工作区隔离和并行执行；
- 队列、优先级、阻塞原因和团队吞吐可见；
- 不要求所有 Agent 使用同一模型/同一 harness。
不可直接照搬：代码仓库的 branch/worktree 是一种 artifact 协作，不能覆盖文档、表格、审批、客户资料等通用办公产物。
### 4.5 Todos：把自然语言变成任务图
Todos 官方文档可确认 Chief/Agent、版本化 plan/diff、独立分支、AI/human review、
Machine、MCP、权限和调度。截图中的“精确 DAG 前序触发后序”仍需实测，不应仅据二手表
升级为实现事实；其 Terms 也没有给出产品源码开源许可。
WanWork 应采用同类确定性调度，但增加：
- 图版本与重规划差异；
- 每条边的输入/输出契约；
- 失败传播和人工恢复；
- artifact 与任务状态的原子提交；
- 不同风险动作的授权和审批。
### 4.6 NEAR AI Agent Market：验证与交易闭环
NEAR 的差异是将 Agent 能力变成可发现、可悬赏、可执行、可验证、可争议的市场任务。它说明开放 Agent 网络不能只有“能互相发 JSON”，还要解决信任和交付。
适合借鉴：Agent Card/能力发现、验收证据、可验证输出、争议/复核状态。暂不适合照搬：代币经济、TEE、托管与企业群聊 MVP 的复杂度不匹配。
### 4.7 Raft（原 Slock）：长期 Agent 与 chat workspace
旧 `slock.ai` 已重定向到 `raft.build`。Raft 当前公开页面/文档强调 chat workspace、
long-running Agent、持久身份/记忆、本地 daemon 和人类 steer；公开 GitHub 主要覆盖文档
与 external-agent 边界，不能据此声称完整产品核心已开源。
WanWork 应把岗位拆成四种可治理对象：角色（为什么存在）、能力（能做什么）、授权（允许做什么）、责任（对什么结果负责）。不能用一个 prompt 同时表达四者。
### 4.8 Mindra：企业集成与治理
Mindra 官网与 Security 页面明确声明 3,000+ 集成、RBAC/SSO、审计、HITL、policy、
SOC 2 Type II、GDPR 和 ZDR。它们已不是无来源二手传闻，但仍是厂商声明；本轮没有源码、
独立审计报告正文或运行证据把它们升级为可验证实现。
风险在于把 Agent 降格为自动化节点：如果所有流程都像 n8n/Zapier，语义协作和动态 handoff 会受限；如果全部交给 LLM，又缺少确定性和合规。目标应是“确定性骨架 + 语义节点”。
### 4.9 OpenWorker：本地执行与批准模式
OpenWorker 的 MIT 仓库可定位 risk class、Inbox 状态与幂等 resolve、SQLite audit
redaction、MCP schema/approval metadata；因此审批不只是一句宣传。仍需实测批准后参数
变化是否强制重批、host 权限隔离以及托管服务边界。
WanWork 应将工具调用分为读取、草稿、内部变更、外部发送、不可逆动作等风险等级；批准的是具体 action intent，而不是模糊的“相信这个 Agent”。
### 4.10 Pi：刻意极小的 runtime 积木
旧表把 Pi 写成内置 MCP、sub-agent、permission popup、plan mode 和 todo，事实恰好相反：
官方 usage/design principles 明确把这些列为非内置能力，需通过 extension、package 或外部
工具补充。Pi 的真实价值是小核心、可替换 package 和 extension surface。
适合通过 runtime port 接入，不应让 extension 内部 todo 或会话状态成为平台真相源。
Pi 默认继承启动者的 filesystem/process/network/credential 权限，生产接入必须由外部
sandbox 与 WanWork action-time policy 收口。
### 4.11 Gotaa Pi.Agent：仅保留历史假设
语雀历史表曾把 Gotaa Pi.Agent 描述为企业知识库、SOP、招聘/财务/行政岗位 Agent 与
审批平台；当前 `pi.gottaa.com` 无法解析，根域为 parking lander。本轮无法验证产品当前
存在性或任何功能，以上只能作为历史产品假设，不能继续使用现在时。
产品上应提供岗位模板，但底层仍分离知识范围、工具授权、运行政策和可观测指标，以便审计和复用。
### 4.12 CodexLoom：线程团队化及其风险
CodexLoom 把多个 Codex CLI/thread 组织成长期 Agent，并显式建模 Profile、Topic、
Message、Artifact、Organization/Collaboration 与 Needs You；相关对象可在源码中定位。
许可证为 Elastic License 2.0，应称 source-available 而非 OSI 开源。组织/协作声明是数据
结构，不自动授予权限，也不证明消息强制路由。
截图同时标注了项目成熟度和代码质量风险。最重要的教训是：
- thread 不是稳定业务身份；
- 终端输出不是结构化事件；
- 共享目录不等于 artifact 事务；
- Owner prompt 不等于组织治理；
- 多进程并发必须有幂等、锁、冲突检测和可恢复任务状态。
### 4.13 Coze 托管产品与 Coze Studio：必须拆开评价
当前可验证的是：扣子托管入口以 AI 办公/Space 呈现；`coze-dev/coze-studio` 的 Agent、
workflow、app 与自部署核心以 Apache-2.0 公开。本轮未找到“Coze 3.0 Agent Team”当前
官方页面或等价开源实现，因此不能把历史营销名、托管体验和 Studio 能力合并成一个事实。
WanWork 的差异化不应是更多预置 Agent，而应是：开放 runtime、可验证状态、企业授权、群聊原生身份和多协议互操作。
### 4.14 OpenAgents：外部 Agent 网络
OpenAgents 当前不只是抽象网络：公开仓库已有 Workspace、Launcher、Network SDK、统一
workspace 地址、跨机器 daemon、共享 thread/file/browser 和 A2A transport/registry。
这些是可验证源码边界，但 action-level approval、结构化 task/handoff、版本化 Artifact、
组织授权与恢复语义仍需 WanWork 上层提供。
WanWork 对外应像网络节点，对内应像协作操作系统：外部消息进入后必须转换为内部 Envelope 并落事件；内部授权和 artifact 不应完全暴露或委托给外部网络。
## 5. 跨竞品机制比较
### 5.1 编排
- 单 Agent 自由规划易展示，但难审计、难恢复。
- 固定工作流可靠，但面对模糊目标太僵硬。
- 最优组合是 LLM 生成/修订计划，确定性 DAG 校验、执行和失败传播。
- 对高不确定任务，可在图中嵌套自治 loop；自治 loop 的输入、预算、出口和权限仍必须明确。
### 5.2 通信
- 群聊适合意图、解释、协调和人工介入。
- 任务事件适合进度、状态与机器消费。
- artifact 适合正式结果和跨 Agent 交接。
- A2A/ACP 适合远程 Agent 互操作。
- MCP 适合工具/数据访问。
把所有信息都塞进聊天消息，会导致状态不可计算、上下文无限增长、失败难恢复。正确做法是多种投影共享同一个事件与对象模型。
### 5.3 上下文
竞品常见两极：全量聊天历史，或完全独立的 Agent 记忆。WanWork 应建立可追踪上下文编译：目标、handoff、政策、依赖 artifact 为 required；相关决策、记忆、聊天和工具摘要按预算选择；所有 omission 明示并可追溯来源。
### 5.4 人类控制
“信任模式/检查模式”的二元开关过于粗糙。更合理的是 action intent 级政策：风险、数据类别、目标系统、外部副作用、可逆性、授权来源共同决定允许、拒绝或 Needs You。
## 6. 市场空白与建议定位
建议定位不是“AI 群聊”或“企业 Agent 商店”，而是：
> **面向真实业务产出的人与 Agent 协作操作系统：每个 Agent 是可治理的群成员，每项工作是可恢复的任务图，每个结果是可追溯的版本化 artifact。**
首批差异化能力：
1. Agent 原生群成员和 `@Agent` 直达；
2. 群聊、任务图、artifact、Needs You 四视图同源；
3. 清晰可见的 Agent 接力和上下文来源；
4. 跨模型、跨 harness、跨协议，不绑定单一供应商；
5. 默认可审计、可暂停、可恢复、可回滚；
6. 从一个高价值业务场景切入，而不是先做空平台。
## 7. 已完成核验与下一轮实测
14 项官方入口、公开仓库、许可证与关键宣传/实现差异已完成首轮核验，详见
`06_competitor_source_validation.md` 的 65 条来源记录（63 个唯一外部 URL + 2 张内部
语雀截图）。下一轮不再重复首页浏览，而应产生运行证据：
- 隔离启动 Multica，验证 claim/start/heartbeat/fail/retry/orphan recovery，并先完成
	自定义许可证的法律边界确认；
- 运行 OpenWorker approval/inbox/audit 测试，验证 action 参数变化后的重新审批；
- 自托管 OpenAgents Workspace，接入两个 runtime，测试断线恢复、共享文件冲突和 Stop；
- 启动 Coze Studio，逐项比较托管办公产品与开源引擎的能力差集；
- 实走 Todos 的 goal → plan review → parallel branch → AI/human review → merge；
- 对 Raft/FloatIM 只做只读产品旅程，观察群消息与 task/artifact 是否结构化关联；
- Gotaa 仅在出现新的官方域名、公告或仓库后重新研究，不围绕停放域名猜测。
## Notion 证据入口
- [06｜竞品官方信源、许可证与实现核验](https://app.notion.com/p/3c1ead4b996e81f9b5eddebebc96d30a)
- [证据库｜飞书与语雀调研截图（只读）](https://app.notion.com/p/3c1ead4b996e8101997fecd0302714ba)

---

来源：https://app.notion.com/p/3c1ead4b996e811fa520ed44582eeb1b?pvs=204

