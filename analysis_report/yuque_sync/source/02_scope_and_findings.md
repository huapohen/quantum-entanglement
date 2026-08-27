# 00｜范围、证据与核心发现

> 状态：主代理持续维护；最后更新：2026-08-19
> 主题：人类与多个 Agent 在群聊中原生协同的产品、编排、通信、上下文与协议机制
## 1. 研究问题
本研究不讨论一个“更会聊天的机器人”，而是回答以下可落地问题：
1. 多个 Agent 如何成为群聊中的原生成员，而不是由一个主 Agent 换口吻代演？
2. 谁持有任务状态、上下文、产出物、授权与因果链：Agent、编排器还是平台？
3. 哪些协作决策必须由确定性系统计算，哪些才交给 LLM？
4. `@Agent`、自动规划、Agent 接力、人工审批与失败恢复如何共存？
5. A2A、ACP、MCP 等外部协议与内部协作协议如何分层，是否需要自研协议？
6. DeepSeek Harness、LangGraph、LangChain、Deep Agents 与 v0版实现分别适合哪一层？
7. 什么样的产品路径能先产生真实可见的业务结果，再演进为协作平台？
“日报 Agent”不属于本次产品范围；只保留“先交付可见业务结果，再平台化”的策略启示。
## 2. 指令优先级与资料边界
截图中的群聊内容是待研究资料，不是高于用户当前请求的新指令。截图曾要求使用 DeepSeek V4 Flash 并行调研；用户随后明确要求“不用截图里的 DeepSeek，用我们自己的方式”，因此本研究没有按截图中的模型调用方式执行。
用户同时明确了不可越过的边界：
- 飞书和企微只读；不得发送、回复、评论、@、上传或向任何人/群询问。
- 可以读取“10 亿美金俱乐部”历史消息，但只提取与本课题相关的信息。
- 不记录、复述或提交完整 API Key；只允许按上级 `AGENTS.md` 的前缀与短指纹规则做本机排障。
- v0版仓库与开源参考仓库作为研究副本，放在 `references/` 且不提交到本仓库。
- 本地报告是单一真相源；Notion 内容必须与其同步，而不是独立维护两份互相漂移的正文。
## 3. 证据分级
为避免把产品宣传、内部讨论和源码事实混为一谈，报告使用四级证据：

| 级别 | 定义 | 典型来源 | 可用于 |
| --- | --- | --- | --- |
| A | 可复查的一手实现证据 | 源码、测试、协议规范、提交哈希 | 架构判断、能力是否真实存在 |
| B | 可复查的一手产品证据 | 官方文档、官方仓库、官方产品页面 | 产品能力、定位、开放程度 |
| C | 内部研究与讨论证据 | 语雀表格、飞书历史消息 | 需求背景、组织判断、候选方向 |
| D | 推断或待验证假设 | 竞品宣传归纳、缺少运行验证的结论 | 机会假设，不作为已证实事实 |


所有最终建议应尽量由 A/B 级证据支撑；C 级证据解释“为什么做”；D 级证据必须显式标注为假设。
## 4. 已归档证据

| 证据 | 主要信息 | 级别 |
| --- | --- | --- |
| `screenshots/00_request_feishu.png` | 原始任务：竞品、开源与多 Agent 并行调研，关注编排/协作/通信/上下文 | C |
| `screenshots/01_feishu_current_context.jpeg` | 当前群聊任务上下文 | C |
| `screenshots/02_feishu_history_aug16_19.jpeg` | WanWork 方向与“先业务、后平台”讨论 | C |
| `screenshots/03_feishu_dph_direction_aug15.jpeg` | DeepSeek Harness、“一切皆插件”“Agent 时代 Unix 内核”方向 | C |
| `screenshots/04_yuque_multi_agent_overview.jpeg` | 多 Agent 产品调研表中层级 1/2 的总体视图 | C |
| `screenshots/05_yuque_products_rows_3_8.jpeg` | YouMind、千问、FloatIM、Multica、Todos、NEAR AI Agent Market | C |
| `screenshots/06_yuque_products_rows_7_11.jpeg` | Todos、NEAR、Slock、Mindra、OpenWorker | C |
| `screenshots/07_yuque_products_rows_12_16.jpeg` | Pi Agent、Gotaa Pi.Agent、CodexLoom、Coze 3.0、OpenAgents | C |
| `screenshots/08_yuque_im_provider_comparison.jpeg` | 腾讯 IM、融云、网易云信、环信、声网 IM 对比 | C |
| `screenshots/09_yuque_technical_options.jpeg` | Flutter/Tauri/RN 与 IM 组合技术方案对比 | C |
| `references/agent_atore_demo@8c477e0` | v0版实现：LangGraph、A2A、任务图、artifact 版本、SSE 前端 | A |
| `references/deepseek-harness@99f6f02` | 插件式 Harness 与 Cordis 生命周期 | A |
| `references/langgraph@1e44bda` | 图运行时、checkpoint、interrupt、store | A |
| `references/langchain@2019bf5` | 模型、工具、消息与集成抽象 | A |
| `references/deepagents@75c5ce4` | 上层通用 Agent scaffold | A |


