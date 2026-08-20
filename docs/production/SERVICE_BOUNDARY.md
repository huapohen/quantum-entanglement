# Quantum Entanglement 服务运行边界

本文定义仓库当前代码可以被怎样运行，以及哪些用法必须被拒绝。它是发布门禁的一部分，
不是产品愿景。任何 README、演示、部署说明或发布证据都不得放宽这里的边界。

## 当前结论

当前版本是可重复验证的协作内核，**不是生产服务，也不是商用 GA**。它可以在可信开发者
控制的本机或隔离 CI 中运行单元测试、恢复测试和演示；不得承载真实客户敏感数据，不得暴露
给不可信调用者，也不得执行不可逆外部副作用。

当前已实现的耐久能力包括事件存储、durable invocation attempt、artifact store、outbox、
projection、tenant authorization primitive、SQLite backup/restore 和 durable approval。它们仍是
尚未由认证服务入口与强制 scope repository 组合起来的内核组件，不能据此推导出端到端生产
保证。

## 允许的运行方式

| 项目 | 当前支持边界 |
|---|---|
| 拓扑 | 单机、单节点、单进程；SQLite；可信本地文件系统 |
| 调用者 | 可信开发者或隔离 CI；直接调用 Python API/CLI/demo |
| 数据 | 合成数据、去敏 fixture、专用临时数据库 |
| Agent | 内嵌测试 Agent 或受控 fake；无不可信代码隔离保证 |
| Connector | fake、no-op 或只读 fixture；不得产生真实外部副作用 |
| 网络 | 不提供 HTTP/ASGI 服务；不监听公网；测试默认无需网络 |
| 运维 | 人工观察下的测试、故障注入、backup/restore rehearsal |
| 发布 | 可验证源码基线；未满足生产 promotion gate |

## 绝对禁止项

在新的、明确授权和独立安全审查完成前，以下行为不属于任何发布阶段：

- 向飞书或企业微信的任何个人、机器人或群聊发送、回复、评论、@、上传或创建内容；
- 使用真实 connector 验证“发送成功”，或把 fake 测试结果描述为真实平台互操作证据；
- 监听公网地址、接入真实身份提供方、导入生产凭据或承载真实客户数据；
- 把 caller-provided `subject_id`、tenant/workspace 字段当作可信认证上下文；
- 对外宣称 exactly-once、完整多租户隔离、HA、已验证 RPO/RTO 或生产就绪；
- 在 `action receipt`、reconciliation 和 action-time authorization 尚未闭环时执行不可逆副作用。

凭据只能通过后续定义的 secret handle/provider 进入运行时。源码、测试、日志、事件、报告、
release evidence 和普通回复不得包含完整 API key、token、cookie、OIDC credential 或私钥。

## 当前缺失的服务闭环

以下缺口任一存在，都必须保持 `NON_PRODUCTION`：

1. 没有可信认证入口、版本化 HTTP API、严格配置模型或统一 composition root；
2. events、delivery、attempt、projection 等 repository 尚未强制 tenant/workspace scope；
3. connector accepted receipt 尚未与 outbox/action state 原子持久化和恢复；
4. 缺少完整的日志脱敏、health/readiness、SIGTERM drain 和单节点部署包；
5. backup manifest、升级/回滚、RPO/RTO 和 clean-host release rehearsal 尚未全部闭环；
6. 缺少容量、故障、安全、可观测性和 soak 的商用证据。

测试数量只说明相应断言在指定环境中通过，不能替代上述闭环或发布决策。

## 分阶段运行边界

| Gate | 必须完成 | 允许的新增运行范围 | 仍然禁止 |
|---|---|---|---|
| A | 服务边界、严格配置与脱敏、显式 migration、可信 context、全 repository scope、legacy contract | 本机离线运行 tenant-scoped 内核 | 网络服务、真实数据、真实 connector |
| B | 认证 loopback API、command idempotency、durable action receipt、fenced fake connector、resumable stream、lifecycle | 隔离环境运行 authenticated loopback API + fake connector E2E | 公网、真实 connector、客户数据 |
| C | 完整 backup/restore、容器、升级/回滚与实测恢复证据 | 受控单租户或少量完全隔离的私有试点候选 | 未批准外部副作用、未经测量的 SLO/RPO/RTO |
| D | 配额、容量、OTel、告警、worker 隔离、安全与 soak 评审 | 有限商用候选 | 未经独立授权的真实飞书/企微 connector |
| E | PostgreSQL、HA、Kubernetes、持续异地 DR 与演练 | 多实例 GA 候选 | 任何未关闭 P0/P1 或未审查 connector |

Gate 是累积条件。后一个 Gate 不会豁免前一个 Gate；任一 P0、security-critical、数据丢失、
tenant escape 或未经授权副作用发现都会立即撤销 promotion。

## 每次运行和晋级需要保留的证据

每个阶段至少记录：

- 完整 source commit、干净 worktree 状态、支持的 Python/OS/数据库版本；
- 精确测试、lint、compile、demo、migration、backup/restore 和 fault-injection 命令；
- dependency lock、reproducible distribution、manifest、SBOM 与适用的安全扫描结果；
- 已知限制、未解决 P0/P1、支持拓扑、容量上限和回滚方法；
- 人工 promotion 决定及其适用范围。

证据放在 `docs/production/evidence/`，并按 `RELEASE_GATES.md` 验证。失败或缺失证据必须被
明确记录，不能用“设计上支持”“测试很多”或“应该可恢复”替代。

## 当前推荐验证命令

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
PYTHONPATH=src python3 examples/group_chat_demo.py --compact
python3 -m compileall -q src tests scripts
ruff check src tests scripts
git diff --check
```

这些命令通过仅允许将提交作为“内核验证基线”继续开发；它们本身不改变本文件定义的运行
边界。
