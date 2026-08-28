# W2 PostgreSQL `0005`：Function-only Writes 与 Exact ACL 计划

状态：实施前冻结，2026-08-28  
分支：`dev_wanwork_quantum_entanglement`，不合并 `main`  
输入代码基线：`8d662bf`；Notion/文档检查点：`4a465d8`；同步收口：`73974d1`

## 1. 结论

`0001`–`0004` 已证明 authority schema、tenant repository 与 Serializable UoW 的数据库事务语义，
但当前 Go repository 仍直接执行 head `INSERT/UPDATE` 与 snapshot `INSERT`。只要运行时数据库凭据
拥有这些表级写权限，应用内的 repository contract 就可以被同凭据下的任意 SQL 绕过。

`0005` 的目标不是增加新业务对象，而是把现有 ordinary conversation authority 写路径收窄为：

```text
typed Go request
  -> tx-bound repository
  -> fixed SECURITY DEFINER function
  -> explicit tenant-context check
  -> successor-only head CAS + append-only snapshot
  -> same PostgreSQL transaction receipt
```

最终运行时角色只能读取必要 projection、执行登记函数；不能直接修改 head、插入 snapshot、写 receipt、
创建 schema/object、修改 RLS/policy/function，或继承 owner/migrator 权限。

## 2. 一级调研映射

以下两层目录并列作为一级产品证据：

- `automation/2026/05_08/1/2output`
- `automation/2026/05_08/1/2output/more`

本计划直接吸收的硬约束：

| 研究证据 | 硬约束 | `0005` 落点 |
|---|---|---|
| `more/protocol-a2a/research_report.md:482-490` | tenant path/参数不能覆盖 authenticated claim；冲突必须 fail closed | 函数参数 `tenant_id` 必须与 transaction-local tenant context 精确一致；仍不把 GUC 冒充 verified claim |
| `more/tech-agent-security-governance/research_report.md:158-160,317-320` | 长期 credential、context secret、过宽 scope 是核心风险；执行必须经过 trusted control point | runtime 只获 function `EXECUTE` + 必要 `SELECT`，不获 raw table mutation |
| 同上 `:479,597,627` | permission/tenant/role negative cases 必须成为 gate；资源侧应只信受控执行入口 | 真实 PostgreSQL 角色/ACL/负向 SQL 测试，不靠 mock 或 UI permission |
| `more/deepseek-harness/research_report.md:43,433-447,809-837` | 0600 文件与同 UID 不是安全边界；enforcement 必须发生在执行侧 | 数据库权限作为独立执行侧 PEP，不依赖 Go package 私有性或 secret 文件模式 |
| `more/sandbase-harness/research_report.md:485-489,551-569` | schema/配置存在不等于 runtime wiring；permission 必须实际执行 | repository 必须真实调用函数，测试证明 raw SQL 被拒绝、函数 SQL 成功 |
| `more/clawith/research_report.md:198-206,407-419,545-555` | Participant、tenant membership、group membership、action-time assignment/credential 分域 | `0005` 只保护持久化写入口；不把数据库角色或 access boolean 升格为动作时业务授权 |

## 3. 证据口径 `[F] / [C] / [A] / [U]`

- `[F]`：只记录在 PostgreSQL 18.6 实际运行、race/vet 通过并能被独立 SQL 复核的结果。
- `[C]`：保证限定在 `0005` 登记函数、当前 ordinary `direct/group` aggregate 与当前数据库。
- `[A]`：function-only write 是 credential containment 的必要底座，但不替代 Clerk admission、PDP/PEP、
  Agent mandate、budget、provider receipt 或 production secret broker。
- `[U]`：生产登录角色映射、KMS/Vault、restore/kill-9、真实多实例和 provider failure 仍需后续证据。

## 4. 固定函数面

`0005` 只允许以下五个写函数；不提供通用 SQL、动态表名、JSON patch 或任意 procedure：

| 函数 | Exact identity arguments | 返回值 |
|---|---|---|
| `wanwork_im.write_conversation_revision` | `text, text, bigint, bigint, text, text, text` | `boolean` |
| `wanwork_im.write_provider_conversation_binding_revision` | `text, text, text, text, bigint, bigint, text, text` | `boolean` |
| `wanwork_im.write_conversation_membership_revision` | `text, text, text, bigint, bigint, text, text` | `boolean` |
| `wanwork_im.write_conversation_access_revision` | `text, text, text, bigint, bigint, boolean, boolean, boolean, boolean, boolean, boolean` | `boolean` |
| `wanwork_im.write_tenant_command_receipt` | `text, text, text, text, text` | `timestamptz` |

参数名与顺序冻结如下；migration policy、`0005` SQL、Go 调用和 postcondition 必须四处一致：

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

所有函数保持 `STRICT`。`conversation.workspace_id` 是当前唯一可空业务值，因此 wire contract 固定为：

- Go 侧无 workspace 时传空字符串，不传 SQL `NULL`；
- 函数只在 snapshot `INSERT` 处执行 `NULLIF(p_workspace_id, '')`；
- 非空 workspace ID 仍由既有外键和 Go value object 校验；
- 任何其他参数都不使用空串表示 `NULL`。

Go revision 是 `uint64`，PostgreSQL revision 是正 `bigint`。repository 在发 SQL 前必须拒绝
`expected_revision` 或 `next_revision` 大于 `9223372036854775807`，不能把 pgx 编码失败误报为普通 store outage。

四个 revision 函数固定完成：

