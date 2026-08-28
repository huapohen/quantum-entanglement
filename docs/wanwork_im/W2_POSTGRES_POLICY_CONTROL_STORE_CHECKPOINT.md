# W2 PostgreSQL Approval Policy Control Store 工程检查点

> 分支：`dev_wanwork_quantum_entanglement`  
> 代码基线：`16d66b6`  
> 状态：本地与 GitHub 已验证，Notion `local_pending`，未合并 `main`

## 当前可用入口

- Go store：`apps/im-api/internal/platform/postgres/authoritycutover/approval_policy_store_postgres.go`
- Catalog/ACL attestation：`approval_policy_store_catalog.go`
- PostgreSQL 18 integration tests：`approval_policy_store_postgres_integration_test.go`
- Cluster/bootstrap：`deploy/postgres/approval-policy-control-store/bootstrap_cluster.psql`
- Create-only schema：`deploy/postgres/approval-policy-control-store/schema.psql`
- 深度证据：`analysis_report/research/36_postgres_approval_policy_control_store_checkpoint.md`

## 已关闭的工程缺口

- policy activation 不再只依赖内存 fake；
- control store 明确禁止部署到目标 IM physical cluster；
- owner/reader/activator 三角色分离；
- reader 无 CAS，activator 无 direct table access；
- archive/record/head 是一笔原子 CAS，冲突零残留；
- empty、present、corrupt 明确区分；
- commit-unknown 连接隔离并 fresh readback；
- self-reported schema digest 不再是唯一依据；客户端独立验证 catalog、function body、ACL 与 role graph；
- PostgreSQL 18.6 normal/race/concurrency/corruption 权限矩阵通过。

## 仍然 NO-GO

`ActivatedApprovalPolicy` 仍不授权数据库 mutation。下一步必须交付：

1. approval durable consumption + mutation-time policy head fence；
2. execution attempt/step receipt 与 unknown reconcile；
3. cutover executor；
4. off-host immutable high-water/WORM；
5. 生产 Secret/TLS/IaC 与分离恢复演练；
6. 随后才接 Clerk、融云、Agent Store、Agent-as-user、`@Agent` 子群和真实 outbound。

## 验证命令

```bash
cd apps/im-api
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55488/postgres?sslmode=disable' \
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off go test -count=1 ./...

WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55488/postgres?sslmode=disable' \
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off go test -race -count=1 ./...

GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off go vet ./...
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off go mod verify
```

## 部署前置条件

- PostgreSQL major 必须为 18；
- control 与 target `system_identifier` digest 必须不同；
- activator/reader 不能是 owner、不能拥有任何可 SET 的 role membership；
- remote deployment 必须由 Secret manager 注入 credential，并通过 verify-full transport；
- 不允许 `CREATE OR REPLACE`、direct archive repair 或把旧 snapshot 当作新 genesis；
- platform restore authority 属于显式 TCB，未接 off-host anchor 前不得宣称完全 anti-rollback。

