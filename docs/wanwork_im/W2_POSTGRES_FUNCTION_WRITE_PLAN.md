# W2 PostgreSQL `0005`：Function-only Writes 与 Exact Access 实施记录

状态：历史阶段实现记录，2026-08-28；当前 W2 已由
[`W2_POSTGRES_RUNTIME_CHECKPOINT.md`](W2_POSTGRES_RUNTIME_CHECKPOINT.md) 接续

分支：`dev_wanwork_quantum_entanglement`，不合并 `main`

本检查点证据 HEAD：`cd92ea56493b43889f5165892b40ec36e958d44a`

## 1. 本检查点结论

最初冻结的 `0005` 目标已经在当前代码中实现并通过 PostgreSQL 18.6 真实测试：

```text
typed Go request
  -> Serializable TenantUnitOfWork
  -> tx-bound repository
  -> one of five fixed SECURITY DEFINER functions
  -> tenant argument == transaction-local tenant context
  -> successor-only head CAS + append-only snapshot
  -> same-transaction command receipt
```

Go repository 的 ordinary conversation authority 写入已经是 function-only。exact access validator 又把 database
owner、三个 group role、显式 login、membership grantor/options、两个 schema、22 张 authority 表、migration
ledger、5 个函数、ACL 和 owner default function privilege 冻结成可读回的 catalog contract。真实临时 migration/
runtime login fixture 证明该 contract 能运行，而不仅是一个 Go struct。

这个阶段关闭了“runtime credential 可用 raw SQL 绕过 repository revision/snapshot contract”的问题，但只在
当前固定函数和精确测试角色边界内成立。production provisioning、真实 service pool/readiness、authenticated
tenant admission、action-time authorization、recovery、EventStore/outbox 与 provider 仍未实现，整体保持
**production IM NO-GO**。

完整深度证据见
`analysis_report/research/34_postgres_function_only_writes_and_exact_access_checkpoint.md`（Topic 34）。

## 2. 计划到实现的完成矩阵

| 冻结目标 | 当前事实 | 状态 |
|---|---|---|
| version 5 只允许登记 function DDL | lexer/policy 只对 `0005/function_only_writes` 放行 exact statement shape | 完成 |
| 五个 fixed function | `0005` up/down、checksum、累计 postcondition、definition digest 已激活 | 完成 |
| successor-only revision | `0→1`、`N→N+1`；stale/duplicate/skipped 返回 false、零 snapshot | 完成 |
| tenant consistency | 五函数都比较参数与 transaction-local `wanwork.tenant_id`；unset/wrong/cross-tenant 拒绝 | 完成 |
| repository function-only writes | conversation、provider binding、membership、access、receipt 全部调用函数 | 完成 |
| runtime raw mutation containment | runtime 对全部 22 张 authority 表无 INSERT/UPDATE/DELETE/TRUNCATE | 完成 |
| exact role/ACL catalog validation | owner/roles/logins/memberships/ACL/default ACL/object inventory fail closed | 完成 |
| 真实 login fixture | migration login 与 runtime login 经 NOINHERIT group role、显式 SET ROLE 运行 | 完成 |
| production IaC/DBA provisioning | 当前只有 integration fixture，没有生产脚本、secret/credential rollout | 未完成 |
| service startup/readiness composition | API 启动尚未接 `Apply` + access validator + separate pools | 未完成 |
| trusted tenant / action resolver | GUC 仍不是 verified claim；permission projection 尚未成为真实 effect PEP | 未完成 |

## 3. 固定函数面

`0005` 只登记下列五个函数，不提供通用 SQL、动态表名、JSON patch 或任意 procedure：

| 函数 | Exact identity arguments | 返回值 |
|---|---|---|
| `wanwork_im.write_conversation_revision` | `text, text, bigint, bigint, text, text, text` | `boolean` |
| `wanwork_im.write_provider_conversation_binding_revision` | `text, text, text, text, bigint, bigint, text, text` | `boolean` |
| `wanwork_im.write_conversation_membership_revision` | `text, text, text, bigint, bigint, text, text` | `boolean` |
| `wanwork_im.write_conversation_access_revision` | `text, text, text, bigint, bigint, boolean, boolean, boolean, boolean, boolean, boolean` | `boolean` |
| `wanwork_im.write_tenant_command_receipt` | `text, text, text, text, text` | `timestamptz` |

参数名和顺序也已进入 manifest，而不只是比较 type signature：