- `expected_revision=0` 时只允许创建 revision 1；
- `expected_revision>0` 时只允许 `next=expected+1`；
- head CAS 失败返回 `false`，repository 映射为 `ErrRevisionConflict`；
- head 与 snapshot 在同一 function statement / outer transaction 中提交或回滚；
- 参数化 SQL，无动态 `EXECUTE`；
- tenant 参数必须与 `current_setting('wanwork.tenant_id', true)` 精确一致；缺失或冲突返回固定
  SQLSTATE `42501`；
- 函数 owner 即便因部署错误拥有 `BYPASSRLS`，显式 tenant check 仍不可省略。

receipt 函数只写当前 command receipt；它仍不是 provider ACK、完整 typed replay、签名证据或 exactly-once。

## 5. 函数安全 manifest

每个函数必须同时满足：

- `LANGUAGE plpgsql`；
- `VOLATILE`、`STRICT`、`PARALLEL UNSAFE`、`LEAKPROOF=false`；
- `SECURITY DEFINER`；
- fixed `search_path=pg_catalog`，所有业务对象使用 fully-qualified name；
- 无 PUBLIC `EXECUTE`；
- exact identity arguments 与 return type；
- exact owner；
- function definition digest 固定并由 migration postcondition 检查；
- 无额外同名 overload。

SQL allowlist 只为 catalog 中 version 5 打开上述函数 DDL；未来 migration 不自动继承 `CREATE FUNCTION`
权限。该 lexer 仍不是不可信 SQL sandbox，变更必须同时经过 embedded checksum、exact function manifest、
postcondition 与 code review。

## 6. 角色与 ACL manifest

目标逻辑角色均为 group role，不直接登录：

| 角色 | 必须属性 | 禁止属性 |
|---|---|---|
| `wanwork_im_owner` | owns schema/tables/functions | LOGIN、SUPERUSER、CREATEDB、CREATEROLE、REPLICATION、BYPASSRLS、INHERIT |
| `wanwork_im_migrator` | 可被受控 migration login 显式 `SET ROLE` | 同上；不是 owner，不是 runtime 成员 |
| `wanwork_im_runtime` | schema USAGE、必要 SELECT、五个函数 EXECUTE | raw INSERT/UPDATE/DELETE/TRUNCATE、CREATE、TEMP、owner/migrator membership |

环境专属 login role 不写死在 schema migration 中；它只能被显式授予一个 group role。生产部署先由 DBA/IaC
创建角色，再由 startup manifest validator 验证 exact attributes、membership 与 grants。角色不满足时 readiness
fail closed，不能自动“尽力修复”。

阶段内采用两步交付：先完成 owner-only function surface 与 repository wiring；随后提交角色/ACL manifest、
owner transfer、runtime grants 与真实 negative tests。任何中间 commit 都保持 owner-only，不能因函数默认 ACL
临时向 PUBLIC 开放。

历史 postcondition 会在每次 `Apply` 时重验 `0001`–当前版本。现有 `0001`–`0004` 又把
`current_user == schema/table owner` 写进了 invariant，因此禁止直接在 owner-only `0005` 之后单独转移 owner
或授予 runtime 权限：那会让下一次启动在 pending migration 之前 fail closed。角色阶段必须作为一个原子发布单元，
同时完成旧 postcondition owner 语义重构、owner transfer、最终 ACL、startup manifest validator 与真实角色负测；
中间状态不得标记为 runtime-ready。

## 7. 必须通过的真实 PostgreSQL 负向测试

- runtime 对 head `INSERT/UPDATE/DELETE/TRUNCATE` 全部 SQLSTATE `42501`；
- runtime 对 snapshot/receipt `INSERT/UPDATE/DELETE/TRUNCATE` 全部 `42501`；
- runtime 不能 `CREATE TABLE/FUNCTION/SCHEMA`、`ALTER POLICY` 或 `SET ROLE owner/migrator`；
- PUBLIC 不能执行任何登记函数；
- runtime 只能执行 exact function signature，不能调用 overload 或旁路函数；
- unset/wrong tenant context 调函数失败且零写入；
- cross-tenant 参数与 GUC 冲突失败且零写入；
- revision 0→1、N→N+1 正常；duplicate/stale/skipped revision 不产生 snapshot；
- callback error、repository poison、transaction rollback 与 unknown commit 既有语义保持；
- malicious function body、owner、SECURITY INVOKER、search path、ACL、volatility 或额外 overload 使 `Apply`
  返回 `ErrMigrationSchema`；
- 真实 PostgreSQL normal/race、`go vet` 与 migration repeat 全绿。

## 8. 明确不宣称

- 不是 production IM 或 production multitenancy；
- tenant GUC 不是 authenticated membership；
- 数据库 function ACL 不是 action-time business authorization；
- access booleans 不是 invoke/publish permission 的完整答案；
- receipt 不是 provider ACK、完整 replay、不可抵赖证据或 exactly-once；
- provider binding 不证明融云对象存在或消息送达；
- `agent_thread`、message、Task、Artifact、Acceptance、production EventStore/outbox 尚未交付；
- 角色 manifest 完成前不能把 owner-only function surface 标记为 runtime-ready。

## 9. 小提交顺序

1. 本计划与 exact function signature 冻结；
2. migration policy 只对 version 5 开 function DDL；
3. `0005` SQL 与 exact function postcondition；
4. conversation repository 改为 function call；
5. provider binding repository 改为 function call；
6. membership repository 改为 function call；
7. access repository 改为 function call；
8. receipt 改为 function call；
9. non-login role/owner/migrator/runtime manifest；
10. raw-write/tenant/function tamper 真实 PG 负向测试；
11. race/vet/文档/HTML/Notion/readback/bundle/push。
