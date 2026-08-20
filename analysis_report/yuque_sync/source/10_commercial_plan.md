# 商用实施计划｜Quantum Entanglement 0.2 → 1.0

> 把已验证的协调内核升级为可部署、可恢复、默认拒绝、支持多租户的商用服务。阶段只有在代码、测试、运行手册、迁移、回滚和发布证据同时完成后才能关闭。
## 关联规格
- [综合报告｜人与多 Agent 群聊协同产品：调研、架构与实现](https://app.notion.com/p/3c1ead4b996e819897daff4941dcbd44)
- [本地生产路线图](https://github.com/huapohen/quantum-entanglement/blob/main/docs/production/ROADMAP.md)
## 核心要求
- 可靠执行：attempt、lease、heartbeat、timeout、retry、receipt、compensation。
- 安全隔离：tenant/workspace/member、默认拒绝、最小 capability、secret handle、SSRF 防护。
- 服务互操作：版本化 API、认证、SSE cursor、MCP、A2A、签名 webhook。
- 运维交付：health、metrics、trace、alert、容量、备份恢复、升级回滚。
- 产品闭环：群聊、任务图、Artifact、Needs You、审计时间线同源。
## 工作假设
- SQLite 支持受控单节点 pilot；PostgreSQL 是分布式商用目标。
- 身份、vault 和部署平台通过 ports 接入，不绑定供应商。
- 飞书与企微继续严格只读；未获得新的明确授权前，只使用 fake 和只读 fixture。
- 商用级由测量证据和发布门禁定义，不由版本号或测试数量单独定义。
## 阶段
### 0.2.0｜可靠单节点服务
- [ ] Outbox publisher 与安全重试/dead letter。
- [ ] Task attempt lease/heartbeat/timeout/fencing。
- [ ] Artifact 持久化、projector/upcaster、action receipt。
- [ ] SQLite migration、backup/restore、health 与运行手册。
**运行边界**：受控内部 pilot；fake 或明确批准的 connector。
### 0.3.0｜安全多租户
- [ ] Tenant/workspace/member/service principal 与 RBAC/ABAC。
- [ ] Scoped expiring capability；委托不能提权。
- [ ] Tenant-aware repository 与 tamper-evident audit。
- [ ] Secret handle、脱敏、保留删除、SSRF/出网策略。
**运行边界**：多个内部组织共享部署，跨租户测试 fail closed。
### 0.4.0｜认证服务与互操作
- [ ] HTTP/OpenAPI、OIDC/JWT、resumable SSE/backpressure。
- [ ] MCP consent/data gate 与 A2A 1.x SDK/TCK。
- [ ] 签名 webhook/replay protection。
- [ ] Session/task/artifact/approval/audit 最小控制台。
**运行边界**：认证客户端与版本固定的 A2A/MCP 参与者可接入。
### 0.5.0｜分布式运维
- [ ] PostgreSQL 与 SQLite 数据迁移。
- [ ] 分布式 worker leasing/cancel/backpressure。
- [ ] OpenTelemetry、dashboard、alert、load/chaos/soak。
- [ ] 容量、autoscaling、SLO 与错误预算。
**运行边界**：水平扩展拓扑满足已发布 SLO。
### 1.0.0｜正式商用 GA
- [ ] Least-privilege reference deployment。
- [ ] Locked dependencies、SBOM、漏洞门禁、provenance。
- [ ] Upgrade/rollback/backup/restore/DR/incident runbooks。
- [ ] 安全评估、性能报告、RC soak 与 operational acceptance。
## 强制成功标准
- [ ] 默认分支每个 commit 后可运行；独立行为独立 commit。
- [ ] 每阶段有版本、changelog、迁移/回滚、测试与运行证据。
- [ ] 未授权外发、跨租户读取和 secret 泄露均为零。
- [ ] 备份、恢复、故障切换和升级在干净环境真实演练。
- [ ] GA 前无未解决 P0/P1。
## 执行台账
- [Quantum Entanglement Production Tasks](https://app.notion.com/p/36464c0e1cff46fa897977dd86b70fd0)：14 个首批任务，包含 Priority、Phase、Type、Acceptance、Plan、Spec、Commit、Release 与 Evidence。
- [生产路线图](https://github.com/huapohen/quantum-entanglement/blob/main/docs/production/ROADMAP.md) 与 [逐提交发布门禁](https://github.com/huapohen/quantum-entanglement/blob/main/docs/production/RELEASE_GATES.md) 是本地执行基线。
## 当前状态
- Phase 0：✅ 54 项基线测试与三 Agent 确定性 demo。
- Phase 1：🔄 进行中；可靠投递、VerifiedCapability 安全边界、durable attempt/lease 三路并行。
- Phase 2–5：⏳ 尚未进入可发布状态。
### 已完成证据（2026-08-20）
- 十维生产就绪审计：425 行，明确 P0/P1/P2 与 0.2→1.0 发布边界。
- 安全政策与威胁模型：24 条攻击/故障路径、capability/模型/工具/存储/遥测控制及验证计划。
- GitHub Actions CI：Python 3.9/3.12、安装、compile、54 项测试与 demo；[run 32278065566](https://github.com/huapohen/quantum-entanglement/actions/runs/32278065566) 成功。
- 私有 GitHub 主分支已通过 Git Data API 同步，远端文件树与本地已提交树一致。
### 尚未关闭的 P0/P1
- durable attempt 存储原语首轮测试通过，但尚未接入 Orchestrator 的 task/result/artifact/terminal 原子链，不能声称永久 RUNNING 已解决。
- Outbox Publisher 正在补 hard timeout、stop race、lease fencing、ACK/NACK 异常和双 publisher 对抗测试，二次审查前不入库。
- 多租户授权正在修复可信时钟、VerifiedCapability、完整委派链、祖先撤销、audience/TTL 与严格反序列化，攻击回归未全绿前不入库。
最近同步：2026-08-20。任何飞书/企微发送、回复、评论、@ 或上传仍然禁止；仅允许 fake/只读 fixture。

---

来源：https://app.notion.com/p/3c1ead4b996e81ea9d69d606525f8d93?pvs=204