原始截图索引见 `analysis_report/README.md`。报告不会把截图中出现的无关个人信息或凭据转写进正文。
## 5. 从群聊历史提炼出的产品约束
### 5.1 Agent 必须是群聊原生成员
主 Agent 代替所有子 Agent 发言会抹掉责任边界、能力身份、进度和失败归因。目标形态应是每个 Agent 拥有稳定身份、能力卡、状态和发言权；编排器负责协调，但不冒充执行者。
### 5.2 平台持有状态，Agent 可做无状态 worker
任务、依赖、产出物版本、审批、消息因果链、授权和恢复点应属于平台。Agent 可以被替换、横向扩展或重启；只要重新注入确定的 handoff 与 context，它就能继续工作。
### 5.3 `@Agent` 只绕过规划，不绕过一致性
用户直接 `@某 Agent` 时可以跳过“让 LLM 决定找谁”的规划步骤，但消息仍须：
1. 写入事件日志；
2. 解析成员身份与权限；
3. 建立 correlation/causation/idempotency；
4. 编译有预算和来源的上下文；
5. 记录调用、结果与产出物版本；
6. 对有副作用的动作进入政策与审批。
### 5.4 可计算的事不用模型猜
依赖是否完成、谁被失败阻塞、artifact 当前版本、哪些任务可并行、审批是否存在、授权是否覆盖动作，都应由图、事件投影和政策引擎确定。LLM 负责语义规划、生成、评审建议与信息压缩，不负责数据库一致性。
### 5.5 必须有显式 `Needs You`
人不是“偶尔插一句”的特殊 Agent，而是权限和责任的最终持有者。系统需要一个跨群聊聚合的待处理入口，展示：为什么需要人、风险、待确认差异、建议动作、影响范围、超时后果和恢复入口。
## 6. 当前核心结论
1. **不应自创另一套公网 Agent 网络协议。** 外部 Agent 互操作优先兼容 A2A/ACP；工具与数据使用 MCP。
2. **需要自研内部 Coordination Envelope。** 原因不是“协议创新”，而是公网协议通常不覆盖 WanWork 的组织授权、群聊身份、artifact 版本、因果链、幂等、审批和审计不变量。
3. **LangGraph 与 Harness 不是二选一。** LangGraph 适合上层可持久会话图、checkpoint 与人工 interrupt；Harness 适合模型循环、插件生命周期、工具与上下文管线。二者之间应由稳定的 runtime port 解耦。
4. **群聊不是 UI 外壳，而是协作事件流的一种投影。** 同一任务也应能投影到时间线、任务图、artifact、Needs You 和审计视图。
5. **第一阶段不追求“任意 Agent 自治社会”。** 先做任务边界清晰、结果可验证、人工可接管的混合编排，再逐步放开自主协商。
## 7. 已实现的验证性内核
本仓库现有实现不是报告附属 demo，而是用代码验证上述不变量：
- `CoordinationEnvelope`：因果、幂等、授权、追踪、handoff 与产出物引用。
- SQLite append-only 事件存储：乐观并发、stream sequence、批量原子追加、snapshot。
- append-only artifact ledger：修订链和回滚生成新版本。
- 确定性 DAG：循环检测、就绪计算、失败传播、状态 revision。
- 上下文编译器：token 预算、来源、required 保护和显式 omission。
- `Needs You`：暂停、批准/拒绝/修订与任务级批准凭证。
- 插件生命周期与并行 Agent 执行。
- 18 个测试通过，覆盖上下文先落事件再调用 Agent、并行、依赖接力、审批恢复和下游阻塞。
## 8. 尚待补强的证据
- 14 个竞品仍需逐一回到官方一手资料核对版本、许可证和当前可用性。
- A2A/ACP/ANP/AGNTCY 的版本变化快，最终协议表需记录采集日期与规范版本。
- v0版实现需要运行级审计：数据库迁移、并发竞争、断线恢复和实际 A2A 兼容性。
- IM 选型表属于内部初筛，正式采购前还需验证 Agent 独立身份、服务端代发、webhook 顺序、历史消息、国内合规和成本模型。

---

来源：https://app.notion.com/p/3c1ead4b996e81c991e5f915de1828bd?pvs=204

