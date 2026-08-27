# DeepSeek Harness、LangGraph、LangChain 与 Deep Agents 源码级架构审计

> 研究日期：2026-08-19。本文只把固定版本的源码与仓库文档作为事实依据；“建议”“判断”均是面向本项目的架构推论，不等同于上游承诺。

## 0. 证据基线

| 项目 | 固定 commit | 仓库版本 | 本文简称 |
|---|---|---|---|
| DeepSeek Harness | `99f6f02fecdb7dff40c3fbc9470f5907c29f74ca` | `dsh-v0.1.0-rc.7` | DSH |
| LangGraph | `1e44bda48ff4982b8ccfeec9c14156ea9e8ae5a2` | `1.2.11` | LG |
| LangChain | `2019bf5ebe50324c548f67c2666a804343f9b772` | `1.3.15`；`langchain-core 1.5.6` | LC |
| Deep Agents | `75c5ce47bf5fb146c6a8436c00c60134dceee8a5` | `0.7.7` | DA |

源码引用均相对于 `references/<repo>/`。版本号来自相应 `package.json`/`pyproject.toml`；LC 1.3.15 明确依赖 `langgraph>=1.2.11,<1.3.0`，DA 0.7.7 明确依赖 `langchain>=1.3.14,<2.0.0` 与 `langchain-core>=1.5.0,<2.0.0`。这意味着 DA 不是独立运行内核，而是 LC/LG 之上的装配层。

## 1. 先给结论

产品不应四选一。建议以 **DSH/Cordis 作为持续对话与 Agent 运行内核**，以 **LangGraph 作为可选的持久工作流执行器**，借用 **LangChain middleware 的 Agent 产品扩展接口经验**，把 **Deep Agents 当作产品模板、工具说明和文件工作区的参考实现**。

两者必须保留不同的事实边界：DSH Session log 是用户对话、模型所见上下文、工具调用和 UI 回放的事实源；LangGraph checkpoint 是某个 workflow run 的 channel 状态、节点调度与恢复事实源。桥接层只交换显式命令和事件，不互相镜像内部状态。

## 2. DeepSeek Harness：适合作为产品运行内核

### 2.1 “Everything is a plugin”不是口号，而是生命周期模型

`docs/architecture.md` 明确把 model adapter、tool registry、session log 和 agent loop 都定义为插件；`vendor/cordis/src/registry.ts` 的 `Registry.plugin()` 为每次挂载创建 `Fiber`，依赖未就绪时保持等待，服务变化后可重新装载。`vendor/cordis/src/fiber.ts` 的 `Fiber.effect()` 收集 disposer，卸载时反向释放；`Fiber._unload()` 等待异步清理。这带来四个可直接复用的性质：

1. 能力以 Service Definition / Provider / Consumer 三角色解耦，替换 provider 不要求修改消费者。
2. 监听器、服务注册、工具注册都归属具体 plugin fiber，热卸载可撤销，不留下全局残留。
3. plugin 依赖服务的可用性，不把启动顺序散落为手写初始化流程。
4. agent loop 自身可替换；新行为优先挂在事件 seam，而不是继续膨胀循环条件分支。

这比 LC 的 middleware 更底层：LC middleware 在 `create_agent()` 时被静态编译进一张 StateGraph；Cordis plugin 是运行期依赖与资源所有权单元，生命周期和服务热替换是核心语义。

### 2.2 Session log 是模型上下文与 UI 回放的共同事实源

`docs/architecture.md` 明确写出 **Model-visible means logged**。默认 agent loop 不是先拼一个临时 prompt 再调用模型，而是先追加 `turn/start`、`step/start`、`user/message` 等耐久事件，再从 session log 派生模型 history。流式输出保留为 `assistant/chunk`，完成后再形成 `assistant/message`；工具也以 `tool/call`、`tool/result` 成对落入日志。

源码入口包括：

- `packages/core/session/src/types.ts`：SessionEvent 类型面；
- `packages/core/session/src/index.ts`：session service；
- `packages/core/session/src/preparation.ts`：派生与准备；
- `packages/core/agent-loop/src/agent.ts`：turn/step 驱动；
- `packages/core/agent-loop/src/invariant.ts`：运行时不变量；
- `packages/session/session-persistence-*`：SQLite、JSONL 与持久化协调。

这个设计对 WanWork 的直接价值是：模型看到的消息、用户看到的流式轨迹、调试所需的工具历史可以从同一日志投影；上下文不再是不可重建的临时 Python/JS 数组。