```text
write_conversation_revision(
  p_tenant_id, p_conversation_id, p_expected_revision, p_next_revision,
  p_workspace_id, p_conversation_type, p_status
)
write_provider_conversation_binding_revision(
  p_tenant_id, p_provider, p_realm_id, p_provider_conversation_id,
  p_expected_revision, p_next_revision, p_conversation_id, p_status
)
write_conversation_membership_revision(
  p_tenant_id, p_conversation_id, p_actor_id,
  p_expected_revision, p_next_revision, p_role, p_status
)
write_conversation_access_revision(
  p_tenant_id, p_conversation_id, p_actor_id, p_expected_revision, p_next_revision,
  p_can_read, p_can_send_message, p_can_manage_members, p_can_manage_conversation,
  p_can_invoke_agent, p_can_publish_artifact_reference
)
write_tenant_command_receipt(
  p_tenant_id, p_command_kind, p_idempotency_key, p_request_sha256, p_result_sha256
)
```

所有函数精确为 `LANGUAGE plpgsql`、`VOLATILE`、`STRICT`、`PARALLEL UNSAFE`、`LEAKPROOF=false`、
`SECURITY DEFINER`、fixed `search_path=pg_catalog`，并完全限定引用 `wanwork_im` 对象。manifest 比较 exact owner、
arguments、return type、normalized definition digest 和无额外 overload。

PUBLIC `EXECUTE` 被逐函数撤销；owner 还必须具备全局（不带 `IN SCHEMA`）default privilege：

```sql
ALTER DEFAULT PRIVILEGES FOR ROLE <owner>
REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
```

exact access validator 会拒绝该 default ACL 缺失、scope 漂移或多余 default ACL。

## 4. Revision、nullable 与 tenant 合同

四个 revision 函数固定执行：

- `expected_revision=0` 时只允许创建 revision 1；
- `expected_revision>0` 时只允许 `next_revision=expected_revision+1`；
- CAS 失败返回 `false`，repository 映射为 `ErrRevisionConflict`；
- CAS 失败不追加 snapshot；head 与 snapshot 在同一 function statement/outer transaction 中提交或回滚；
- 只使用参数化静态 SQL，不使用动态 `EXECUTE`；
- tenant 参数必须精确等于 `current_setting('wanwork.tenant_id', true)`；缺失或冲突固定 SQLSTATE `42501`。

`conversation.workspace_id` 是当前唯一可空业务值。Go 无 workspace 时传空字符串，函数只在 snapshot insert
执行 `NULLIF(p_workspace_id, '')`；其他参数不以空串表示 NULL。

Go revision 是 `uint64`，PostgreSQL revision 是 signed `bigint`。repository 在调用 pgx 前拒绝
`expected_revision` 或 `next_revision` 大于 `9223372036854775807`，避免把编码错误混同普通 store outage。

receipt 函数只持久化当前 command key/request/result digest 和时间。它仍不是 typed result replay、provider
ACK、审计签名或 exactly-once 证明。

## 5. Repository 写入口已经收窄

五条写路径逐一改造并分别提交：

1. Conversation repository 调 `write_conversation_revision`；
2. provider conversation binding repository 调 `write_provider_conversation_binding_revision`；
3. membership repository 调 `write_conversation_membership_revision`；
4. access repository 调 `write_conversation_access_revision`；
5. UoW receipt 调 `write_tenant_command_receipt`。

production repository 文件中的 raw business `INSERT`/`UPDATE` 已清除，并有源码级 regression test 防止回流。
runtime 读取只保留真实使用的 9 张表：四组 head/snapshot 与 receipt；其余 13 张 authority 表不授予 SELECT。

imstore 集成测试保留并重新验证：

- exact replay callback 只执行一次；same key/different request digest 冲突；
- operation error、ignored repository error、panic 均回滚且 receipt 不残留；
- callback 返回或 panic 后 escaped repository 失效；
- 64 路相同 command 只有一个 fresh callback；64 路相同 CAS 只有一个 winner；
- synthetic commit ACK loss 用新连接 readback 收敛，不盲目重放 mutation；
- function write 与 receipt 继续共享外层 transaction。

## 6. Exact authority access contract

### 6.1 角色与 membership

manifest 必须显式列出：

- external database owner/provisioner；
- NOLOGIN `owner` group role；
- NOLOGIN `migrator` group role；
- NOLOGIN `runtime` group role；
- 至少一个 migration LOGIN role；
- 至少一个 runtime LOGIN role。

三个 group role 和所有环境 login 都必须 NOINHERIT、NOSUPERUSER、NOCREATEROLE、NOCREATEDB、
NOREPLICATION、NOBYPASSRLS，无 scoped settings、无 expiry、connection limit `-1`。external database
owner/provisioner 的 cluster attribute 不在这条约束内。唯一允许的 membership 图是：

```text
owner role    -> migrator role
migrator role -> each listed migration login
runtime role  -> each listed runtime login
```

