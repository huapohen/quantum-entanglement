# WanWork Quantum Entanglement 原生 IM

本目录定义 `dev_wanwork_quantum_entanglement` 分支的产品、架构、协议、安全和交付合同。该
分支在独立 worktree 中开发，不自动合并回 `main`，完成后由用户人工审阅与合并。

## 目标

从零实现一套与 Quantum Entanglement 协作内核合为一体的企业 IM：

1. 先提供企业办公所需的联系人、组织、单聊、群聊、消息、文件、搜索、已读、通知和群治理；
2. 再让 Agent 以普通群成员身份加入组织和群聊，而不是作为数量受限的供应商机器人；
3. 通过 Agent Store 完成发现、认领、安装、授权和版本治理；
4. 用户 `@Agent` 时创建与父群关联的 Agent 工作子群，Agent 的过程和回复只进入该子群；
5. 将聊天事件连接到 Quantum Entanglement 的 durable invocation、Artifact、Needs You、审批与
   审计链，而不是让 IM 消息冒充业务真相；
6. 使用 provider-neutral ports 隔离 Clerk、融云、模型、工具和跨端 SDK。

## 文档导航

- [PRODUCT_REQUIREMENTS.md](PRODUCT_REQUIREMENTS.md)：产品范围、用户旅程与验收标准。
- [ARCHITECTURE.md](ARCHITECTURE.md)：分层架构、数据所有权、插件边界和关键时序。
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)：阶段、提交序列、门禁和可停点。
- [POSTGRES_PRODUCTION_AUTHORITY.md](POSTGRES_PRODUCTION_AUTHORITY.md)：Gate A0 的 production
  topology/IaC、Secret、authority cutover、receipt/reconcile、rollback 与验收合同。
- [W2_POSTGRES_CUTOVER_PLAN_CHECKPOINT.md](W2_POSTGRES_CUTOVER_PLAN_CHECKPOINT.md)：immutable authority
  specification、canonical cutover plan、strict decoder、测试证据与剩余 No-Go。
- [W2_POSTGRES_RUNTIME_CHECKPOINT.md](W2_POSTGRES_RUNTIME_CHECKPOINT.md)：当前 W2 PostgreSQL
  工程入口、attested runtime composition、Go/No-Go 与接 IM 前剩余 P0。
- [W2_POSTGRES_AUTHORITY_CHECKPOINT.md](W2_POSTGRES_AUTHORITY_CHECKPOINT.md)：`0001..0005`、
  function-only write 与 exact access 的前序工程检查点。
- [RESEARCH_COVERAGE.md](RESEARCH_COVERAGE.md)：`2output` 全部 40 份 Markdown 的行数、摘要、角色和处置。
- [RESEARCH_TRACEABILITY.md](RESEARCH_TRACEABILITY.md)：用户调研快照、采纳/拒绝决策与从证据到验收的映射。

当前 authority/cutover plan 代码证据基线为 `ad60859`；当前深度报告与可视化仍保留
attested runtime baseline `53dd38b`：

- `analysis_report/research/35_postgres_attested_runtime_composition_checkpoint.md`；
- `analysis_report/html/35_postgres_attested_runtime_composition_checkpoint.html`；
- `analysis_report/screenshots/35_postgres_attested_runtime_composition_map.svg` 与 `.png`。

Topic 33 与 Topic 34 保留为 persistence/function-only/exact-access 的历史检查点，不是当前 W2 入口。

后续会增加 API、数据库、融云 provider profile、Clerk 鉴权、跨端和运维文档。任何实现若与
本目录的冻结不变量冲突，必须先修订文档并单独提交，不得在代码中暗改语义。

## 当前安全边界

- 飞书、企微和其他既有聊天系统保持零发送；
- 融云真实 outbound 默认关闭，开发阶段只使用 fake adapter 或专用 sandbox inbound-only；
- Clerk、融云和模型凭据只通过未入 Git 的 secret reference 注入；
- `ext_info` 只保存版本化、限长、非秘密 metadata，不保存 token、权限证明或消息正文；
- 当前 PostgreSQL receipt 只做 tenant command dedupe/digest，不是 provider ACK、完整结果、
  exactly-once 或不可抵赖证据；
- 当前 `0005`、五个 function-only write、exact access manifest、attested runtime pool、startup/readiness
  route barrier 与独立 migrator 已作为受控 PostgreSQL 18.6 子集交付；role provision helper 仍是测试 fixture，
  first-deploy ownership/grant cutover 不是生产 IaC；
- authority expectation 已收敛成 immutable specification；canonical cutover plan 与 strict decoder 已交付，
  但 provisioner preflight、executor、durable receipt/reconcile、secret provider 与 remote TLS E2E 尚未实现；
- strict connection policy 已拒绝 ambient `PG*` presence、remote passwordless、默认 credential/TLS file
  adoption、malformed query、ambient system-root override 与 raw DSN 二次解析；API 也拒绝继承 migration
  URL 的环境。`Pool.Acquire` 仍是 trusted low-level escape hatch，不是 tenant/action authority；
- 当前 access boolean 只作为 resolver 的未来输入；production cutover/credential rotation、Clerk trusted
  tenant context、action-time resolver、recovery、event/outbox 未完成前，真实 IM outbound 保持关闭；
- HTTP 业务响应统一为 200，但业务 `code`、`message` 和 `requestId` 必须表达真实结果；连接被
  网关中断、请求未到达应用或响应无法传输不伪装为业务成功。

## 独立审阅空间

Notion 私人顶层页：

<https://app.notion.com/p/3c9ead4b996e814985fec31a36d70a67?pvs=204>

本地 Git 是实现真相源；Notion 只做阶段性的语义镜像与人工审阅入口。
