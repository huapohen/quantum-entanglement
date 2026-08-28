# W2 PostgreSQL Authority Persistence 检查点

> 检查点日期：2026-08-28
>
> 分支：`dev_wanwork_quantum_entanglement`
>
> 基线 HEAD：`8d662bf4faec1cfaa12b63f4dfc2132ae6869dbb`
>
> 判定：`W2 in progress / persistence substrate checkpoint`

## 1. 这个检查点的用途

本文件是 W2 工程执行入口，用来回答四个问题：

1. 当前 PostgreSQL 层已经能承担什么；
2. 哪些边界已由数据库与真实测试强制，而不只是文档约定；
3. 为什么现在还不能把真实 IM 流量建立在这套 authority 上；
4. 下一批必须按什么顺序关闭 P0。

详细证据、调研映射、提交台账和禁止性宣称见
`analysis_report/research/33_postgres_authority_persistence_checkpoint.md`。

## 2. 当前已交付

### 2.1 Migration 系统

- PostgreSQL 18.x fail-closed runner；
- 4 个连续、checksummed migration；
- `wanwork_meta.schema_migrations` 精确 ledger；
- session advisory lock；
- gap/future/checksum/name/ledger-shape drift 拒绝；
- 每次提交前累计验证 `0001..current` postcondition；
- transaction 内固定 `search_path=pg_catalog`；
- token-aware DDL statement allowlist，并拒绝 `CREATE TABLE AS SELECT`、危险 DEFAULT、
  `set_config`/`pg_sleep` 等 data-executing form；
- commit/lock outcome unknown 时连接隔离；
- rollback/unlock/close 使用有界清理 context。

### 2.2 Authority schema

4 个 migration 共建立 22 张 `wanwork_im` 表：

| Migration | 交付 |
|---|---|
| `0001_authority_roots` | provider realm、tenant、workspace roots |
| `0002_identity_authority` | human principal、Clerk identity binding、Actor、tenant membership、provider Actor binding |
| `0003_conversation` | ordinary direct/group conversation、RongCloud group binding |
| `0004_conversation_authority` | conversation membership、access、tenant command receipt |

17 张 tenant-scoped 表启用并强制 RLS。head/current snapshot、revision/status/type、active uniqueness、
tenant FK 和 provider realm 等不变量进入 schema。

### 2.3 Repository 与 UoW

- typed store port，不暴露 `pgx.Tx`；
- Conversation/provider binding/membership/access repository；
- repository 固定 tenant，每条 SQL 同时有显式 tenant predicate；
- create `0→1`，update `N→N+1`；
- callback 返回后 repository 失效；
- callback 即使忽略 repository error，transaction 仍被 poison；
- read-only Repeatable Read；write Serializable；
- same command exact replay；same key/different request digest conflict；
- session advisory lock 在 transaction 前获取；
- final receipt 与业务 mutation 同 transaction 单次 INSERT；
- unknown commit 用新连接 readback，不盲重试。

## 3. 已冻结的不变量

### 3.1 Identity

- global human principal 与 tenant Actor 分离；
- external identity 以 provider realm 隔离；
- Actor prefix/type 精确匹配；
- tenant membership 不是 conversation membership；
- Actor row、provider binding、prefix、revision 都不自动授予执行权限。

### 3.2 Conversation

- 当前只持久化 ordinary `direct/group`；
- `agent_thread` 未实现时 fail closed；
- nullable workspace 与 Go 合同一致；
- type 在 head 冻结，不允许 revision drift；
- RongCloud binding 只映射 group；
- active provider subject 跨 tenant 唯一；
- provider binding 是本地 routing metadata，不是 provider truth。

### 3.3 Conversation authority

- ordinary participant 只允许 human/Agent Actor；
- membership 与 access 独立；
- access 是六个 immutable boolean projection；
- all-false projection 表示显式撤权；
- access 字段只是 resolver 的未来输入，不是已经部署的 PEP；
- receipt 是 database command dedupe/digest primitive，不是 provider ACK、audit signature 或完整结果。

