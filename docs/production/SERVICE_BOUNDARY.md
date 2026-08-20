# Quantum Entanglement 服务运行边界

本文定义当前仓库代码允许怎样运行，以及哪些用法必须拒绝。它是发布门禁，不是产品愿景；
README、演示、测试数量、环境变量或操作便利均不得放宽这里的边界。

## 当前结论

当前版本是可重复验证的协作内核，**不是生产服务，也不是商用 GA**。它可在可信开发者控制
的本机或隔离 CI 中运行单元测试、恢复测试、构建验证和演示；不得承载真实客户敏感数据，
不得暴露给不可信调用者，也不得执行不可逆外部副作用。

仓库已经包含事件存储、durable invocation-attempt store、artifact store、inbox/outbox、
publisher、projection、tenant authorization primitive、SQLite backup/restore、严格配置和文件
secret provider、依赖锁/SBOM，以及 durable approval/recovery 等真实组件。这些组件尚未被
可信认证入口、强制 scope repository、action receipt 和统一服务生命周期组合成端到端闭环。

事件内嵌的 `ArtifactLedger` 已有逐行 global replay、累计 state-data 门禁、global-position CAS
和 exact idempotency；其合同见 `ARTIFACT_LEDGER_REPLAY.md`。它仍无 tenant/workspace scope，
也不是 `SQLiteArtifactStore` 的生产替代品。

## 允许的运行方式

| 项目 | 当前支持边界 |
|---|---|
| 拓扑 | 单机；编排器单进程；SQLite 位于可信本地文件系统 |
| 调用者 | 可信开发者或隔离 CI；直接调用 Python API、管理 CLI 或 demo |
| 数据 | 合成数据、去敏 fixture、专用临时数据库 |
| Agent | 内嵌测试 Agent 或受控 fake；无不可信代码沙箱保证 |
| Connector | fake、no-op 或只读 fixture；不得产生真实外部副作用 |
| 网络 | 不提供 HTTP/ASGI 服务；不监听公网；核心测试无需真实外部服务 |
| 运维 | 人工观察下的测试、故障注入、构建和 backup/restore rehearsal |
| 发布 | 可验证源码与本地制品候选；全部 production promotion gate 仍关闭 |

Invocation lease、projection lease 和 SQLite WAL 中的多进程原语只是单机组件能力，不把整个
`OrchestratorKernel` 提升为多进程安全的服务拓扑。

## 绝对禁止项

在新的明确授权和独立安全审查完成前，以下行为不属于任何当前发布阶段：

- 向飞书或企业微信的任何个人、机器人或群聊发送、回复、评论、@、上传或创建内容；
- 使用真实 connector 验证“发送成功”，或把 fake 测试描述为真实平台互操作证据；
- 监听公网地址、接入真实身份提供方、导入生产凭据或承载真实客户数据；
- 把调用者提供的 subject、tenant、workspace 或 role 字段当作已认证上下文；
- 对外宣称 exactly-once、完整多租户隔离、HA、已验证 RPO/RTO 或生产就绪；
- 在 action receipt、effect-unknown reconciliation 和 action-time authorization 未闭环时
  执行不可逆副作用。

凭据只允许通过受支持的 opaque `SecretRef` 和 provider 边界进入未来 composition root。
源码、测试、日志、事件、报告、release evidence 和普通回复不得包含完整 API key、token、
cookie、OIDC credential 或私钥。

## 当前缺失的端到端闭环

以下缺口任一存在，都必须保持 `NON_PRODUCTION`：

1. 没有可信认证入口、版本化服务 API、统一 composition root、health 或 SIGTERM lifecycle；
2. events、snapshots、delivery、attempt 和 projection repository 尚未强制 tenant/workspace
   scope，caller-provided identity 仍不可信；
3. runtime 尚未把 durable invocation attempt 与 `RUNNING` task、result/artifact acceptance
   和恢复状态机连接起来；
4. connector acceptance 尚未与 action digest、authorization/approval revision、outbox ACK 和
   durable action receipt 原子绑定；
5. typed safe logging 目前只迁移 publisher，其他自由文本错误、历史数据库和 exporter 尚未
   完成 redaction/canary 门禁；
6. dependency-risk evaluator 已 fail closed 且 promotion disabled；真实 scanner/database/legal
   policy、签名 provenance、artifact signature、可信 builder、容量、故障、安全、可观测性和
   soak 仍缺真实 promotion evidence；
7. 没有支持拓扑上的 clean-host 部署、升级、回滚、恢复与 RPO/RTO 演练证据。

测试通过只证明对应断言在记录的环境成立，不能替代上述闭环或人工晋级决定。

## 累积运行 Gate

| Gate | 必须完成 | 晋级后最大允许范围 | 仍然禁止 |
|---|---|---|---|
| A | 严格配置/secret/redaction/schema control、可信 request context、全 repository scope、legacy contract rehearsal | 使用合成数据的离线 tenant-scoped 内核 | 网络服务、真实数据、真实 connector |
| B | authenticated loopback API、command receipt、durable action receipt、fenced fake connector、resumable stream、lifecycle | 隔离环境 authenticated E2E + fake connector | 公网、客户数据、真实 connector |
| C | 完整 backup/restore、least-privilege 单节点部署、升级/回滚和实测恢复证据 | 批准拓扑内的受控私有试点候选 | 未批准副作用、未经测量的 SLO/RPO/RTO |
| D | quota/capacity、OTel/告警、worker 隔离、安全评审和 soak | 实测边界内的有限商用候选 | 未经独立授权的飞书/企微 connector |
| E | PostgreSQL、HA/Kubernetes、持续 immutable DR 与重复演练 | 多实例 GA 候选 | 任意未关闭 P0/P1 或未审查 connector |

Gate 是累积条件，后一个 Gate 不能豁免前一个 Gate。当前 A–E 全部关闭。任一 P0、
security-critical、数据丢失、tenant escape、凭据泄露或未经授权副作用都会立即撤销晋级。

## 每次验证和晋级必须保留的证据

- 完整 source commit/tree、干净 worktree、支持的 Python/OS/SQLite 版本；
- 精确 test、lint、type、compile、demo、migration、backup/restore 和 fault 命令与结果；
- dependency lock、reproducible distribution、manifest、双 SBOM 及适用的风险扫描结果；
- 支持拓扑、数据等级、connector allowlist、容量上限、已知限制和未解决 P0/P1；
- 升级/回滚步骤、触发条件、人工 reviewer 与 promotion 决定。

证据写入 `docs/production/evidence/` 并遵守 `RELEASE_GATES.md`。架构期望、测试数量或“设计上
支持”不能替代实际运行证据。

## 当前本地验证命令

```bash
python3 scripts/verify_dependency_locks.py --repository-root .
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
ruff check src tests scripts
ruff format --check src tests scripts
PYTHONPATH=src mypy --strict src/quantum_entanglement
PYTHONPATH=src python3 -m compileall -q src tests scripts examples
PYTHONPATH=src python3 examples/group_chat_demo.py
git diff --check
```

这些命令通过只允许把提交作为“内核验证基线”继续开发，不会自动打开任何运行 Gate。
