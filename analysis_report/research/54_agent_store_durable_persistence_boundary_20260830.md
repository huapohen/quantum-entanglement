# Agent Store 从内存 synthetic 迁移到 PostgreSQL durable 的最小边界审计

> 日期：2026-08-30（Asia/Shanghai）
> 状态：设计审计 / `local_pending`，不是已交付的 PostgreSQL 实现
> 代码观察基线：`dev_wanwork_quantum_entanglement` 在本报告开始前的 `79dbbd0`（后续提交不改变本报告对“当前仍未持久化”的结论）
> 范围：Agent Store 的 definition、release、Trust Passport、installation、capability grant、安装/撤权幂等和 action-time policy
> 明确不做：Notion/语雀写入，飞书/企微消息，真实 provider 调用，业务代码修改

## 结论先行

当前 Agent Store 是一个可从浏览器验收的内存 synthetic vertical slice。它已经把“发布者声明、审阅
证据、租户安装决定、运行时能力”分开，并在动作时重新检查 Trust Passport；但 `Service` 仍把目录、
安装、幂等请求、会话成员和 Workboard 放在单进程 map 中。进程重启、第二个 API 实例、并发请求、
provider ACK 丢失或数据库切换都会丢失或分裂状态。因此，当前不能把 `installationStatus=active`、
`grantedCapabilities` 或 fake provider receipt 当作生产 authority。

迁移的最小安全边界不是“把几个 map 序列化成 JSON”，而是同时交付以下四层：

1. **不可变 catalog/policy 记录**：definition、release、能力声明、prohibition、data route、
   attestation 和 Trust Passport 的版本链；
2. **租户安装 authority**：installation head/snapshot、实际授予能力、绑定 data route、Agent actor
   和生命周期状态；
3. **命令与外部 effect 的 durable receipt**：install/offboard 的 request digest、result、状态、
   provider effect 及 commit-unknown reconcile；
4. **每个动作的实时解析与单一事务边界**：可信 tenant/principal/workspace context、当前 Passport、
   installation CAS、ACL/mandate/capability 检查都不能依赖发现页或缓存。

最小迁移顺序应是“schema → RLS/function-only authority → repository/UoW → read projection →
provider outbox/reconcile → 关闭内存 fallback”。数据库不可用时应 fail closed；不能为了保留演示
体验而在 durable 路径失败后偷偷回退到内存写入，否则会产生两个互不一致的 authority。

## 1. 证据与当前状态

### 1.1 当前代码事实

| 证据 | 当前行为 | 迁移含义 |
|---|---|---|
| `apps/im-api/internal/localdemo/service.go` 的 `Service` | `agentCatalog`、`agentInstallRequests`、`agentOffboardRequests`、`installation`、`passport`、conversation/task/artifact map 均为进程内字段 | 必须拆成 tenant-bound repository；不能直接把 `Service.mu` 当跨进程锁 |
| `NewWithRuntime` | 启动时构造 v0版研究 Agent、可安装的 v0版规划 Agent，且调用 fake provider provision/group create | 启动重建不是 durable recovery；生产启动不得重复创建 provider effect |
| `apps/im-api/internal/localdemo/agents.go` | `ListAgents` 返回 discovery projection；`InstallAgent` 在动作锁内检查 definition/release/passport，并将安装授予能力保存进 `InstallationSnapshot` | discovery 与 action-time resolver 必须明确分层；grant set 要以规范化后的数据库行恢复 |
| `apps/im-api/internal/localdemo/agents.go` | `grantedCapabilities=nil` 保持完整 reviewed requested 集合；显式列表必须是 Trust Passport requested 子集；非法扩权 fail closed；幂等 digest 绑定 release digest 与 grant set | 数据库函数必须重复验证，不能只相信 Go caller；request digest 必须包含规范化集合 |
| `apps/im-api/internal/localdemo/agents_offboard.go` | provider 成员移除、用户撤权、conversation projection 清理后才把 installation 转为 `offboarded`；有 retain/archive/delete | offboard 必须有持久 command/effect 状态；部分成功、ACK 丢失和重试不能靠 map 推断 |
| `apps/im-api/internal/agentstore/catalog.go` | `ReleaseSnapshot` 规范化 capability/prohibition/data route；`TrustPassport.Allows` 只允许 active Passport 的 requested 且非 prohibited 能力 | 规范化与跨表 subset 约束要在 repository 和 `SECURITY DEFINER` 函数双重执行 |
| `apps/im-api/internal/agentstore/installation.go` | `NewInstallationSnapshot` 要求 Agent actor、tenant、workspace、Passport、grant set、bound route 和 revision 一致；offboarded 是终态 | snapshot/head/CAS 和历史不可变性必须在 PostgreSQL 中可审计 |
| `apps/im-api/internal/agentstore/installation.go` | `CatalogRepository`、`InstallationRepository` 只有值合同，没有 PostgreSQL 实现 | 不能把接口存在误报为 durable 完成；需要明确 repository/UoW wiring |
| `apps/im-api/internal/platform/postgres/imstore/uow.go` | 已有 attested runtime pool、tenant transaction、serializable command receipt、advisory lock、commit-unknown readback | Agent Store 应复用该 UoW 语义，不应另造绕过 tenant GUC 的裸连接 |

