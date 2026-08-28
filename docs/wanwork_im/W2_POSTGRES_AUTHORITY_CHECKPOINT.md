# W2 PostgreSQL Authority Persistence 检查点

> 检查点日期：2026-08-28
>
> 分支：`dev_wanwork_quantum_entanglement`
>
> 证据 HEAD：`cd92ea56493b43889f5165892b40ec36e958d44a`
>
> 判定：`W2 database containment checkpoint / production IM NO-GO`

## 1. 结论先行

W2 当前已经完成 PostgreSQL authority 的第二个阶段性闭环：`0001`–`0005` 建立并冻结 22 张
`wanwork_im` 表和 5 个固定写函数；Conversation、provider conversation binding、membership、access 与
command receipt 的 Go repository 写路径已经只调用登记函数，不再直接执行业务表 `INSERT/UPDATE`。

本机 PostgreSQL 18.6 的真实角色测试进一步证明：runtime login 必须显式 `SET ROLE` 到 NOLOGIN、
NOINHERIT runtime group role；该角色只能读取 9 张当前 repository 必需表并执行 5 个函数，不能直接修改
任意 22 张 authority 表，也没有 `MAINTAIN`、database `TEMPORARY`、schema/database `CREATE` 等权限。
read-only exact access validator 会拒绝角色属性、membership option/grantor、owner、ACL、default privilege、
额外 relation/function 等漂移。

这是数据库 credential containment 和后续 authority resolver 的可复核底座，不是生产服务已经接线。
生产 provisioning、service startup/readiness wiring、authenticated trusted tenant admission、action-time resolver、
recovery、PostgreSQL EventStore/outbox 和真实 provider inbound/outbound 仍未完成。因此真实 IM 流量、真实
outbound 和“生产商用级”仍是 **NO-GO**。

更深的实现证据、提交台账与边界见
`analysis_report/research/34_postgres_function_only_writes_and_exact_access_checkpoint.md`（Topic 34）；前一阶段
schema/repository/UoW 基线见 `analysis_report/research/33_postgres_authority_persistence_checkpoint.md`。

## 2. 当前数据库交付面

### 2.1 `0001`–`0005` migration

| Migration | 当前事实 |
|---|---|
| `0001_authority_roots` | provider realm、tenant、workspace roots |
| `0002_identity_authority` | human principal、identity binding、Actor、tenant membership、provider Actor binding |
| `0003_conversation` | ordinary `direct/group` conversation、provider conversation binding |
| `0004_conversation_authority` | conversation membership、access、tenant command receipt |
| `0005_function_only_writes` | 5 个 exact `SECURITY DEFINER` 写函数；收窄 ordinary authority 的 runtime 写入口 |

当前 `wanwork_im` relation inventory 精确为 22 张普通表，其中 17 张 tenant-scoped 表启用并强制 RLS。
`wanwork_meta` 精确为一张 `schema_migrations` ledger 表，两个受控 schema 都不允许出现额外 table/view/
materialized view/sequence/foreign table；`wanwork_meta` 不允许出现 routine。

Migration runner 继续保持：

- 连续 version、embedded checksum、name 与 exact ledger shape；
- session advisory lock 和每次提交前 `0001..current` 累计 postcondition；
- transaction 内固定 `search_path=pg_catalog`；
- token-aware DDL allowlist；只有 version 5 的五个 exact function DDL 获准，不给后续 migration 自动继承；
- gap/future/checksum/name/schema drift fail closed；
- commit/lock outcome unknown 时隔离连接，并使用有界 rollback/unlock/close 清理。

### 2.2 五个固定写函数

当前唯一登记的写函数是：

1. `write_conversation_revision`
2. `write_provider_conversation_binding_revision`
3. `write_conversation_membership_revision`
4. `write_conversation_access_revision`
5. `write_tenant_command_receipt`

函数 manifest 精确比较 identity arguments、参数名、返回类型、owner、`plpgsql`、`VOLATILE`、`STRICT`、
`PARALLEL UNSAFE`、`LEAKPROOF=false`、`SECURITY DEFINER`、`search_path=pg_catalog` 和 normalized
definition digest；PUBLIC 不得拥有 `EXECUTE`，也不允许同名 overload 或额外 routine。