但 WanWork 不能把 DSH SessionEvent 直接当成全部业务事件。DSH 事件描述单个 Agent session 的 turn/step/工具轨迹；群聊成员、WorkflowPlan、TaskAttempt、Approval、ArtifactVersion、组织授权属于跨 Agent 的领域事实。正确方式是两个事件域通过明确的 `agent.invocation.started/completed` 和 context digest 关联，而不是复制彼此所有事件。

### 2.3 Turn/step 生命周期适合单 Agent run，不等同于多 Agent 调度器

`docs/agent-lifecycle.md` 展示了严格的运行顺序：inbox claim → `agent/pre-step` → 持久 step → prompt/tool schema 组装 → `agent/request` → `llm/stream` → tool pipeline → `step/end` → 是否继续下一步 → `turn/end`。

其中三个机制值得直接借鉴：

1. **输入只有一个 inbox**：steering、follow-up 和 injected context 有明确的 claim 时机，避免在模型请求中途隐式修改上下文。
2. **waterfall hook**：`agent/pre-step`、`agent/request`、`llm/stream`、tools pre/execute/post 有清晰的委托顺序；监听器必须调用 `next()` 才继续。
3. **失败也形成耐久边界**：被拒绝或未花费 step 的 turn 仍有开始/结束事实，便于解释“系统为什么没做”。

WanWork 可以把一个 TaskAttempt 映射成一个 DSH turn 或 session，但任务 DAG 的 ready set、依赖失败、并行限制和跨 Agent handoff 不能交给该 loop 自由推断。这是自研 scheduler 与 DSH runtime 的边界。

### 2.4 工具、沙箱与批准是 capability seam

DSH 将 filesystem、subprocess、shell、sandbox、tools、approval 拆成独立 capability。`docs/architecture.md` 把 seam 定义为 Service Definition、Provider、Consumer 三角色；只实现一个工具函数不算完整 seam。

相关源码：

- `packages/core/tools/src/index.ts` 与 `types.ts`：工具注册和执行面；
- `packages/interaction/user-approval/src/index.ts`：用户批准服务；
- `packages/sandbox/*`、`packages/fs/*`、`packages/shell/*`：执行世界与政策；
- `packages/mcp/mcp-client/src/tools.ts`：MCP 工具接入。

这个抽象比“每个 Agent 挂一个 tools 列表”更适合企业产品，因为同一模型工具可切换本机、容器、远端 sandbox 或只读 provider。WanWork 应复用 seam 思路，但批准决策必须由平台 action intent 和组织 policy 主导，DSH approval 作为单 run 的交互实现。

### 2.5 Compaction 不是覆盖历史，而是生成替代表面

`packages/compaction/compaction-basic` 在 `agent/pre-step` 压力点和 canonical context-overflow 错误上触发；`agent-lifecycle.md` 说明只有 pruning 或 summarization 推进了 surface replacement generation 才打开新 retry turn。原始 session events 仍保留。

对 WanWork 的启示：

- 摘要是一个有版本、来源和覆盖区间的派生 artifact，不是删除历史；
- tool result pruning 与聊天摘要应分开，因为前者可重新读取，后者可能包含决策；
- context overflow 后重试必须有“上下文确实变化”的证明，否则会形成无限重试；
- 下游 Agent 应优先拿 handoff + artifact，而不是继承上游完整 session compaction。

### 2.6 DSH 的采用建议与风险

建议复用或对齐：插件树、Fiber effect 生命周期、capability seam、耐久 session event、turn/step 事件、工具执行管线、approval/sandbox 接口、compaction generation 和 invariant companion。

不建议直接复用为平台真相源：群聊 membership、组织 RBAC、DAG、任务尝试、artifact 业务版本、跨 Agent 因果、租户配额和 IM 投递。

风险：上游 README 明确处于 developer preview 且会有 breaking changes；当前固定版本还是 `0.1.0-rc.7`。因此应通过 `AgentRuntimePort` 隔离，先做薄适配器和契约测试，不 fork 大量上游代码。

## 3. LangGraph：适合 durable control flow

### 3.1 StateGraph 与 Pregel 的真实执行语义

`libs/langgraph/langgraph/graph/state.py:StateGraph` 是 builder；节点签名是 `State -> Partial<State>`，state key 可以用 reducer 合并并行更新，编译后才得到可 `invoke/stream/ainvoke` 的 graph。

底层 `libs/langgraph/langgraph/pregel/main.py:Pregel` 使用 Bulk Synchronous Parallel：

1. Plan：从 channel 更新计算本 superstep 要运行的 actor；
2. Execution：并行执行被选 actor，本 step 内写入对其他 actor 不可见；
3. Update：统一更新 channel；
4. 重复到无 actor 或达到 recursion/step 限制。