### 1.2 当前 PostgreSQL 基线

现有 migration catalog 在代码中列到 `0001..0010`，其中已经有：

- `wanwork_im.tenants`、`workspaces`、identity/actor authority；
- direct/group conversation、membership/access 和 provider binding；
- `tenant_command_receipts`、event log、projection checkpoint、native IM inbox；
- `FORCE ROW LEVEL SECURITY`、`wanwork.tenant_id` transaction setting、固定
  `SECURITY DEFINER` writer 和 runtime/owner exact access 检查；
- migration checksum/postcondition 和 PostgreSQL 18.6 集成门禁。

这些是 Agent Store 的底座，不是 Agent Store 已经落库的证据。当前没有
`agent_definition*`、`agent_release*`、`agent_passport*`、`agent_installation*` 的 migration 或
`platform/postgres/agentstore` repository；本审计提出的 `0011+` 仅是建议编号，不能写成现有代码。

## 2. authority 分层与不变量

### 2.1 四类对象必须保持分离

```text
Publisher claim
    │ 只声明身份、版本、能力、路线
    ▼
Release + Trust Passport
    │ 平台审阅、有效期、撤销/隔离
    ▼
Tenant installation decision
    │ 选择 workspace/actor，授予 requested 的子集
    ▼
Action-time resolver / PEP
    │ 当前 tenant + principal + membership + installation + grant + Passport
    ▼
Provider effect / Agent invocation
```

- **Definition** 是谁声明了 Agent 以及显示信息，不授予运行权。
- **Release** 冻结 executable/artifact/manifest/persona digest、版本、requested capability、
  prohibition、data route 和 isolation，不等于已安装。
- **Trust Passport** 把 definition/release 与完整审阅声明绑定；它是治理证据，不是 bearer token。
- **Installation** 是某 tenant/workspace 对某 release 的实际决定，保存 Agent actor、安装者、实际
  grant set、bound route 和状态；它不自动获得群成员或 provider 权限。
- **Membership/ACL** 仍属于 IM authority；安装不应隐式写入任意群。`@Agent` 还要在当前 conversation
  上做 ACL、installation、Passport 和 capability 解析。
- **Provider binding/effect** 只是外部副作用和 readback 证据。provider 成功不能反向证明平台状态。

### 2.2 必须不变量

| 编号 | 不变量 | 失败时的公开行为 |
|---|---|---|
| I-01 | definition/release/passport/installation 的 tenant 一致，workspace 必须属于 tenant | `forbidden` 或 `integrity`；不得跨租户泄露存在性 |
| I-02 | 当前 definition=`active`、release=`published`、Passport=`active`，且所有 attestation 在 `now` 有效 | install/invoke/offboard preparation fail closed |
| I-03 | `grantedCapabilities ⊆ requestedCapabilities \ prohibitions`；能力规范化、排序、去重 | 越权返回 forbidden；重复/非法返回 validation；数据库函数也拒绝 |
| I-04 | installation 的 `release_id/version/digest/passport_revision` 与当前绑定一致；grant/route 属于同一 snapshot revision | integrity/revision conflict；不能用新 release 解释旧安装 |
| I-05 | installation head 的 current revision 必须有对应 snapshot；历史 snapshot 追加不可更新/删除 | integrity；人工修复必须通过受控迁移，不可直接 UPDATE |
| I-06 | 同一 `(tenant,workspace,definition,release)` 至多一个 active 安装；不同 release 的升级必须显式 command | unique conflict；不可用重复 POST 产生第二个 Agent actor |
| I-07 | 同一 command key 只接受一个 canonical request digest；相同 digest replay，其他 digest conflict | HTTP 200 envelope 中业务冲突（当前 API 约定）或内部 command conflict |
| I-08 | install 只有在 provider effect 明确 committed/replayed 并完成平台 CAS 后才进入 active；unknown 保持 pending/reconcile | dependency unavailable/unknown；绝不先标 active |
| I-09 | offboarded 是终态；成员/access 清理、provider revoke、invocation/credential lease 处理各有 receipt | 未完成任一步保持 pending/revoking，不伪造成功 |
| I-10 | requester 的可信 principal、tenant membership、workspace/ACL 必须从 action-time authority 解析 | 未授权请求 forbidden；客户端提交的 actor/tenant 字段不具权威 |
| I-11 | provider ext_info 不含 capability、tenant、workspace、Passport、credential、ACL 或 route authority | schema/secret canary 失败即停止 provider effect |
| I-12 | 数据库故障、RLS 漂移、function digest 漂移、readback 不确定都 fail closed | readiness/route barrier 不放行写操作 |

