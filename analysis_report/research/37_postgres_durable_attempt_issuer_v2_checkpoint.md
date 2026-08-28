# PostgreSQL durable attempt issuer v2 阶段检查点

> 日期：2026-08-29（Asia/Shanghai）  
> 分支：`dev_wanwork_quantum_entanglement`（尚未合并 `main`）  
> 代码证据基线：`ec9ed68`  
> 一级研究根：`/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output` 及 `more/`  
> 同步状态：本地/GitHub 待本批推送；Notion 按用户决定延后，当前 `local_pending`

## 1. 结论

本检查点把 execution admission 的第一段 durable authority 从“可验证的内存/抽象 store”推进为
PostgreSQL 18.6 上可独立部署的五角色边界：`owner`、`reader`、`activator`、`attempt issuer`、
`fencer`。attempt issuer 只能在 approval 验签、plan 绑定和 preflight 通过后签发一次不可变
post-preflight grant；fencer 只能消费真实 durable grant，不能自行制作 generation、ID 或 receipt。

已经落地的关键事实：

1. attempt record 升级为格式 `/2`，包含完整 policy head、approval、plan、preflight 向量、有效期
   和 `AttemptIssuanceID`；receipt 覆盖除数据库分配字段外的所有字段，并对数据库分配的 generation、ID、
   createdAt 一并封存；
2. `CompareAndIssue` 是唯一事务写边界，数据库分配单调 generation、opaque ID 和 UTC 秒级时间；
3. exact issuance retry 先按稳定 issuance ID 回读，再做时间和 policy-head 检查，支持 commit-unknown；
4. 读回使用严格 canonical JSON decoder（unknown/trailing field、非 canonical bytes、超 64 KiB 均拒绝）；
5. fence 格式升级为 `/3`，逐字段绑定 attempt，而不是只比较 plan/target；admission trust 函数要求真实
   attempt row 的完整向量、canonical request 和 receipt 同时成立；
6. v2 catalog/contract digest 已重新冻结，SQL identity 返回的 contract digest 与 Go expectation 对齐；
7. 受控 SQL smoke test 已证明：真实 grant 签发、issuance retry 幂等、authoritative readback、真实 fence
   opening 均通过；完全自洽但没有 durable attempt row 的伪造 grant 返回 `attempt_untrusted`。

这仍不是目标 IM mutation 的 exactly-once 证明。没有 target-side permit/before-state CAS、mutation receipt、
reconciliation 和 off-host high-water 前，不开放真实 outbound 或生产 cutover。

## 2. 五角色最小权限矩阵

| 角色 | 允许的函数 | 明确禁止 |
|---|---|---|
| owner（NOLOGIN） | 全部内部函数，持有 schema/table/function | 作为运行时 credential 使用 |
| reader（LOGIN） | identity、policy read、fence read | activation CAS、attempt issue/read、fence open |
| activator（LOGIN） | identity、policy read、activation CAS | attempt issue、fence open、任何 direct table DML |
| attempt issuer（LOGIN） | identity、policy read、attempt read、attempt issue | activation CAS、fence open、任何 direct table DML |
| fencer（LOGIN） | identity、policy read、fence read、fence open | attempt issue/read、activation CAS、任何 direct table DML |

五个角色均 `NOINHERIT`、非 superuser、不可建库/建角色、不可 replication/bypass RLS，且受保护角色图
同时检查入向和出向 membership。任何 role/ACL/catalog 漂移都会在每次连接上被拒绝。

## 3. Go 侧边界

新增入口：

- `NewPostgresApprovalExecutionAttemptStore`：只接受 schema-v2 expectation，且 login 必须等于
  `ControlAttemptIssuerRole`；
- `Load(namespace, issuanceID)`：只按平台生成的稳定 issuance ID authoritative readback；
- `CompareAndIssue(namespace, candidate)`：只接受 package-owned candidate，canonicalize 后调用固定 SQL；
- `ApprovalExecutionAttemptIssuer.Issue(plan, approval, report)`：先用 issuer-owned UTC clock 做完整
  `ValidatePreflightReport`，再进入 durable store，任何 ACK 歧义都使用不继承取消信号的 bounded fresh readback。