这比自己写“遍历节点 + asyncio.gather”更成熟，尤其适合复杂条件分支、子图、重试、stream 与 time-travel。但它的 state channel 仍是工作流运行状态，不天然等于 Task、Artifact、Approval 等领域记录。

### 3.2 Checkpoint、store 与业务事件不可混同

LangGraph checkpointer 保存每个 thread/checkpoint namespace 的 channel values、pending writes、metadata 与版本；仓库同时提供 memory、SQLite/第三方和 Postgres 实现。store 则提供跨 thread 的长期键值/搜索能力。

建议职责：

- checkpoint：恢复到某个节点/step，支持 interrupt、time travel 和 subgraph；
- WanWork event store：保存“计划已创建、任务已批准、artifact v2 已提交”等不可反转业务事实；
- artifact store：保存正式内容与版本；
- memory/store：保存可检索偏好或知识，但不能替代授权与任务事实。

如果只把所有内容放在 graph state，图 schema 变更会演化困难，多个工作流也难共享同一个 artifact；如果只用事件存储又自己重造所有 checkpoint/superstep 机制，工程成本过高。因此两者应并存。

### 3.3 Interrupt 的关键陷阱

`libs/langgraph/langgraph/types.py:interrupt()` 明确说明：首次调用抛出 `GraphInterrupt`；恢复要用 `Command(resume=...)`；恢复时**节点从开头重新执行**；多个 interrupt 按调用顺序匹配；必须配置 checkpointer。

因此 interrupt 节点在 `interrupt()` 之前不能包含未做幂等保护的外部副作用。正确顺序是：

1. 计算待批准 action proposal；
2. 把 proposal/approval request 写入平台事件；
3. 调用 `interrupt()`；
4. resume 后读取批准凭证；
5. 由 outbox/幂等 action executor 执行副作用。

v0版实现把 interrupt 独占为单节点的方向是正确的；WanWork bridge 还必须把 LangGraph interrupt 映射成统一 Needs You，而不是为每张图重新发明 UI。

### 3.4 LangGraph 适合与不适合

适合：复杂长期流程、人工中断、checkpoint、条件边、子图、图级 stream、重试/time-travel 和需要可视化的业务 workflow。

不应承载：公网 Agent 传输、组织权限真相源、IM 历史、模型 provider 抽象、工具协议、artifact 内容存储。简单确定性 DAG 也不必全部编译成 LangGraph；本项目当前 scheduler 仅数百行，能够更直接地验证核心不变量。

## 4. LangChain：借 middleware 和统一模型/工具面，不采用为领域内核

当前代码位于 `libs/langchain_v1/langchain/`。`agents/factory.py:create_agent()` 最终装配一张 StateGraph；`agents/middleware/types.py:AgentMiddleware` 暴露 before/after agent、before/after model、wrap model call、wrap tool call 等钩子，并允许 middleware 扩展 state/context schema。

价值：

- 大量模型、消息、tool、structured output 接口；
- middleware 生态与清晰的 model/tool wrapper；
- 与 LangGraph checkpointer/store 直接集成；
- HumanInTheLoop、summarization、todo 等通用能力可快速验证。

限制：

- middleware 通常在 `create_agent()` 时静态编译进 graph，不具备 Cordis 那种运行期服务依赖与可逆资源所有权；
- 其 AgentState/message reducer 是单 Agent 应用语义；
- provider/tool 抽象变化快，企业运行时需要在外面再加稳定端口；
- 直接把 LC message state 当平台 memory 会把模型消息格式泄漏到业务层。

建议只在 adapter/runtime 内使用 LC 类型；领域包不 import 具体 ChatModel、ToolMessage 或 middleware state。

## 5. Deep Agents：很好的 product scaffold，但确实“太上层”

`libs/deepagents/deepagents/graph.py:create_deep_agent()` 直接调用 LangChain `create_agent()`，默认装配 filesystem、subagent、summarization、memory、skills、patch tool calls、human-in-the-loop 和 backend；返回 `CompiledStateGraph`。它显式依赖 LC/LG，因此不是独立内核。

### 5.1 值得复用的产品机制

- `DeepAgentState` 用 `DeltaChannel` 降低 checkpoint 从 O(N²) 增长到 O(N)；
- BackendProtocol 统一 State、Filesystem、Store、Composite 和 sandbox backend；
- FilesystemPermission 与 HITL middleware 结合；
- SubAgent 支持不同模型、工具、middleware、skills、permissions 和 structured response；
- AsyncSubAgent 支持远端 Agent Protocol server 的 launch/status/follow-up/cancel；
- summarization 能把长历史/大 tool result spill 到 backend；
- harness profile 将面向模型的行为提示、工具和 middleware 作为组合。