## 3. 最小 PostgreSQL schema 提案

### 3.1 总体原则

建议新增连续 checksummed migrations（暂称 `0011_agent_store_catalog`、`0012_agent_store_installation`、
`0013_agent_store_commands`，实际编号以 migration owner 冻结为准），不要修改 `0001..0010`。所有表放在
`wanwork_im`，使用 `text COLLATE "C"` + 明确 check；时间使用 `timestamptz`；ID、digest、status、
revision 都拒绝空值。不要用一个可任意写入的 `jsonb` blob 代替 authority 字段；只有非权威诊断元数据
才可存 JSON，并且必须有 digest/大小/敏感字段限制。

每个 head/snapshot 对遵循现有 conversation/identity 模式：

```text
head(current_revision) ──DEFERRABLE FK──> snapshot(revision)
snapshot ──只追加──> child capabilities/routes/attestations
```

`current_revision` 更新只能由固定 writer function 执行，且要求 expected revision；普通 runtime role
不能直接 `INSERT/UPDATE/DELETE` authority table。

### 3.2 Catalog：definition

#### `agent_definition_heads`

建议字段：

```text
tenant_id          text NOT NULL
definition_id      text NOT NULL              -- agd_...
current_revision   bigint NOT NULL
created_at         timestamptz NOT NULL
PRIMARY KEY (tenant_id, definition_id)
```

外键指向 `tenants`；ID check 与现有 ID grammar 一致；`current_revision` 为正数。定义 head 不含
display text，避免把 mutable projection 与 authority 混在一起。

#### `agent_definition_snapshots`

建议字段：`tenant_id`、`definition_id`、`revision`、`claimed_by`（`hpr_...`）、`publisher_id`
（`pub_...`）、`display_name`、`summary`、`status`（`draft|active|revoked`）、`recorded_at`。

- PK `(tenant_id, definition_id, revision)`；FK 回 head；current snapshot deferrable FK；
- `display_name/summary` 复用 Go 合同的 UTF-8/NFC、无 control、长度约束；
- revoked/draft 历史保留，不通过删除表达撤销；
- 若未来 publisher 有独立 authority，再将 `publisher_id` 改为受控 FK；在此之前至少做格式与
  publisher registry/readback 校验。

### 3.3 Catalog：release、能力和 data route

#### `agent_release_heads` / `agent_release_snapshots`

`agent_release_heads` 的 key 建议为 `(tenant_id, definition_id, release_id)`，含
`current_revision/created_at`，FK 到 definition head。

`agent_release_snapshots` 建议保存：

```text
tenant_id, definition_id, release_id, revision,
version, artifact_digest, manifest_digest, persona_digest,
isolation, status, published_at, recorded_at
```

约束：release ID 必须绑定 definition；digest 为小写 `sha256` 64 hex 且不可为全零；`isolation` 只
允许 `process|container|microvm`；draft 不得有 `published_at`，published/quarantined/revoked 必须
有 UTC 时间；release snapshot 只追加。

#### `agent_release_capabilities`

不要把 requested/prohibitions 作为调用方可覆盖的数组。使用规范化子表：

```text
tenant_id, definition_id, release_id, release_revision,
capability, kind(requested|prohibited),
PRIMARY KEY (tenant_id, definition_id, release_id, release_revision, capability, kind)
```

FK 到同 revision release snapshot；capability 使用与 `ParseCapability` 相同的 grammar、长度和 C
collation。固定 `write_agent_release` function 在同一次写入中检查 requested 与 prohibited 不重叠、
requested 非空、无重复。`kind` 不应由普通 runtime role 直接改写。

#### `agent_release_routes` / `agent_release_route_destinations`

按 route 一行、destination 一行规范化：

```text
agent_release_routes:
  tenant_id, definition_id, release_id, release_revision,
  route_name, direction, classification, retention_days
  PK(..., route_name)

agent_release_route_destinations:
  tenant_id, definition_id, release_id, release_revision,
  route_name, destination
  PK(..., route_name, destination)
```

route name/destination 复用 `NewDataRoute` grammar；不允许 URL、token、connection string、`..` 或
秘密。每个 published release 至少一个 route；route 的 revision 必须与 release revision 同步。

