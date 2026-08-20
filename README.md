# Quantum Entanglement

私有 GitHub 仓库：<https://github.com/huapohen/quantum-entanglement>

面向 WanWork 方向的“人 + Agent 原生群聊协同”底层实验仓。它不是另一个套壳聊天机器人，
而是一套可以独立验证的多 Agent 协作内核：确定性任务图负责可计算的编排，事件日志负责
历史重建与审计，插件负责可替换能力；当前已实现 A2A 数据映射和 LangGraph 桥接，ACP、
MCP 及真正的跨进程协议互操作仍属于后续阶段。

当前版本实现四条核心不变量：

1. **模型可见即已记录（标准编排路径）**：经 `Orchestrator` 发送给 Agent 的上下文会先写入
   事件日志；直接调用 runtime adapter 的路径尚未提供同等保证。
2. **平台持有协作状态**：Agent 可以无状态，任务图、产出物版本、审批和因果链由平台保存。
3. **可计算的事不用模型猜**：依赖、就绪、幂等、版本、权限和失败传播都是确定性逻辑。
4. **协议兼容优先**：内部 Envelope 已携带因果、幂等、权限和追踪字段；当前只提供 A2A
   数据映射，ACP、MCP 和传输层 adapter 尚未实现。

当前 `0.1.x` 是可运行的内核实验基线，不是生产发布。事件历史可以重建已记录状态，但运行中
attempt 的 lease、heartbeat、崩溃接管以及外部副作用确认仍在 `0.2.0` 门禁内。完整差距、阶段
边界和发布条件见：

- `docs/production/READINESS_AUDIT.md`
- `docs/production/ROADMAP.md`
- `docs/production/RELEASE_GATES.md`
- `docs/production/APPROVAL_DURABILITY.md`
- `docs/production/WORKFLOW_INITIALIZATION_DURABILITY.md`

## 仓库结构

```text
src/quantum_entanglement/   协作协议、事件存储、插件内核、调度与适配器
tests/                      标准库 unittest 测试（无需外部服务）
examples/                   可运行的人与 Agent 群聊协作示例
docs/                       架构、协议与决策记录
analysis_report/            调研总报告、专题研究和原始截图证据
references/                 本地只读参考仓库（被 .gitignore 排除）
```

## 快速验证

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 examples/group_chat_demo.py
```

可选安装 LangGraph 桥接：

```bash
python3 -m pip install -e '.[langgraph]'
```

## 参考实现

本地 `references/` 中保留静然的 `agent_atore_demo`、DeepSeek Harness、LangGraph、
LangChain 和 Deep Agents 的浅克隆，便于逐行核对；它们不被复制进本仓库，也不改变原许可证。