attempt 对外仍为 opaque capability：`String`、`GoString`、`LogValue` 和 JSON 都不泄露 ID、receipt 或
内部完整向量。错误只返回固定 sentinel，不回显 DSN、密码、canonical 文档或底层错误 canary。

## 4. PostgreSQL v2 部署与验证

新增部署文件：

- `deploy/postgres/approval-policy-control-store/bootstrap_cluster_v2.psql`
- `deploy/postgres/approval-policy-control-store/schema_v2.psql`
- `deploy/postgres/approval-policy-control-store/upgrade_cluster_v1_to_v2.psql`
- `deploy/postgres/approval-policy-control-store/upgrade_schema_v1_to_v2.psql`
- `deploy/postgres/approval-policy-control-store/tests/v2_fixture.psql`
- `deploy/postgres/approval-policy-control-store/tests/v2_contract.psql`

fresh PostgreSQL 18.6 安装后冻结的摘要：

```text
Go contract digest:    sha256:86e6fd8685696f91c63dcca441a01d27152826a2f2990c5e9c0e2ef26b4eb474
Catalog digest:        sha256:04d1d97ec0d0cb9b1fab51d89e4b1f4aca17d6c3408ab1fc7d7fa93e3ff49f13
Attempt record format:  wanwork.im.postgres-authority-approval-execution-attempt/2
Fence record format:   wanwork.im.postgres-authority-approval-execution-fence/3
```

推荐验证顺序：

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_wanwork_quantum_entanglement
psql "$POSTGRES_ADMIN_URL" --file=deploy/postgres/approval-policy-control-store/bootstrap_cluster_v2.psql
psql "$POSTGRES_CONTROL_ADMIN_URL" --file=deploy/postgres/approval-policy-control-store/schema_v2.psql
psql "$POSTGRES_CONTROL_ADMIN_URL" --file=deploy/postgres/approval-policy-control-store/tests/v2_fixture.psql
psql "$POSTGRES_CONTROL_ADMIN_URL" --file=deploy/postgres/approval-policy-control-store/tests/v2_contract.psql
cd apps/im-api
go test ./...
go test -race ./...
go vet ./...
go mod verify
```

本轮实际在本机 PG 18.6 fresh database 上完成安装、fixture、contract smoke test；未把本机 socket、密码或
任何完整 credential 写入报告。

## 5. 从一级研究吸收的约束

`2output` 中 DeepSeek Harness、Sandbase Harness、AgentSpace、Clawith、Raft/S-LOCK 等材料没有被直接
复制进 runtime。它们被转换为本阶段的低层规则：

- Harness/插件生命周期只是 runtime seam，不能自行声明 authority；
- schema/validator 的存在不是接线证据，必须在真实连接上做 catalog、ACL、role 和 function-body attestation；
- health/readiness 必须形成 credential→scope→effect 的证据漏斗；
- Agent/人协作的审批必须可回放，模型 narration、IM message 和 LangGraph checkpoint 不能冒充 durable fact；
- activity/wake 可以重复或丢失，不能替代 issuance ID、receipt 和 authoritative readback。

## 6. 当前剩余 NO-GO

1. target-side mutation permit、before-state CAS、mutation receipt 和 crash reconciliation；
2. approval consumption 与 mutation-time policy-head fence 的完整 executor；
3. control/target 独立 restore、PITR、failover 和 off-host WORM/high-water anchor；
4. 生产 Secret/TLS/IaC、credential rotation、旧 session drain 和云环境 verify-full E2E；
5. Clerk、融云、Agent Store、Agent-as-user、`@Agent` 子群以及 Web/Desktop/Mobile 全链路接入；
6. 上述边界完成前，不得把“attempt 已签发”写成“目标 mutation 已完成”，也不得开启真实 IM outbound。

## 7. 同步策略

本地报告、SQL、Go、测试和 Git 提交全部完成后，再批量上传 Notion 并逐页回读。频繁编辑期间操作 Notion
会引入网络往返、页面块重写和回读核验，通常比本地提交慢；因此本阶段以本地 Git 与 GitHub 为事实源，
Notion 只保留待同步标记，不影响代码推进。