四个 revision 函数只接受 `0→1` 或 `N→N+1`。duplicate、stale、skipped revision 返回 `false` 且不追加
snapshot；Go repository 将其映射为 `ErrRevisionConflict`。超过 PostgreSQL signed `bigint` 上界的 Go
`uint64` revision 在发 SQL 前被拒绝。无 workspace 时只有 conversation 函数把空串 sentinel 转为 SQL
`NULL`，其他参数不复用该约定。

所有函数把显式 `tenant_id` 与 transaction-local `wanwork.tenant_id` 精确比较；unset、wrong 或 cross-tenant
context 以 SQLSTATE `42501` 失败并保持零写入。这个比较是数据库内的 tenant consistency check，**不是**
authenticated tenant membership 证明。

### 2.3 Function-only repository writes

以下 production repository 写路径已全部改为参数化 `SELECT wanwork_im.write_*`：

- Conversation head + snapshot CAS；
- provider conversation binding head + snapshot CAS；
- conversation membership head + snapshot CAS；
- conversation access head + snapshot CAS；
- tenant command receipt。

回归测试会扫描 repository production source，拒绝重新引入业务 raw `INSERT`/`UPDATE` 或未登记 mutation。
receipt 与业务 mutation 仍在同一 Serializable UoW transaction 内完成；same-command replay、digest conflict、
repository poison、callback lifetime、64 路 exact replay/CAS 和 unknown-commit readback 语义保持。

## 3. Exact access validator 与真实角色 fixture

### 3.1 冻结的角色图

环境中的 database owner/provisioner、三个 group role 和 login role 必须全部显式列入 manifest：

```text
database owner / provisioner
  ├─ grants owner role to migrator role
  ├─ grants migrator role to listed migration login
  └─ grants runtime role to listed runtime login

listed migration login --SET ROLE--> owner
listed runtime login   --SET ROLE--> runtime
```

三个 group role 必须是 `NOLOGIN NOINHERIT`；环境 login 必须是 `LOGIN NOINHERIT`。这些 group/login
角色都必须是 `NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS`、connection limit `-1`、
无 role/database settings、无 expiry；external database owner/provisioner 的 cluster attribute 不在这条约束内。
每一条 membership 必须由 database owner/provisioner 授予，且精确为
`ADMIN=false, INHERIT=false, SET=true`。

`migrator` 是 capability indirection：它让受控 migration login 获得显式 `SET ROLE owner` 的路径；它不应
被表述为不可绕过的“两跳审批”。

### 3.2 精确 owner、inventory 与 ACL

`ValidateAuthorityAccess` 必须运行在 manifest 列出的 migration login connection 上，并且该 session 已
`SET ROLE owner`。validator 在 read-only Repeatable Read transaction 内只读 catalog，任何不精确都返回
`ErrAuthorityAccessDrift`，不自动修复。

它冻结：

- 当前 database owner 必须是 manifest 的 external provisioner；
- `wanwork_meta`、`wanwork_im`、23 张受控表（22 张 authority 表 + migration ledger）和 5 个函数 owner；
- PUBLIC 无 database `CONNECT/CREATE/TEMPORARY`；
- owner group role 只获 database `CREATE`；
- migration/runtime login 只直接获 database `CONNECT`；login 不直接获 schema/table/function 权限；
- runtime group role 只获 `wanwork_im` schema `USAGE`；
- runtime 只获 9 张当前 repository 表的 `SELECT` 和 5 个 exact function 的 `EXECUTE`；
- 无任何 non-owner column ACL、grant option、额外 relation/function 或 metadata function；
- owner 的全局 function default ACL 精确撤销 PUBLIC `EXECUTE`。

九张 runtime read 表是 conversation、provider conversation binding、membership、access 的 head/snapshot，
加 `tenant_command_receipts`。identity roots 等其余 13 张 authority 表没有被“为了方便”加入 SELECT grant。