### 3.4 Trust Passport：审阅证据与有效性

#### `agent_passport_heads` / `agent_passport_snapshots`

建议 key 为 `(tenant_id, definition_id, release_id)`，而不是只用 release ID，防止跨 tenant/definition
碰撞。snapshot 至少包含：

```text
tenant_id, definition_id, release_id, passport_revision,
definition_revision, release_revision, status(active|quarantined|revoked),
recorded_at
```

写入 function 必须在同一个事务检查：definition 当前/指定 snapshot 为 active；release snapshot 为
published；ID/tenant/definition 一致；passport revision 单调。passport 不应在运行时复制 release
正文；通过版本 FK 绑定 immutable release。

#### `agent_passport_attestations`

字段建议：`tenant_id, definition_id, release_id, passport_revision, claim, issuer_id,
evidence_digest, issued_at, expires_at, recorded_at`，PK 包含 claim/issuer，FK 到 passport snapshot。

- claim 至少覆盖 `publisher_verified`、`security_reviewed`、`data_routes_reviewed`；
- evidence digest 必须非零；`expires_at > issued_at`；
- 当前 resolver 在 `now` 对每一条 attestation 检查有效期；过期不能只依赖异步刷新；
- attestation 变更生成新 passport revision，不更新旧行。

### 3.5 Tenant installation：实际授权决定

#### `agent_installation_heads` / `agent_installation_snapshots`

`agent_installation_heads`：

```text
tenant_id, workspace_id, installation_id, current_revision, created_at
PRIMARY KEY (tenant_id, workspace_id, installation_id)
```

`agent_installation_snapshots`：

```text
tenant_id, workspace_id, installation_id, revision,
definition_id, release_id, release_revision, passport_revision,
version, agent_actor_id, installed_by,
status(pending|active|suspended|revoked|offboarded),
created_at, disabled_at, recorded_at
```

约束：

- tenant/workspace FK；Agent actor FK 到现有 `actor_heads`，并由 snapshot/status 校验 `agt_`；
- `installed_by` 必须是 tenant 内 human principal；
- `definition/release/passport` 的 composite FK 防止把某 tenant 的 release 装到另一 tenant；
- active/suspended 等非禁用状态不能有 `disabled_at`；revoked/offboarded 必须有不早于 created 的时间；
- offboarded 终态不可回 active；状态迁移只能由 function 依据 expected revision 执行；
- partial unique index 建议限制同一 `(tenant_id,workspace_id,definition_id,release_id)` 至多一个
  active/pending（是否允许多个历史 release active 要由产品策略冻结，不能由实现猜测）。

#### `agent_installation_capabilities`

字段：`tenant_id, workspace_id, installation_id, installation_revision, capability`，PK 为完整
复合键，FK 到 installation snapshot。写入 function 必须通过同一事务的 `EXISTS` join 验证：

```sql
capability exists in the selected passport/release requested set
AND capability is not in the selected release prohibition set
AND installation tenant/workspace/release/passport are consistent
```

这条跨表约束不能只靠普通 CHECK 完成；必须在 SQL function 和 Go domain constructor 都执行。安装时
规范化排序和重复拒绝；读取时按 `capability COLLATE "C"` 排序，确保 digest/replay 稳定。

#### `agent_installation_routes`

保存 installation 对 release route 的实际绑定：`tenant_id, workspace_id, installation_id,
installation_revision, route_name`。FK 到同一 release revision 的 route；未来如果 route 需要额外
租户 policy，新增独立 binding revision，不直接篡改 release route。

### 3.6 Install command、provider effect 和 offboard

#### `agent_install_commands`

不要只依靠通用 `tenant_command_receipts` 的 result digest；安装需要知道“是否已经进入 pending、
哪个 installation、provider effect 到哪一步”。建议字段：

```text
tenant_id, workspace_id, definition_id,
idempotency_key, request_digest, normalized_grants_digest,
target_release_id, target_release_revision,
installation_id, status(received|pending_provider|active|rejected|unknown|conflict),
result_digest, created_at, committed_at, last_error_code
PRIMARY KEY (tenant_id, workspace_id, definition_id, idempotency_key)
```

`request_digest` 必须覆盖 API 版本、definition/release/passport revision、canonical grant set、
bound route set、requested-by principal 和必要 policy revision；不存 token、prompt、原始完整 Key。
同 key 不同 digest 只能得到 conflict；相同 digest 读取原 result，不重复创建 actor/effect。

#### `agent_provider_effects`

外部 provider effect 需要显式 durable receipt，而不是把 provider user/group 的状态当平台事实：

