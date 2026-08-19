# Quantum Entanglement

面向 WanWork 方向的“人 + Agent 原生群聊协同”底层实验仓。它不是另一个套壳聊天机器人，
而是一套可以独立验证的多 Agent 协作内核：确定性任务图负责可计算的编排，事件日志负责
可恢复与审计，插件负责可替换能力，A2A/ACP/MCP 适配器负责生态互操作，LangGraph 负责
需要持久 checkpoint 与人工中断的上层会话流程。

当前版本实现四条核心不变量：

1. **模型可见即已记录**：发送给任一 Agent 的上下文必须先写入事件日志。
2. **平台持有协作状态**：Agent 可以无状态，任务图、产出物版本、审批和因果链由平台保存。
3. **可计算的事不用模型猜**：依赖、就绪、幂等、版本、权限和失败传播都是确定性逻辑。
4. **协议兼容优先**：不发明新的公网传输层；内部使用带因果、幂等、权限、追踪字段的协作
   Envelope，对外桥接 A2A/ACP，工具与数据接入使用 MCP。

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