### 3.3 fixture 的证据边界

Migration integration fixture 会创建真实临时 database、database owner、NOLOGIN group roles、LOGIN migration/
runtime roles、exact membership/owner/grant/default privilege，并通过两种真实连接验证：

- migration login 未 `SET ROLE owner` 时不能验证，切换后能重复 `Apply` 并验证 manifest；
- 未列入 manifest 的 admin session 即使切到 owner 也被拒绝；
- runtime login 默认不继承权限，只能切到 exact runtime role，不能切 owner/migrator；
- runtime login 切到 runtime 后能执行 exact read/function surface。

imstore integration fixture 另行用真实 LOGIN + NOINHERIT group role 建 pool，并在 `AfterConnect` 显式
`SET ROLE`，因此 repository/UoW 测试不是以 owner/admin 凭据伪装 runtime。out-of-band receipt writer 也显式
切到同一个 runtime role。

这些 fixture 是可复现测试 provisioning，**不是** production IaC/DBA provisioning 或 service composition。

## 4. PostgreSQL 18.6 测试门禁

本阶段在本机 PostgreSQL 18.6 上通过 migration 与 imstore 的 normal/race，以及对应 `go vet`：

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

`LOCAL_TEST_POSTGRES_URL` 只在本地 shell 注入，不写入 Git、报告或远端文档。

关键负向证据包括：

- runtime 对全部 22 张 authority 表的 `INSERT/UPDATE/DELETE/TRUNCATE` 均被 SQLSTATE `42501` 拒绝；
- runtime 对全部 22 张表都没有 PostgreSQL 18 `MAINTAIN`；
- 对全部 22 张表发出 `ANALYZE` 时，必须是 `42501`，或 PostgreSQL 以 permission warning 明确跳过；测试
  不把“命令未报 fatal error”误判为已经执行维护；
- runtime 不能创建 schema/table/function/temp table，不能改 policy，不能 `SET ROLE owner/migrator`；
- unset/wrong/cross-tenant、duplicate/stale/skipped revision 均零写入；
- function owner/body digest/security/volatility/search path/PUBLIC ACL/overload 漂移被 migration postcondition 拒绝；
- role LOGIN/INHERIT/settings、membership ADMIN/INHERIT/SET/grantor/duplicate grantor、PUBLIC CONNECT、
  database CREATE/TEMP、schema/table/function owner 和直接 login privilege 漂移被 access validator 拒绝；
- extra table/view/sequence/function、metadata routine、column ACL、raw write/MAINTAIN grant、extra SELECT、
  missing EXECUTE、global function default ACL 漂移被拒绝；
- fresh/repeat migration、malicious historical RLS weakening rollback、hostile search path、双 migrator
  serialization、normal/race、repeat `Apply` 保持通过。

## 5. 已冻结但不能越界解释的不变量

- runtime function-only write 证明同一 runtime credential 不能用 raw table DML 绕过 revision/snapshot seam；
  它不证明其他 cluster superuser、database owner 或错误部署的外部 credential 不会绕过。
- transaction-local tenant GUC 绑定函数参数，但当前 tenant 值仍由应用调用链传入；它不是 Clerk verified claim。
- six permission booleans 仍是 immutable projection，不是已经部署的 action-time PEP。
- membership head 存在不等于 current membership active；resolver 必须组合 current membership 与 permission。
- receipt 是 command dedupe/digest primitive，不是 typed-result store、provider ACK、审计签名或 exactly-once。
- provider binding 是 local routing metadata，不证明 provider object 存在、消息送达或 callback 可信。
- ordinary `direct/group` 已持久化；`agent_thread`、message、Task、Artifact、Acceptance 尚未落地。

## 6. Go / No-Go 判定