membership head 存在不等于 current membership active。成员移除必须在同一 UoW 追加 removed membership
与 all-false access；action gate 必须同时重验 active membership 与 permission bit。

receipt result digest 不能重建 typed result。command/digest canonicalization 必须版本化；replay/unknown
resolution 后仍需 typed aggregate readback 与 revision/integrity 验证。

## 4. 测试门禁

本检查点在本机 PostgreSQL 18.6 上通过：

```bash
cd apps/im-api

WANWORK_TEST_POSTGRES_ADMIN_URL="$LOCAL_TEST_POSTGRES_URL" \
  go test -count=1 ./internal/platform/postgres/migrations

WANWORK_TEST_POSTGRES_ADMIN_URL="$LOCAL_TEST_POSTGRES_URL" \
  go test -race -count=1 ./internal/platform/postgres/migrations

WANWORK_TEST_POSTGRES_ADMIN_URL="$LOCAL_TEST_POSTGRES_URL" \
  go test -count=1 ./internal/platform/postgres/imstore

WANWORK_TEST_POSTGRES_ADMIN_URL="$LOCAL_TEST_POSTGRES_URL" \
  go test -race -count=1 ./internal/platform/postgres/imstore

go vet ./internal/platform/postgres/migrations
go vet ./internal/platform/postgres/imstore
```

`LOCAL_TEST_POSTGRES_URL` 只在本地 shell 注入，不写进 Git、文档示例值、Notion 或日志。

关键行为证据：

- fresh/repeat migration 与 ledger/schema drift；
- 恶意新 migration 削弱旧 RLS 时整笔回滚；
- hostile search path；
- 两 migrator serialize；
- identity/conversation/authority FK、type、status、revision、active uniqueness、RLS；
- same command replay 与 request digest conflict；
- rollback 无部分状态/receipt；
- ignored repository error 仍 poison；
- escaped/panic callback repository 失效；
- 64 路 exact retry callback 一次；
- 64 路相同 CAS 单 winner；
- synthetic commit ACK loss 新连接收敛；
- runtime fixture 无 snapshot/receipt UPDATE/DELETE/TRUNCATE 权限。

## 5. Go / No-Go 判定

| 使用场景 | 判定 | 原因 |
|---|---|---|
| 继续本地 schema/repository 开发 | GO | 当前 transaction、CAS、test seam 足够稳定。 |
| 作为后续 authority resolver 的存储底座 | GO with gates | 必须先完成受控写函数、roles/ACL 和 authenticated tenant context。 |
| 接 fake IM vertical slice | GO with explicit fake labels | 不得访问真实 provider，不得把 access/receipt 宣称为生产 enforcement。 |
| 接融云 sandbox inbound-only | NO-GO（当前） | 尚无 callback auth、mapping resolver、dedupe/resume 与 service DB composition。 |
| 接真实 IM 流量或开放 outbound | NO-GO | DB credential containment、action-time authorization、outbox/reconcile 均未完成。 |
| 标记生产商用级 | NO-GO | recovery、role provisioning、observability、provider contract 和业务闭环缺失。 |

## 6. 下一批 P0：必须按顺序执行

### 6.1 P0-A：`0005` 受控写函数与 exact role/ACL manifest

目标是让 runtime credential 无法绕过 repository 直接改 head 或插 snapshot。

验收：

- non-login owner、migrator、runtime exact role contract；
- runtime `NOSUPERUSER NOBYPASSRLS NOINHERIT`；
- runtime 无 schema `CREATE`、无 owner membership；
- runtime 无 head `UPDATE`、snapshot `INSERT/UPDATE/DELETE/TRUNCATE`；
- revision 写只能调用固定 `SECURITY DEFINER` function；
- function owner、definition、security definer、fixed search path、volatility、PUBLIC/named ACL 被 manifest
  精确冻结；