```text
tenant_id, workspace_id, installation_id,
effect_id, effect_kind(user_provision|member_add|member_remove|user_revoke),
provider, provider_realm_id, provider_subject_id,
request_digest, status(queued|sent|committed|replayed|unknown|failed),
attempt_count, provider_receipt_digest, first_sent_at, last_attempt_at, committed_at
PRIMARY KEY (tenant_id, effect_id)
```

`provider_subject_id` 只存经过 grammar/tenant mapping 校验的外部 ID；不存 provider access token、
签名原文或任意 ext_info。`unknown` 必须由 reconcile worker 通过 provider readback/人工处置推进，
不能由超时自动改成 committed。

#### `agent_offboard_commands`

字段建议：`tenant_id, workspace_id, installation_id, idempotency_key, expected_revision,
request_digest, data_disposition(retain|archive|delete), revoke_provider_identity,
remove_conversation_memberships, cancel_active_invocations, revoke_credential_leases,
status(received|running|waiting_effect|committed|unknown|failed), result_digest,
created_at, committed_at`，主键包含 tenant/workspace/installation/idempotency key。

四个 cleanup boolean 必须在 function 中要求全部为 true；它们是清理承诺，不是可选 UI 装饰。每一项
provider/member/credential/invocation effect 以 `agent_provider_effects` 或对应 domain receipt 记录。
installation 只有在 required cleanup 的成功/可重放 receipt 完整后才 CAS 到 offboarded；重试不能重复
删除历史 snapshot，data disposition 只能由受控 worker 执行。

### 3.7 Event/outbox 与既有表的关系

- 平台状态转换（catalog、passport、installation、command receipt）和 canonical domain event 应在同一
  serializable UoW 中写入已有 `event_log`，并使用 expected revision；不能只写事件再异步猜状态。
- provider effect 是外部副作用，需 durable outbox/effect 表和可重入 worker；不能把 provider ACK 放进
  数据库事务里假装 exactly-once。
- 若复用 `tenant_command_receipts`，必须把 `command_kind` 版本化（例如
  `agent.install.v1` / `agent.offboard.v1`），并额外保存 domain result/installation ID；禁止让两个
  不同语义共用同一个 key namespace。
- event payload 只含无秘密、可重建的引用和 digest；Artifact 正文、credential、provider token 不进入
  installation receipt。

## 4. repository 与 UnitOfWork 最小合同

### 4.1 读取合同

建议新增一个 Agent Store repository 组合到现有 `TenantRepositories`（名称可由实现者调整），至少有：

```text
CurrentDefinition(tenant, definitionID) -> definition snapshot
CurrentRelease(tenant, definitionID, releaseID) -> release + normalized children
CurrentPassport(tenant, definitionID, releaseID) -> passport + attestations
CurrentInstallation(tenant, workspaceID, installationID) -> installation + grants/routes
FindActiveInstallation(tenant, workspaceID, definitionID, releaseID) -> current snapshot
ReadInstallCommand(tenant, workspaceID, definitionID, key) -> receipt/result
ReadOffboardCommand(tenant, workspaceID, installationID, key) -> receipt/result
```

所有读取都在同一 UoW transaction snapshot 内完成，返回 domain value object；SQL row 不应泄露到 handler。
读取错误要区分 not found、context unavailable、integrity 和 store unavailable；未知/坏 row 不能被当作
空目录。

### 4.2 写入合同

安装和撤权不是单一 `Save(snapshot)`，建议分成可审计阶段：

```text
ClaimInstall(command) -> existing replay/conflict OR pending installation + outbox effects
RecordProviderEffect(effect receipt) -> CAS effect state
ActivateInstallation(expected revision, committed effects) -> active installation + domain event
ClaimOffboard(command) -> existing replay/conflict OR cleanup plan
CommitOffboard(expected revision, complete cleanup receipts) -> offboarded + projection cleanup
ResolveCommand(command) -> exact durable receipt (including unknown)
```

`Claim*`、`Activate*`、`Commit*` 必须使用现有 `TenantUnitOfWork.Execute` 语义：

1. acquire attested runtime pool connection；
2. 获取 tenant+command 的 advisory lock；
3. `SET LOCAL search_path=pg_catalog` 并绑定 `wanwork.tenant_id`；
4. 在 serializable transaction 中先读 command receipt，再读 current head/passport/ACL；
5. 只调用固定 `SECURITY DEFINER` function 写 authority；
6. 写 result digest/event/outbox 后提交；commit unknown 时释放/隔离连接并用 fresh connection readback。

不能把 `Pool.Acquire` 或 raw `pgx.Tx` 暴露给 Web handler，也不能在事务外读取 Passport 再把值带进
写事务；否则撤销与安装之间会有 TOCTOU。

### 4.3 schema function 的职责