| 使用场景 | 判定 | 原因 |
|---|---|---|
| 继续 schema/repository 本地开发 | GO | function-only write、exact manifest 与真实角色 fixture 已形成稳定底座。 |
| 作为 trusted tenant/resolver 的数据库基础 | GO with gates | 下一步必须把认证 admission 与 action-time resolver 接到每个 effect。 |
| fake IM vertical slice | GO with explicit fake labels | 仅可用于 contract/integration；不得宣称 provider 或生产 authority 已完成。 |
| production provisioning 或 service startup | NO-GO | 现在只有 validator 和测试 provisioner；无 production IaC/secret/readiness wiring。 |
| 融云 sandbox inbound/outbound | NO-GO | callback auth、mapping resolver、dedupe/resume、outbox/reconcile 与 provider contract 未完成。 |
| 真实 IM 流量、真实 outbound | NO-GO | trusted tenant、action-time authorization、service composition、recovery 均未关闭。 |
| 标记生产商用级 | NO-GO | recovery、EventStore/outbox、observability、provider 和业务对象闭环缺失。 |

## 7. 下一批生产接入前 P0

1. **Production provisioning / credential split**：用 DBA/IaC 创建 database owner、group/login roles、owner
   transfer、grants/default privileges；停止服务并关闭旧 session 后再撤销 PUBLIC/旧 credential。fixture SQL
   不能直接当生产脚本。
2. **Service startup/readiness wiring**：migration pool 只用列出的 migration login 并显式 `SET ROLE owner`；
   application pool 只用 runtime login 并显式 `SET ROLE runtime`；`Apply` 与
   `ValidateAuthorityAccess` 任一失败时 readiness=false。当前服务 composition 尚未引用这些组件。
3. **Trusted authenticated tenant context**：verified identity claim → realm-scoped identity binding → active
   principal/membership/Actor → path tenant consistency → trusted RequestContext → TenantUnitOfWork。客户端
   body/header/GUC 不能覆盖 claim。
4. **Active authority resolver / PEP**：ordinary read/send/manage 在每次 effect 前组合 tenant、Actor、
   Conversation、active membership、current revision 和 explicit permission；invoke/publish 继续叠加 installation/
   release/mandate/capability/budget/Artifact/Acceptance。
5. **Recovery/compatibility**：dump/restore、database/process restart、kill-9、old binary/future schema、role/
   function/ACL readback、unknown commit operator state。
6. **PostgreSQL EventStore/outbox/checkpoint**：transaction-bound stream append、global position、outbox、
   projection checkpoint、backfill+live、crash/reopen/restore。memory fake 与 receipt 不得替代该层。
7. **Provider boundary**：verified callback、provider mapping resolver、inbound dedupe/resume、outbound outbox/
   reconcile、provider ACK 与审计证据；在这些完成前不开放真实 provider side effect。

## 8. 禁止性宣称

- “production role provisioning 已交付”；
- “真实服务已经使用 migration/runtime 分权连接”；
- “生产级多租户已经完成”；
- “tenant GUC 证明用户属于 tenant”；
- “六个权限位已经控制真实动作”；
- “database receipt 实现 exactly-once 或 provider ACK”；
- “provider binding 证明融云群存在或消息送达”；
- “schema/function digest 保护所有业务数据行”；
- “Agent thread/message/Task/Artifact/EventStore/outbox 已落地”；
- “当前可以开放真实 IM inbound/outbound”；
- “当前已经生产商用级”。

## 9. 复核路径

- Topic 34 深度报告：`analysis_report/research/34_postgres_function_only_writes_and_exact_access_checkpoint.md`
- Topic 34 HTML：`analysis_report/html/34_postgres_function_only_writes_and_exact_access_checkpoint.html`
- Topic 33 前置检查点：`analysis_report/research/33_postgres_authority_persistence_checkpoint.md`
- Function-only 实施记录：`docs/wanwork_im/W2_POSTGRES_FUNCTION_WRITE_PLAN.md`
- Migration / exact validator：`apps/im-api/internal/platform/postgres/migrations`
- Repository / runtime fixture：`apps/im-api/internal/platform/postgres/imstore`
- 生产接入总计划：`docs/wanwork_im/IMPLEMENTATION_PLAN.md`

本文件随 W2 代码边界更新。Git 是 canonical source；外部知识库只是稳定检查点镜像，不能替代本地测试、
commit 与 readback 证据。