- direct SQL、alternate named grant、owner-member、BYPASSRLS、function replacement 和 search-path attack
  全失败。

设计纪律：不要简单放开 migration allowlist 的所有 `CREATE FUNCTION`。先冻结 function catalog
manifest，再允许明确的受控函数语句。

### 6.2 P0-B：Trusted authenticated tenant context

```text
Clerk verified claim
  -> realm-scoped human identity binding
  -> active human principal
  -> active tenant membership
  -> exact Actor
  -> path tenant consistency
  -> trusted RequestContext
  -> TenantUnitOfWork
```

客户端 body/header/GUC 自报 tenant 不能覆盖 verified claim。claim/path mismatch 和 revoked/suspended/
removed 任一状态都必须在 DB operation 前拒绝。

### 6.3 P0-C：Active authority resolver

ordinary read/send/manage 必须组合 tenant、Actor、Conversation、membership、explicit permission 的 active
状态和 current revision。Agent invoke/publish 继续叠加 installation/release/mandate/capability/budget/
Artifact/Acceptance。

### 6.4 P0-D：Migration 与真实服务启动

- 独立 migrator 或 startup gate；
- exact server/schema/role/function manifest 未通过时 readiness=false；
- pool 用 runtime role，不能复用 owner/migrator credential；
- shutdown 等待 in-flight UoW，unknown commit 进入 operator-visible 状态；
- config 只引用 secret，不记录原始 credential。

### 6.5 P0-E：恢复与兼容性

- dump/restore + role/function/ACL readback；
- DB/process restart 与 kill-9；
- old binary/future schema fail closed；
- backup/restore 中 tenant isolation 与 receipt/revision 不漂移；
- 恢复脚本与 evidence 不泄露 credential。

### 6.6 P0-F：PostgreSQL event store/outbox/checkpoint

实现 transaction-bound stream append、global position、outbox、projection checkpoint、backfill+live 和
crash/reopen/restore。现有 memory fake 和 command receipt 不得被复用成 production durability 声明。

## 7. 后置对象

完成上述 P0 seam 后再按以下顺序扩展：

1. Agent installation/release repository；
2. `agent_thread` 与 parent/root/invocation lineage；
3. message/reaction/read state；
4. verified RongCloud adapter 与 inbound dedupe/resume；
5. Task/Attempt/Action/Budget/NeedsYou；
6. Artifact/Acceptance/explicit publish-ref；
7. Web/PWA 审阅体验与跨端。

后置不等于删除 TODO。它表示在底层 authority seam 可被 DB credential 绕过时，不让更多产品对象建立在
不稳定安全边界上。

## 8. 禁止性宣称

- “生产级多租户已经完成”；
- “六个权限位已经控制真实动作”；
- “database receipt 实现 exactly-once”；
- “provider binding 证明融云群/ACK”；
- “RLS GUC 证明用户属于 tenant”；
- “snapshot 表天然不可篡改”；
- “schema digest 保护业务数据行”；
- “Agent thread/message/Task/Artifact 已落地”；
- “真实服务已经使用 PostgreSQL authority”；
- “当前可以开放真实 IM outbound”。

## 9. 复核路径

- 深度报告：`analysis_report/research/33_postgres_authority_persistence_checkpoint.md`
- 可视化报告：`analysis_report/html/33_postgres_authority_persistence_checkpoint.html`
- 实施计划：`docs/wanwork_im/IMPLEMENTATION_PLAN.md`
- 调研矩阵：`docs/wanwork_im/RESEARCH_TRACEABILITY.md`
- Migration：`apps/im-api/internal/platform/postgres/migrations`
- Store：`apps/im-api/internal/imstore`、`apps/im-api/internal/platform/postgres/imstore`

本文件随 W2 代码边界更新。Git 是 canonical source；Notion 只做稳定检查点的镜像，且必须在写入后
fetch/readback 验证。