至少冻结以下 function（精确签名、owner、body digest、search_path、权限都进入 authority manifest）：

- `write_agent_definition_revision`：definition head/snapshot successor-only CAS；
- `write_agent_release_revision`：release/能力/route 同步写入，禁止 requested/prohibited overlap；
- `write_agent_passport_revision`：绑定 definition/release revision，验证所需 attestations；
- `claim_agent_install`：验证 tenant/workspace/requester、current Passport、canonical grant subset、
  active uniqueness，并写 pending installation + command；
- `activate_agent_install`：只接受 expected pending revision 和已提交 provider effect receipt；
- `claim_agent_offboard` / `commit_agent_offboard`：强制四项 cleanup、状态 CAS、data disposition；
- `record_agent_provider_effect`：effect idempotency/replay/conflict/unknown；
- 如复用通用 receipt，使用已冻结的 `write_tenant_command_receipt`，但 command kind 必须 domain-specific。

这些 function 应 `REVOKE ALL FROM PUBLIC`，runtime role 只获 exact `EXECUTE`。function 内再次验证
`current_setting('wanwork.tenant_id', true) = p_tenant_id`，不能依赖 RLS 作为唯一业务授权。

## 5. action-time resolver / PEP 边界

### 5.1 Discovery 允许做什么

`GET /agents` 可以在 Repeatable Read 中返回目录投影：显示名、版本、审阅声明、requested/granted
capabilities、routes、installation status。该页允许短暂陈旧，只用于展示和候选选择；它不能授予
`install`、`@Agent`、provider、secret 或 conversation 权限。

缓存 key 必须包含 tenant/workspace/definition/release/passport revision；缓存失效或 schema digest
漂移时返回不可用，不回退到上一次 active projection。

### 5.2 Install 的实时解析顺序

```text
trusted auth context
  → tenant membership + workspace membership/manager permission
  → command key/digest canonicalization
  → current definition/release/passport + attestation(now)
  → grant subset / prohibition / route checks
  → active uniqueness + current head revision
  → pending installation + outbox receipt
  → provider effect worker
  → fresh current read + activate CAS
```

关键点：

- API path 的 `definitionId` 只作为 lookup key；不能接受客户端提交的 release digest、tenant 或
  installedBy 作为 authority；
- `grantedCapabilities` 先 parse、sort、duplicate reject，再以 Passport 当前 release 的 requested
  集合逐项验证；Go constructor 与 SQL function 必须都有这条检查；
- Passport 在 `ClaimInstall` 和 `ActivateInstallation` 两次读取，避免 pending 时间内被撤销的
  release 变 active；
- provider provisioning 只通过普通用户式 adapter；ext_info 是非秘密身份投影，不包含 grant set；
- provider receipt committed/replayed 仍需由平台 command/effect row 记录，不能由 HTTP 200 直接当事实；
- 任何 row 缺失、组合 FK 不一致、function digest drift、RLS 不可用或 fresh readback 不确定都拒绝激活。

### 5.3 Invocation / mention 的实时解析

每次 `@Agent` 需要在同一事务/一致性边界重新解析：

1. Clerk/JWKS 等可信认证上下文得到 principal，不接受客户端 actor；
2. 当前 tenant membership、workspace、conversation membership/access 和 `invoke_agent` 权限；
3. installation head 当前 status=`active`，definition/release/passport/attestation 仍有效；
4. 所需 capability（当前 thread 至少 `conversation.read`）存在于 installation grant；
5. Agent actor 与 installation、provider binding、child thread lineage 一致；
6. mention command receipt、child conversation 和 canonical event 按既有 inbox/outbox/receipt 规则提交。

过期 Passport、offboarded installation、grant 缺失和 ACL 缺失都必须 fail closed；不能因为旧群成员、
旧 Web projection 或 provider 仍显示用户存在而继续执行。

### 5.4 Offboard 的实时解析顺序

先解析 requester 的管理权限和当前 installation revision，再 claim offboard command。随后按 durable
effect 状态机处理：

```text
claim request
  → cancel active invocation / revoke credential lease
  → remove Agent from every bound parent/child conversation
  → revoke provider normal-user identity
  → record committed/replayed/unknown receipts
  → CAS installation → offboarded
  → clear platform membership/access projection (retain/archive/delete policy)
```

顺序和可重试性必须与产品合同冻结。若 provider removal 成功但 revoke unknown，安装不能标记成功；
reconcile worker 依据 effect ID 和 provider readback 收敛。清理不应硬删审计 snapshot，`delete` 仅是
数据处置策略，不是删除 authority history。

## 6. migration 与 cutover 计划

### Phase A：冻结合同（代码不切流量）

