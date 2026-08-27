# v0版 `agent_atore_demo` 实现审计

> 审计对象：`references/agent_atore_demo` 当前本地快照  
> 审计日期：2026-08-19  
> 审计方式：只读代码、文档、依赖和仓库结构审计；未改参考仓，未启动外部服务，未使用或输出任何凭据  
> 目标方向：群聊原生 Agent 协作产品；LangGraph + DeepSeek Harness（下文简称 DSH）底层组合；能力、运行时和通道插件化；建立可演进的协作协议族

## 0. 结论先行

这个仓库不是空壳。它已经把几个最有价值、也最容易在产品早期被忽略的概念做成了可读代码：

1. 用 LangGraph 显式表达意图路由、DAG 派发、人工检验和用户验收，而不是把全部流程藏在一个 ReAct 循环里。
2. 把外部 Agent 当远程 peer，通过一组 A2A 风格的 JSON-RPC/SSE 接口调用，而不是要求所有 Agent 共享同一框架进程。
3. 把平台记忆、主 Agent 压缩上下文、子 Agent 精确上下文包分开。
4. 子 Agent 以自己的身份在消息流中发言，并把 artifact 当一等对象管理。
5. 用追加 task 和 artifact 版本表达修订历史，而不是覆盖已完成任务。

但它仍然是一个**单用户、单进程、内存实时总线、无安全边界的演示实现**，不能作为生产系统直接延长。尤其需要避免一种误判：代码里有 PostgreSQL、LangGraph Checkpointer、A2A Client 和 SSE，并不等于已经具备分布式可靠性、标准 A2A 互操作、多人群聊或故障恢复。

最关键的审计结论如下：

- **建议复用概念和局部纯逻辑，不建议把当前 runtime 直接扩成产品主干。** 应以新代码仓建立领域协议、持久化事件入口、任务尝试模型和插件 SPI，再把现有节点逐个迁入。
- **LangGraph 适合继续做 durable control flow，DSH 适合成为 Agent 执行内核插件。** 两者不应互相侵入：LangGraph 管协作状态和人机 gate，DSH 管单个 Agent run 的模型循环、上下文裁剪、工具执行与轨迹。
- **不需要从零发明一个取代 A2A/MCP 的万能协议。** 需要自定义的是本产品独有的“群聊协作语义”：身份、话轮、任务/尝试、artifact 依赖、审批、幂等、因果和权限；外部互操作继续用 A2A，工具用 MCP，事件封装借鉴 CloudEvents，追踪用 OpenTelemetry。
- **当前最高优先级不是增加更多 Agent，而是建立可靠性和安全地基。** 没有租户隔离、幂等键、事务出站、持久队列、策略化上下文披露和 task attempt，就无法安全支撑群聊原生并发。

### 0.1 真实成熟度判断

| 维度 | 当前判断 | 说明 |
|---|---|---|
| 架构概念 | 中上 | 关键概念边界较清楚，文档量大 |
| 单机 demo 功能 | 中 | 主链代码完整，但当前快照没有可执行验证证据 |
| 多 Agent 编排 | 中 | 有 DAG、并行批次、gate、@旁路；边界条件有实质错误 |
| A2A 互操作 | 中下 | 客户端/服务端自实现子集互通；未经过官方 SDK/TCK 证明 |
| 群聊原生 | 低 | 只有角色化消息视图，没有多人、成员、权限、顺序和投递语义 |
| 可靠性 | 低 | 进程内队列/锁/总线，副作用与 checkpoint 非原子 |
| 安全 | 极低 | 无鉴权，注册入口可 SSRF，slug 可抢占，外发上下文无策略 |
| 可观测性 | 极低 | 主要是日志和表记录，无 trace/metric/usage/审计闭环 |
| 可测试性 | 极低 | 当前仓库没有任何测试文件 |
| 生产可用性 | 不可用 | 存在多项 P0 阻断项 |

## 1. 审计范围、证据与可运行性

### 1.1 已审计的主要代码

- 组合根与配置：`backend/app/main.py`、`backend/app/common/config.py`、`backend/app/modelgateway/provider.py`
- 数据模型：`backend/app/common/db/models.py`、`backend/sql/001_artifact_unique.sql`
- 网关：`backend/app/gateway/routers/*.py`、`backend/app/gateway/transport/*.py`
- 编排：`backend/app/runtime/graph.py`、`state.py`、`session_manager.py`、`main_agent.py`
- 调度与上下文：`task_planner.py`、`task_runner.py`、`blackboard.py`、`artifacts.py`、`messages.py`
- 全部 LangGraph 节点：`backend/app/runtime/nodes/*.py`
- A2A 与商店：`backend/app/common/a2a/client.py`、`backend/app/store/*.py`
- 外部 Agent 参考：`mock_agents/a2a_server.py`、`mock_agents/main.py`、`mock_agents/llm.py`
- 前端实时链路：`apps/web/src/api.ts`、`apps/web/src/store.ts`、`apps/web/src/download.ts`
- 当前文档：`README.md`、`docs/current/*`、`docs/research/*`

### 1.2 仓库证据边界

当前 Git 历史是 shallow/grafted 快照，`git log` 只能看到 `8c477e0 更新 design-decisions.md`。因此本审计能够判断“当前代码是什么”，不能据此还原完整演进历史，也不能验证文档所称的历次端到端测试。

`README.md` 和 `docs/README.md` 多次引用 `docs/current/status-audit.md`，但当前快照没有该文件；也引用 `docs/history/qa.md` 等 `docs/history/` 内容，而当前快照没有整个 `docs/history/`。这使“逐条现状对账”这一最重要的自证材料不可用。

### 1.3 当前快照可运行性

只读检查结果：

- Python 文件共 56 个，均可被 AST 解析，说明没有明显语法残缺。
- 当前机器默认 Python 为 3.9.6；代码大量使用 `str | None` 等 PEP 604 类型表达式，且没有 `from __future__ import annotations`，实际运行要求 Python 3.10+。
- `README.md`、`backend/requirements.txt`、项目根均未声明 Python 版本，也没有 `pyproject.toml` 或 Python lockfile。这会导致本机按默认 Python 安装后在导入阶段失败。
- 根目录声明 Node `>=20`，本机 Node/pnpm 满足；但当前快照没有 `node_modules`，因此未执行前端 typecheck/build。
- 当前快照没有 `backend/.env`，配置对象 `Settings()` 在模块导入时要求 `DATABASE_URL`，所以后端不能在未配置环境时启动。仓库有 `backend/.env.example`，但没有自动化 bootstrap。
- 仓库中没有测试文件；`find` 只命中名为 `inspect_gate.py` 的业务文件。没有单测、集成测试、协议兼容测试、迁移测试或端到端测试。
- 依赖只设宽泛版本区间，没有生成锁文件或镜像 digest。Python 依赖的可重复构建性不足。

因此更准确的表述应是：**当前快照具备一条完整的 demo 代码路径和手工启动说明，但没有随仓库交付可重复验证的“已运行通过”证据。**

### 1.4 建议立即补的可运行性基线

在迁移任何功能前先补：

1. `pyproject.toml` 明确 `requires-python >= 3.11`，生成锁文件。
2. 单命令启动开发环境，并在启动前验证 DB、模型配置、迁移版本和外部 Agent。
3. 把 `create_all()` 改为 Alembic 迁移；CI 从空库和旧库分别升级验证。
4. 增加不依赖真实模型的协议级 fake Agent；真实模型验证作为可选 smoke test。
5. CI 至少执行 Python lint/type/test、前端 typecheck/build、PostgreSQL 集成测试、A2A 合同测试。

