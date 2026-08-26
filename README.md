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

- `docs/production/SERVICE_BOUNDARY.md`
- `docs/production/CURRENT_READINESS.md`
- `docs/production/ROADMAP.md`
- `docs/production/RELEASE_GATES.md`
- `docs/production/APPROVAL_DURABILITY.md`
- `docs/production/WORKFLOW_INITIALIZATION_DURABILITY.md`
- `docs/production/SESSION_RECOVERY.md`
- `docs/production/ARTIFACT_LEDGER_REPLAY.md`
- `docs/production/PROJECTION_HANDLER_SECURITY.md`
- `docs/production/LOGGING_AND_REDACTION.md`
- `docs/production/INVOCATION_RECOVERY_COORDINATION.md`
- `docs/production/REPORT_SYNC_BUNDLE.md`
- `docs/production/DEPENDENCY_LOCKS_AND_SBOM.md`
- `docs/production/DEPENDENCY_RISK_PROMOTION.md`

## 仓库结构

```text
src/quantum_entanglement/   协作协议、事件存储、插件内核、调度与适配器
tests/                      标准库 unittest 测试（无需外部服务）
examples/                   可运行的人与 Agent 群聊协作示例
docs/                       架构、协议与决策记录
analysis_report/            调研总报告、专题研究和原始截图证据
references/                 本地只读参考仓库（被 .gitignore 排除）
worktrees/                  本地历史分支 worktree（被 .gitignore 排除）
artifacts/                  本地评审、发布与旧管理仓库证据（被 .gitignore 排除）
```

分支用途、时间节点和推荐用法见 [`BRANCH_CATALOG.md`](BRANCH_CATALOG.md)，本次目录迁移的
可审计记录见 [`MIGRATION_MANIFEST.md`](MIGRATION_MANIFEST.md)。正式主线就是当前仓库根目录的
`main`，不存在额外的 `main/` 嵌套仓库。

## 本地产品试用

想先从产品界面体验当前协作切片，运行：

```bash
./scripts/start_local_trial.sh
```

页面允许发布任意自定义指令，默认通过本地 `.env` 配置的 OpenAI-compatible GPT 模型依次执行
分析、生成、复核三个 Agent，并展示真实模型 narration、任务 DAG、3 个可预览和下载的 Markdown
Artifact、Needs You、25 步事件时间线和三张内联 SVG 系统图；它不会连接或发送任何飞书、
企微消息。缺失模型配置或调用失败时会明确报错，不会隐式伪装成合成成功；需要离线 fixture 时
显式运行 `./scripts/start_local_trial.sh --synthetic`。
完整启动方式、页面导览、安全边界和故障排查见
[`docs/LOCAL_PRODUCT_TRIAL.md`](docs/LOCAL_PRODUCT_TRIAL.md)。当前仍是本地体验，Gate A–E 全部关闭。

只看终端结果：

```bash
./scripts/start_local_trial.sh --cli
```

## 本地报告库存 checkpoint

生成 report/截图的确定性本地库存（只输出到终端，不访问 Notion、语雀、飞书或企微）：

```bash
python3 scripts/report_sync_bundle.py
```

使用未占用的新文件名保存并立即验证 schema v3 checkpoint：

```bash
python3 scripts/report_sync_bundle.py \
  --output analysis_report/report_sync_bundles/checkpoint-20260827-clawith-local-sync-ledger.json
python3 scripts/report_sync_bundle.py \
  --verify analysis_report/report_sync_bundles/checkpoint-20260827-clawith-local-sync-ledger.json
```

`sourceTargets` 记录的是 source-target entry，不等于远端页面数；所有实时远端回读标记固定为
`false`。最新 Clawith 本地同步台账 checkpoint 固定 40 个本地 source、41 个 source-target 和
20 张图片；旧 `checkpoint-20260827-clawith.json` 作为传输源更新前的不可变历史快照保留。
Clawith 的 Notion 与语雀条目均为 `local_pending`，不构成远端写入或回读证明。其中
`analysis_report/notion_sync_manifest.json` 仍是 2026-08-20 的历史回读控制文件，故意不登记
尚未远端写入并回读的 Clawith 页面；Clawith 的确定性计划页 key 只存在于本地 checkpoint 的
`proposedTargetPageKey`，不能当作真实 Notion 页面标识。完整字段、pinned-read 安全边界、v2→v3
迁移和 recovery 处置见
[`docs/production/REPORT_SYNC_BUNDLE.md`](docs/production/REPORT_SYNC_BUNDLE.md)。

## 开发验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 examples/group_chat_demo.py
```

可选安装 LangGraph 桥接：

```bash
python3 -m pip install -e '.[langgraph]'
```

## 参考实现

本地 `references/` 中保留静然的 `agent_atore_demo`、DeepSeek Harness、LangGraph、
LangChain 和 Deep Agents 的浅克隆，便于逐行核对；它们不被复制进本仓库，也不改变原许可证。