- 固定 `DefinitionSnapshot`、`ReleaseSnapshot`、`TrustPassport`、`InstallationSnapshot` 的字段、
  status transition、grant subset 和 route 语义；
- 生成 golden canonical rows/digest，明确历史旧客户端省略 grant field 的兼容行为；
- 将 function signatures、role access、RLS policy、catalog digest 加入 authority specification；
- 规定 synthetic 仅用于测试 fixture，不作为 production backfill source。

### Phase B：新增 schema（只加不改）

- 新增 definition/release/capability/route/passport/attestation/head/snapshot/command/effect 表；
- 每个 migration 通过现有 checksum、old postcondition、empty/non-empty schema、rollback 语义；
- 开启并 FORCE RLS；固定 C collation、ID/digest/status/revision check；
- provision owner/migrator/runtime 权限，runtime 只有 exact function execute/select projection 权限；
- 在 disposable PostgreSQL 18.6 上跑 raw-row constraint、RLS、cross-tenant 和 privilege-negative tests。

### Phase C：repository/UoW（双读但单写）

- 先实现 repository contract tests，再接 `TenantUnitOfWork`；
- discovery API 只读 PostgreSQL projection；install/offboard 先 feature flag 关闭或仅 synthetic；
- 开启 durable install command，但 provider effect 先使用 test adapter，完整记录 committed/replayed/unknown；
- 严禁 DB 失败时 fallback memory。若要保留本地演示，使用明确的 `synthetic` mode，不能与 production
  tenant 混用。

### Phase D：真实数据导入/切换

- 对每个 tenant 做受控 dry-run：definition/release/passport 来源、digest、attestation、actor mapping、
  workspace 和 active installation 计数；
- provider 侧已有 user/group 只能作为待核对外部 evidence，不得直接生成 active installation；
- backfill 先写 immutable catalog/passport，再写 installation pending，经过 provider readback 后 activate；
- 双读期间比较 projection/digest，差异进入 quarantine；不要静默覆盖数据库 authority；
- 切换 install/invoke/offboard 写流量到 durable；旧 map 只读诊断，稳定窗口后删除 fallback 代码。

### Phase E：故障与回滚

- schema migration 失败：停在上一个 migration，修复后向前迁移；不能手改历史 checksum；
- repository/function drift：readiness false，禁止 effectful route；
- provider ACK unknown：保留 command/effect=`unknown`，人工或 reconcile 决定，不重复猜测；
- durable 切换后的业务 rollback 只能用 forward compensation（revoke/offboard/new revision），不能恢复
  已被观察的 active snapshot 或删除 receipt；
- `kill -9`、DB 重启、连接断开、旧 binary 读新 schema、备份恢复和多实例竞争都必须有演练记录。

## 7. 验证矩阵（最小可接受证据）

### Schema/权限

- 空库、已有数据、重复 migration、rollback/restore、checksum drift；
- runtime role 对 authority 表 raw `INSERT/UPDATE/DELETE` 全部拒绝；只允许精确 function；
- tenant A 的 session 看不到 tenant B 的 definition/release/passport/installation/command/effect；
- workspace 不属于 tenant、actor 类型错误、跨 definition/release/passport FK 均拒绝；
- RLS context 缺失、错误、事务结束后遗留、connection pool 污染均 fail closed；
- digest、ID、status、revision、timestamp、UTF-8/控制字符和最大集合边界负测。

### Catalog/Passport

- requested/prohibited overlap、空 requested、重复 capability、重复 route/destination；
- release digest/version 变化只产生 successor revision，不覆盖旧 snapshot；
- 缺 publisher/security/data-route 任一 attestation、已过期、未来 issued、Passport revoked/quarantined；
- definition/release/passport 不一致、旧 release 冒充当前 release；
- action-time resolver 在目录缓存仍为 active 时准确拒绝当前 revoked/expired row。

### Installation/grant

- omitted `grantedCapabilities` 按兼容规则授予完整 requested；
- 显式单项/多项 subset 成功并返回 canonical sorted grant；
- 未声明、prohibited、非法、重复、显式空数组拒绝；
- 相同 key+digest replay；相同 key 改 grant/release/passport/workspace/requester conflict；
- 两个实例并发同 key 只有一个 pending/active winner；另一个精确 replay/conflict；
- pending provider effect 期间重启，恢复后不重复 provision；commit unknown 使用 fresh readback；
- active installation 的 grant drift、release drift、Passport revoke 均禁止静默修改/继续执行；
- `InstallationOffboarded` 不可回 active；新 install 必须是新 command/new revision/new actor policy。

### Offboard/provider