### 5.2 为什么不能直接作为 WanWork 内核

`middleware/subagents.py` 的默认语义明确写着：subagent invocation 是 stateless，子 Agent 只看到 task prompt，父 Agent 只看到最终 assistant message；默认提示还要求父 Agent 代为转述结果。这与本项目“Agent 是群聊原生成员、平台持有 handoff/artifact/进度”的目标冲突。

此外，Deep Agents 的 `task` tool 让主模型决定何时调用子 Agent，适合个人通用 Agent，却不够表达组织权限、确定性依赖、artifact transaction、审批链和独立 Agent 发言。可以借它的 backend、middleware、prompt/tool UX，但不要让 `task` tool 成为平台 scheduler。

## 6. 横向对比

| 维度 | DSH/Cordis | LangGraph | LangChain | Deep Agents | WanWork 应拥有 |
|---|---|---|---|---|---|
| 核心抽象 | plugin/service/effect + session loop | graph/channel/checkpoint | model/tool/message/middleware | 预装的通用深度 Agent | member/task/attempt/artifact/approval |
| 状态范围 | 单 Agent session 事件 | 单 workflow run state | 单 Agent message state | 单 Agent + subagent scaffold | 跨人/Agent/群聊的领域事实 |
| 动态扩展 | 强，运行期可卸载 | 图编译为主 | middleware 编译为主 | 配置装配 | 组织策略 + runtime adapter |
| 人工介入 | approval capability | interrupt/Command | HITL middleware | interrupt_on | 统一 Needs You + task capability |
| 并行 | 工具/任务能力 | superstep actor 并行 | 依赖 LangGraph | subagent/tool 并发 | 确定性 DAG + 限额/优先级 |
| 上下文 | session log 派生 + compaction | state/channel | messages + middleware | filesystem/memory/summarization | versioned ContextBundle/Manifest |
| 外部互操作 | MCP 等 plugin | RemoteGraph 但非 A2A | provider/tool integrations | Agent Protocol/ACP 部分 | A2A/ACP/MCP adapters |
| 最佳用途 | 单 Agent 运行内核 | durable workflow | 集成与 agent factory | 快速产品 scaffold | 协作领域内核 |

## 7. 推荐组合架构

```text
WanWork Domain Kernel
  ├─ EventStore / CoordinationEnvelope / Policy / Artifact / TaskGraph
  ├─ WorkflowEnginePort
  │    ├─ Builtin deterministic DAG (默认、低开销)
  │    └─ LangGraphAdapter (复杂长流程、interrupt、checkpoint)
  ├─ AgentRuntimePort
  │    ├─ DSHAdapter (默认目标)
  │    ├─ LangChain/DeepAgentsAdapter (生态/快速验证)
  │    └─ InProcessAdapter (测试)
  ├─ RemoteAgentPort → A2A / ACP
  └─ ToolPort → MCP / native tools
```

关键接口约束：

- `AgentRuntimePort.invoke(envelope, context_manifest)` 返回流式 RuntimeEvent 与最终 AgentResult；
- runtime 不直接修改 TaskGraph，所有业务变化提交为 command/event；
- workflow checkpoint 只保存位置和临时 state，正式 artifact 先由平台提交；
- 每个外部调用携带 correlation/causation/idempotency/authority；
- runtime adapter 有自己的版本和契约测试，允许上游 breaking change。

## 8. 迁移与实施顺序

1. 继续用本项目 dependency-free runtime 固化领域语义和测试。
2. 用 DSH headless/SDK 做最薄 `AgentRuntimePort` proof-of-concept，不 fork DSH。
3. 用现有 `LangGraphBridge` 对一个 Needs You 流程做 Postgres checkpoint 集成测试。
4. 把 v0版实现中的 planner、@ handler、impact check 迁成领域 command handler 或 graph node。
5. 引入 outbox，把 event/artifact commit 与异步 runtime dispatch 解耦。
6. 增加 adapter contract suite：同一个测试对 InProcess、DSH、DeepAgents、A2A 运行。
7. 上线前固定依赖版本并建立上游升级矩阵；DSH 0.1 RC 只允许隔离试用。

## 9. 最终判断

“LangGraph + DeepSeek Harness”是合理方向，但二者相加仍不是完整产品。真正的护城河是上面那层稳定的协作领域模型：群成员身份、任务/尝试、artifact、上下文清单、授权、人工待办与因果事件。DSH 让每个 Agent run 更强、更可替换；LangGraph 让长期流程更可恢复；WanWork 必须拥有两者之间以及两者之上的业务不变量。