箭头表示“左侧 role 被授予右侧 member”。每条记录的 grantor 必须精确为 database owner/provisioner，option
必须是 `ADMIN=false, INHERIT=false, SET=true`。validator 比较原始 membership row，不会 group 后漏掉不同
grantor 的重复记录。`migrator` 只是 capability indirection，不是组织审批系统。

### 6.2 Database、schema、relation、function ACL

exact contract 是：

- database owner 精确为 external provisioner；PUBLIC 无 CONNECT/CREATE/TEMPORARY；
- owner group role 只获 database CREATE；列出的 login 只直接获 CONNECT；
- 两个 schema、22 张 authority 表、migration ledger、5 个函数 owner 精确为 owner group role；
- runtime group role只获 `wanwork_im` USAGE、9 张表 SELECT、5 个函数 EXECUTE；
- environment login 不直接获 schema/table/function ACL；
- 任意 non-owner column ACL、grant option、额外 object 或 metadata routine 都被拒绝；
- owner 全局 function default ACL 必须精确撤销 PUBLIC EXECUTE。

`ValidateAuthorityAccess` 是 read-only/fail-closed validator。调用 session 必须同时满足：`session_user` 在显式
migration login 列表，且 `current_user == owner`。因此 unlisted admin 即使能切 owner、listed migration login
忘记切 owner，两者都会失败。validator 不创建角色、不转 owner、不授予或修复权限。

## 7. 真实 PostgreSQL fixture 与负测

### 7.1 真实 connection 路径

access integration fixture 在临时 database 内创建完整 provisioner/group/login 图并转移 owner，再以 migration
login 新连接执行 `SET ROLE owner`、重复 `Apply` 和 exact validation。另一个 runtime login 新连接先证明
NOINHERIT，再显式 `SET ROLE runtime` 检查功能面。

imstore fixture 也不再使用 owner pool：它创建独立 runtime LOGIN 与 NOLOGIN group role，pool
`AfterConnect` 显式选择 group role，所有 UoW/repository 行为从该连接执行；out-of-band receipt writer 同样
显式使用 runtime role。

### 7.2 22 表 mutation、MAINTAIN 与 ANALYZE

真实 runtime role 对每一张 authority 表都执行四类攻击：

```text
22 × (INSERT, UPDATE, DELETE, TRUNCATE) -> SQLSTATE 42501
```

PostgreSQL 18 新增/使用的 `MAINTAIN` privilege 被独立检查：runtime 对 22 张表全部为 false。每张表还发出
`ANALYZE`；验收要求它要么返回 SQLSTATE `42501`，要么产生明确 permission warning 并跳过，不能在缺少
MAINTAIN 的情况下静默完成维护。

此外 runtime 不能 CREATE schema/table/function/temp table、ALTER POLICY 或 SET ROLE owner/migrator；但能
读取登记的 9 张表并执行 5 个 exact function。

### 7.3 Role / ACL drift injection

当前 integration suite 会注入、验证拒绝、修复后再验证以下漂移：

- unsafe LOGIN/INHERIT 与 role/database scoped settings；
- membership ADMIN/INHERIT/SET option、错误或重复 grantor；
- PUBLIC database CONNECT、runtime database CREATE/TEMPORARY、login direct database CREATE；
- schema/table/function owner；
- raw INSERT 或 MAINTAIN grant、extra SELECT、column UPDATE grant；
- direct login schema/table/function privilege；
- PUBLIC function EXECUTE、missing runtime EXECUTE、function default ACL；
- authority/metadata schema 的 extra table/function；
- function body digest、owner、SECURITY INVOKER、volatility、search path、额外 overload。

修复后 validator 与重复 `Apply` 必须恢复通过，证明它不是只在 fresh schema 上工作的单次检查。

## 8. 当前测试门禁

本阶段在本机 PostgreSQL 18.6 上通过：

```bash
cd apps/im-api

WANWORK_TEST_POSTGRES_ADMIN_URL="$LOCAL_TEST_POSTGRES_URL" \
  go test -count=1 ./internal/platform/postgres/migrations
WANWORK_TEST_POSTGRES_ADMIN_URL="$LOCAL_TEST_POSTGRES_URL" \
  go test -race -count=1 ./internal/platform/postgres/migrations
go vet ./internal/platform/postgres/migrations

WANWORK_TEST_POSTGRES_ADMIN_URL="$LOCAL_TEST_POSTGRES_URL" \
  go test -count=1 ./internal/platform/postgres/imstore
WANWORK_TEST_POSTGRES_ADMIN_URL="$LOCAL_TEST_POSTGRES_URL" \
  go test -race -count=1 ./internal/platform/postgres/imstore
go vet ./internal/platform/postgres/imstore
```