- member removal、user revoke、credential lease、active invocation 各自有 effect receipt；
- 任一 effect failed/unknown 时 installation 仍为 pending/revoking，不返回 committed success；
- provider replay、provider conflict、mapping drift、错误 realm/subject、callback authenticity 失败；
- 重试同 key 同 disposition replay，改 disposition conflict；
- retain/archive/delete 的数据处置只影响受控 data plane，不删除 authority history；
- offboard 后 conversation membership/access、invoke permission、mention resolver 均拒绝。

### 运维/恢复

- PG18.6 normal/race/serializable/deadlock/connection pool contamination；
- outbox worker 重启、重复 delivery、延迟/乱序 callback、provider outage、租约过期；
- event projection 清空后从 canonical event 重建相同 Agent Store projection；
- backup/restore 后 command digest、head current revision、effect unknown 状态和审计链一致；
- API envelope 不泄漏 token、原始 provider error、grant authority、digest 之外的敏感值；
- readiness 在 catalog/function/RLS/role/schema drift 时关闭写 route。

## 8. 当前明确未完成与验收出口

截至本审计，以下事项仍是 No-Go，不能用本地 Web 绿色门禁替代：

- Agent Store PostgreSQL migrations/schema/function/repository/UoW 尚未实现；
- durable installation/command/offboard/provider effect receipt 尚未接入 `TenantUnitOfWork`；
- 当前 `CatalogRepository`/`InstallationRepository` 仍只是 domain interface；
- 真实 Clerk/JWKS trusted tenant/principal context、真实融云 callback/readback/authenticity 未完成；
- invocation、Task/Artifact/Acceptance 与 Agent Store 的 durable join/outbox/reconcile 未闭合；
- backup/restore、live credential rotation、multi-instance cutover 和 rollback drill 未完成；
- synthetic/fake provider 的 `committed` 只证明合同测试，不证明外部消息送达、用户撤权或 exactly-once；
- Notion 本轮按用户指令保持 `local_pending`，不在此文档宣称远端已更新。

Durable Agent Store 阶段的出口必须同时满足：

1. migration catalog、authority access manifest、RLS/function digest 与 PostgreSQL 18.6 evidence 可回读；
2. repository contract 与现有 domain constructor 一致，并通过 normal/race/cross-tenant/unknown 测试；
3. install/invoke/offboard 的 action-time resolver 不依赖 discovery cache，grant 子集和状态 CAS 有强证据；
4. provider effect outbox、replay/conflict/unknown reconcile 可重启恢复；
5. production role 不能 raw mutate authority，secret canary、backup/restore 和 readiness gate 通过；
6. 旧内存 fallback 被移除或只保留在显式 synthetic mode，且没有同一 tenant 双写/双读 authority。

在上述出口以前，用户可以继续验收当前 Web synthetic Agent Store，但“已安装”“已撤权”“能力已授予”
只代表本地进程内的合同结果；接入真实 IM 或多实例生产流量仍应保持 No-Go。

## 9. 关联证据

- [`research/52_agent_store_acceptance_20260830.md`](52_agent_store_acceptance_20260830.md)：当前 Agent
  Store Web/fake vertical slice、安装、最小权限和 offboard 验收；
- [`research/53_agent_store_live_gate_20260830.md`](53_agent_store_live_gate_20260830.md)：实时门禁、远端
  SHA 与 `local_pending` 边界；
- [`docs/wanwork_im/W2_POSTGRES_RUNTIME_CHECKPOINT.md`](../../docs/wanwork_im/W2_POSTGRES_RUNTIME_CHECKPOINT.md)：
  attested runtime pool、tenant UoW、readiness 和 PostgreSQL 现状；
- [`docs/wanwork_im/W2_POSTGRES_CUTOVER_PLAN_CHECKPOINT.md`](../../docs/wanwork_im/W2_POSTGRES_CUTOVER_PLAN_CHECKPOINT.md)：
  Gate A0 topology、authority specification、cutover/No-Go 顺序；
- [`apps/im-api/internal/agentstore/catalog.go`](../../apps/im-api/internal/agentstore/catalog.go)：
  definition/release/Passport value contract；
- [`apps/im-api/internal/agentstore/installation.go`](../../apps/im-api/internal/agentstore/installation.go)：
  installation grant、状态迁移、offboarding contract 与 repository ports；
- [`apps/im-api/internal/platform/postgres/imstore/uow.go`](../../apps/im-api/internal/platform/postgres/imstore/uow.go)：
  attested runtime UoW、serializable command receipt 和 commit-unknown readback；
- [`apps/im-api/internal/platform/postgres/migrations/catalog.go`](../../apps/im-api/internal/platform/postgres/migrations/catalog.go)：
  当前 `0001..0010` migration catalog/checksum 机制。