本地管理 URL 只通过 shell 环境变量注入，不进入文档示例值、Git 或外部同步内容。

## 9. 一级调研约束的落点

本阶段继续把以下两层目录作为一级证据，而不是只参考二次摘要：

- `automation/2026/05_08/1/2output`
- `automation/2026/05_08/1/2output/more`

| 一级研究结论 | 当前落点 | 仍未关闭 |
|---|---|---|
| tenant path/参数不能覆盖 authenticated claim | 函数参数与 transaction-local context 冲突 fail closed | GUC 来源尚未绑定 verified claim |
| credential/context secret/过宽 scope 是核心风险 | runtime 仅 9 SELECT + 5 EXECUTE，无 raw mutation/MAINTAIN | production secret、credential rollout 未完成 |
| enforcement 必须在执行侧 | PostgreSQL role/ACL/function 成为独立执行侧约束 | action-time business PEP 未完成 |
| schema/配置存在不等于 runtime wiring | repository 与真实 runtime login 已实际执行函数 | production service composition/readiness 未完成 |
| participant/membership/action assignment 分域 | database role 不被冒充为 conversation/business authority | active resolver、Agent mandate/budget 未完成 |

## 10. 仍然 NO-GO 的生产缺口

### 10.1 Production provisioning 与 service wiring

目前创建角色、转 owner、设置 grant/default privilege 的代码只存在于 integration fixture。需要独立 DBA/IaC
流程、secret reference、旧 connection drain/cutover、migration/runtime credential 分离、startup gate 和
readiness fail closed。PUBLIC CONNECT 撤销不会终止已存在的连接，真实 cutover 必须停服并关闭旧 session。

### 10.2 Trusted tenant 与 action-time resolver

transaction-local GUC 只证明“调用参数与本 transaction 记录值一致”，不证明该值来自 Clerk/OIDC verified
claim。必须完成 verified identity → active principal/membership/Actor → path tenant consistency → trusted
RequestContext，并在每个 read/send/manage/invoke/publish effect 前使用 current authority resolver。

### 10.3 Recovery、event/outbox 与 provider

尚未交付 dump/restore + role/function/ACL readback、restart/kill-9、old-binary/future-schema compatibility、
PostgreSQL EventStore/global position/outbox/checkpoint/backfill、verified provider callback、inbound resume/dedupe、
outbound reconcile/ACK。现有 memory event fake 与 command receipt 不能代替这些能力。

## 11. 后续执行顺序

1. 生产 role/database provisioning contract 与安全 cutover runbook；
2. migration/runtime 两套真实配置、pool composition、startup `Apply` + exact validator + readiness；
3. authenticated trusted tenant RequestContext；
4. active authority resolver / action-time PEP；
5. dump/restore、restart/kill-9、compatibility 与 operator-visible unknown state；
6. PostgreSQL EventStore/outbox/checkpoint；
7. verified provider inbound/outbound、dedupe/resume/reconcile；
8. 再扩 Agent thread/message/Task/Artifact/Acceptance 等产品对象。

## 12. 明确不宣称

- 不是 production IM 或 production multitenancy；
- integration role fixture 不是 production provisioning；
- repository 使用 runtime test login 不等于真实 API service 已完成 pool wiring；
- tenant GUC 不是 authenticated membership；
- database function ACL 不是 action-time business authorization；
- access booleans 不是 invoke/publish permission 的完整答案；
- receipt 不是 provider ACK、完整 replay、不可抵赖证据或 exactly-once；
- provider binding 不证明 provider object 或消息送达；
- `agent_thread`、message、Task、Artifact、Acceptance、production EventStore/outbox 尚未交付；
- 当前不能接真实 IM 流量或开放 outbound；
- 当前不能标记生产商用级。

## 13. 复核入口

- 深度报告：`analysis_report/research/34_postgres_function_only_writes_and_exact_access_checkpoint.md`
- HTML：`analysis_report/html/34_postgres_function_only_writes_and_exact_access_checkpoint.html`
- W2 当前检查点：`docs/wanwork_im/W2_POSTGRES_RUNTIME_CHECKPOINT.md`
- Function migration/postcondition：`apps/im-api/internal/platform/postgres/migrations`
- Repository/UoW/真实 runtime fixture：`apps/im-api/internal/platform/postgres/imstore`

Git 是 canonical source。Topic 34 是历史检查点；当前稳定事实由 Topic 35、当前 W2 工程入口和后续已提交、
已测试的 Git 证据表达。知识库只镜像已经完成阶段末同步与回读的批次，不能反向覆盖更新的本地事实。
