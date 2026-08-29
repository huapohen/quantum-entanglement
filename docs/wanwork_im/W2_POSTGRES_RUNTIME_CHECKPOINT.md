# W2 PostgreSQL Attested Runtime Composition 工程检查点

> 当前代码证据基线：`53dd38b4224003a415605074f25470405ebe799e`
>
> 当前分支：`dev_wanwork_quantum_entanglement`，未合并 `main`
>
> 深度证据：`analysis_report/research/35_postgres_attested_runtime_composition_checkpoint.md`

## 1. 工程结论

当前 W2 已从“PostgreSQL exact access 只存在于 integration fixture”前进到“真实 API composition 会使用
runtime-only credential 并在 effect 前 fail closed”。已交付的最窄链路是：

```text
private runtime config
  -> shared strict connection policy / single canonical parse
  -> pgxpool physical connection attestation
  -> acquisition-time session guard
  -> full exact catalog readiness
  -> attested-only UnitOfWork constructor
  -> runtime API readiness / business route barrier
```

独立 `im-migrate` 已把 owner-capable migration URL 与 API 进程分开。当前仍缺 production authority
bootstrap/cutover、credential rotation、old-session drain、恢复演练、trusted tenant 与 action-time resolver，
因此 W2 尚未完成，真实 IM outbound 继续关闭。

## 2. 当前可用入口

在仓库根目录执行默认零外联验收：

```bash
./scripts/start_im_api.sh
curl --fail http://127.0.0.1:18080/health/live
curl --fail http://127.0.0.1:18080/api/v1/system/ping
```

Runtime PostgreSQL 模式只在显式提供以下三项时启用：

- `WANWORK_IM_POSTGRES_RUNTIME_URL`：私有 credential-bearing URL；
- `WANWORK_IM_POSTGRES_AUTHORITY_MANIFEST`：非秘密 exact database/role/login manifest JSON；
- 本地明文测试才可设 `WANWORK_IM_POSTGRES_ALLOW_INSECURE_LOCAL_TEST=true`。

启动脚本还要求显式 `WANWORK_IM_ALLOW_RUNTIME_COMPOSITION=1`，用于阻止 ambient environment 意外连接。

Migration process 使用 `WANWORK_IM_POSTGRES_MIGRATION_URL`，不由 API 读取：

```bash
GOTOOLCHAIN=local go run ./apps/im-api/cmd/im-migrate
```

真实 URL/password 只能由未入 Git 的 secret injection 提供，不能复制进本文档、启动脚本、日志、报告、
截图或 Notion。

## 3. 当前已交付边界

### 3.0 Durable native IM inbox admission（本地增量）

当前分支已增加 PostgreSQL migration `0009_native_im_inbox` 与 runtime-only
`NativeIMInboxStore`。它只负责 verified envelope 进入平台后的 digest-bound transport 去重：同一
`tenant/workspace/provider/channel/eventId` 加相同 event/payload digest 返回 `replayed`，digest 漂移
返回冲突；RLS、function-only writer 和完整 row readback 已在本机 PostgreSQL 18.6 验证。对应证据为
`analysis_report/research/45_postgres_native_im_inbox_implementation.md`，当前 `local_pending`。

这不等于 provider callback 已认证，也不等于消息/mention/Agent 执行已接通；strict verified envelope、
Clerk trusted context、event bridge、outbox/action receipt 和真实融云仍为 NO-GO。`NativeIMInboxStore`
现在已把 commit-unknown 隔离并通过 fresh readback reconcile：精确找到相同 receipt 时只返回
`replayed + ResolvedAfterUnknown=true`，找不到或发生漂移则返回 `ErrInboxCommitUnknown`，不会触发下游
路由。详细证据见 `analysis_report/research/46_postgres_inbox_commit_unknown_reconcile.md`。Notion
暂不写入，待本地阶段收口后批量同步并回读。

### 3.0.1 Inbox → canonical event 原子桥（本地增量）

本地提交 `f24786a` 增加 `eventstore.NativeIMAtomicStore`。verified
`events.InboxEventProjection` 现在显式携带 schema/stream/event type/actor/time、correlation 与 retry
identity，并在进入数据库前重算完整 `EventToAppend` canonical digest；digest 不一致直接拒绝。

`AdmitAndAppend` 在同一 serializable PostgreSQL transaction 内依次执行 inbox admission、canonical
`write_event` 和双方 readback，然后只经过一个 commit acknowledgement 对外返回。首次 append 失败会连同
inbox receipt 一起回滚；replayed inbox 如果找不到精确对应 event 返回 `ErrInboxEventInconsistent`，不会按
调用方 revision 擅自修复。commit acknowledgement unknown 时会隔离当前连接，并从 fresh connection 同时
readback 两侧；只有两侧都精确匹配才返回 `replayed + ResolvedAfterUnknown=true`。

对应设计与边界见
[`analysis_report/research/47_postgres_atomic_inbox_event_bridge.md`](../../analysis_report/research/47_postgres_atomic_inbox_event_bridge.md)。
该增量仍不等于 callback authenticity、Clerk trusted context、message/mention/Agent 路由或真实 RongCloud
outbound 已完成；真实 provider 继续关闭。Notion 继续遵循本地优先策略，待阶段收口后批量同步并回读。

### 3.0.2 Inbox function semantics hardening（本地增量）

migration `0010_native_im_inbox_semantics` 在不修改已发布的 0009 文件与 checksum 的前提下替换同签名
admission function：拒绝全零摘要、控制字符 identity、inline payload digest 与 bytes 不一致、非法
reference shape，并保护 `delivery_count` 上限。v9 的历史 function digest 与 v10 的新 digest 分开验证，
因此空库升级、重复 Apply 与旧 postcondition 不会被静默放宽。详细报告见
[`analysis_report/research/48_postgres_inbox_semantics_hardening.md`](../../analysis_report/research/48_postgres_inbox_semantics_hardening.md)。

本地 PostgreSQL 18.6 的 migration 全包及毒性 payload 正负向测试已通过；这只证明 disposable 数据库
合同，不等于 production migration/cutover。真实 provider、Clerk 和 outbound 仍保持关闭，Notion 仍待
本地阶段收口后批量同步。

### 3.1 Connection policy

- URL 必须显式携带 user、host/Unix socket、port、database、sslmode；
- 远程 URL 还必须显式携带非空 password；passwordless 只允许显式开启的 loopback/Unix-socket 测试；
- database 与 login 必须出现在 exact manifest；
- DSN 只允许 connectivity/credential/TLS keys；
- pgx 识别的 24 个 `PG*` 环境变量，以及 Go system-root loader 的
  `SSL_CERT_FILE/SSL_CERT_DIR`，只要存在就拒绝，包括空值；
- raw query 使用严格 `url.ParseQuery`；任何 malformed pair 都使整个配置失败，不会被静默丢弃；
- 拒绝 `role/search_path/options/application_name` 等 session parameters；
- 拒绝 pgx query-mode/cache 与 pgxpool size/lifetime/health 参数偷渡；
- 拒绝 service/servicefile/passfile；
- 内部 canonical DSN 强制 `passfile=`，并在 URL 未明确提供时强制空
  `sslcert/sslkey/sslrootcert`，不采用默认 `.pgpass` 或默认客户端证书材料；
- 拒绝 `sslmode=require`、`prefer` downgrade、multi-host/fallback；
- 远端必须是 verify-full 等价 authenticated TLS；
- 明文仅允许显式开启的 numeric loopback/absolute Unix socket 本地测试。
- 最终 host/port/database/login/password 与审阅 URL 精确一致；connect timeout 同时冻结到字段和受控
  `DialFunc`；runtime pool 不再二次解析 raw DSN。

未显式提供 `sslrootcert` 的远程连接使用宿主 OS root store；这是经审阅的 host TCB，不是应用层冻结的
exact CA digest。两个常见环境覆盖入口已经拒绝，但 production 仍需远程 authenticated-TLS 正向 E2E、
trust-store 责任边界和变更审计。

### 3.2 Runtime pool

- `AfterConnect`：initial login/database proof → `SET ROLE runtime` → fixed session baseline → full exact
  validator → baseline recheck；
- `PrepareConn`：open/not-busy/idle、role/database、search path/application name、tenant/session GUC、
  advisory lock、LISTEN；
- session drift 使当前 Acquire 失败并销毁旧物理连接；不静默替换；
- 正常 transaction-local tenant 提交后的空 custom-GUC placeholder 被视为 no authority；任何非空 session
  tenant 被拒绝；
- `Ready` 每次执行 full exact validator，不以 `Ping` 或历史 success 冒充当前 ready；
- public `UnitOfWork` constructor 不接 raw pool。

`Pool.Acquire` 仍是 exported trusted low-level escape hatch：它返回经过 `PrepareConn` guard 的连接，但调用者
仍可在 runtime role 权限内直接执行 SQL。它没有暴露底层 `*pgxpool.Pool`，却也不是 tenant、conversation
或 action-time authority 边界；普通业务写路径必须继续使用受控 UnitOfWork 和后续 PEP。

### 3.3 Service gate

| 路径 | 成功 | 失败 | 含义 |
|---|---|---|---|
| `/health/live` | HTTP 200 | 进程不可达 | 只证明进程活着。 |
| `/health/ready` | HTTP 200 | HTTP 503 | runtime DB exact authority 是否当前可用。 |
| `/api/v1/*` | HTTP 200 success envelope | HTTP 200 / `code=50301` | dependency-before-effect fail closed。 |

SIGINT/SIGTERM 会触发 10 秒 HTTP graceful shutdown；Listen 返回后才关闭 pool。当前还没有独立 draining
readiness state、old credential session termination 或 provider/runtime long-connection drain。

API 进程环境只要存在 `WANWORK_IM_POSTGRES_MIGRATION_URL` 就在启动前拒绝，包括空值；检查只看 presence，
不读取、不保存、不回显 migration credential。one-shot migrator 仍是唯一允许读取该变量的进程。

## 4. PostgreSQL 18.6 已验证矩阵

- exact Open/Ready；
- raw table mutation 与 `MAINTAIN` 拒绝；
- `RESET ROLE`；
- search path/application name drift；
- non-empty session tenant；
- session statement timeout；
- advisory lock；
- LISTEN channel；
- open transaction release；
- function ACL drift、repair 和 full revalidation；
- cancellation 与 pool exhaustion；
- 24 个 ambient `PG*` 变量及 empty-presence 拒绝；
- `SSL_CERT_FILE/SSL_CERT_DIR` ambient trust override 拒绝；
- malformed raw query pair 整体拒绝；
- remote passwordless 拒绝、explicit local passwordless 测试允许；
- default passfile/client TLS file override 与 single canonical pool parse；
- API 拒绝 migration URL presence；
- normal local tenant UoW 不 churn backend PID；
- 全包 normal、全包 race、vet。

验证命令：

```bash
cd apps/im-api
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55488/postgres?sslmode=disable' \
go test ./... -count=1 -timeout=10m

GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55488/postgres?sslmode=disable' \
go test -race ./... -count=1 -timeout=15m

GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off go vet ./...
```

本轮机器可读本地运行摘要见
[`w2-postgres-8ca4fca.local-run.json`](../production/evidence/checkpoints/w2-postgres-8ca4fca.local-run.json)：
17 package、858 个 terminal test pass event、零 skip/零 fail，normal/race 均通过且 vet 无输出；该文件明确
不是 CI artifact 或 production promotion record。

## 5. Go / No-Go

| 能力 | 状态 | 决策 |
|---|---|---|
| 默认 zero-outbound API shell 本地验收 | 已实现 | Go（provider 仅占位标识，adapter 未实现） |
| PostgreSQL runtime config/pool/readiness 专项验收 | 已实现 | Go（仅 disposable local/exact fixture） |
| one-shot migrator config/process boundary | 已实现 | Go（尚非 production bootstrap） |
| production ownership/grant cutover | 未实现 | No-Go |
| production credential rotation/old-session drain | 未实现 | No-Go |
| production restore/restart/rolling upgrade | 未实现 | No-Go |
| trusted Clerk tenant/action-time permission | 未实现 | No-Go |
| RongCloud inbound/outbound | 未实现且 outbound disabled | No-Go |
| `@Agent` durable child thread/Task/Invocation | 未实现 | No-Go |
| production IM / production multitenancy | 未实现 | No-Go |

## 6. 下一垂直切片提交序列（不是 W2 完成清单）

唯一串行门禁以 `IMPLEMENTATION_PLAN.md` 的“W2 接真实 IM 前的 P0 顺序”为准。下面每项仍应按
合同、实现、故障矩阵和证据拆成更小 commit；pure contract 或 zero-network fake 可以提前编译验证，
但不得产生 provider 状态、网络副作用或绕过汇合门禁。

### Gate A0：production deployment authority 与 rotation contract

冻结合同见 [POSTGRES_PRODUCTION_AUTHORITY.md](POSTGRES_PRODUCTION_AUTHORITY.md)。它不构成 IaC、cutover、
remote TLS、rotation 或 production promotion 已完成的证据。

1. `docs(im): freeze production topology iac and secret responsibility boundary`
2. `feat(im): freeze authority cutover plan format`
3. `test(im): canonicalize cutover plan digest`
4. `feat(im): add provisioner preflight`
5. `feat(im): apply transactional object ownership cutover`
6. `test(im): inject grantor and column acl drift`
7. `feat(im): record cutover receipts and reconcile nontransactional steps`
8. `test(im): run empty-db migrate-cutover-runtime e2e`
9. `test(im): run non-empty schema upgrade with retained tenant data`
10. `test(im): prove remote authenticated TLS and controlled host trust injection`

### Gate B：trusted human/Agent authority

11. `feat(im): define Go trusted request context`
12. `feat(im): verify Clerk JWT and rotating JWKS`
13. `feat(im): resolve realm binding principal membership and actor`
14. `test(im): reject every identity and revision drift`
15. `feat(im): require trusted tenant for persistence operations`
16. `feat(im): enforce conversation action-time permissions`
17. `feat(im): bind minimal Agent installation authority to Agent Actor`
18. `test(im): freeze four-subject participant authorization matrix`

### Gate C0：bounded readiness 与 explicit draining

19. `feat(im): add bounded dependency readiness monitor and frozen max staleness`
20. `test(im): keep high-risk action authorization outside readiness cache`
21. `feat(im): expose explicit draining gate state`

### Gate A1：live credential rotation

22. `feat/test(im): replace live pools and prove old sessions cannot survive termination and revoke`

### Gate C1：recovery

23. `test(im): exercise dump restore database restart and process kill-9`
24. `test(im): prove old-binary future-schema and rolling-shutdown behavior`

### Gate D：durable mention vertical slice

25. `feat/test(im): add PostgreSQL EventStore expected-revision and exact dedupe`
26. `feat/test(im): add outbox projection checkpoint backfill and live handoff`
27. `test(im): prove event outbox crash reopen kill-9 restore and rebuild`
28. `feat/test(im): freeze zero-network outbound-disabled provider contract and fake`
29. `feat/test(im): persist agent_thread message and independent ACL after durable gate`
30. `feat/test(im): add durable mention inbox and exact dedupe`（通用 digest-bound native IM inbox 已先由
    `0c9cbac`/`5248a0a` 交付；verified envelope→event/mention bridge 仍未完成）
31. `feat/test(im): dispatch one mention deterministically through the fake`
32. `feat/test(im): issue Go to Python single-use narrow authorization`
33. `test(im): inject duplicate out-of-order ACK-loss and crash end to end`

真实 provider 不在这份 W2 vertical-slice 序列中。进入 subgroup create/invite/send 前，必须另行通过 W3
的 profile/capability matrix、callback authenticity、dedupe/resume、mapping drift、sandbox config 与
inbound-only readback，并取得用户对具体 sandbox outbound 的明确授权。

完成上述 33 项仍不能宣称 W2 完成。message/reaction/read state、Task/Attempt/Budget/NeedsYou/Artifact/
Acceptance、memory/skill/capability、action/evidence、taint/declassification、promotion、presence/data-route/
routine 以及 W2 总计划列出的其余对象，仍须分批交付 schema、repository/UoW、tenant/revision/dedupe
不变量和恢复证据。

## 7. 研究约束

本检查点继续以 `automation/2026/05_08/1/2output` 与 `more` 为一级调研根。DeepSeek Harness 的
everything-is-a-plugin 被实现为 capability seam 和 final-composition validation，不允许插件持有 tenant/
Action/Task/Artifact 不变量；Sandbase/AgentSpace 的核心教训被转化为真实 entrypoint 与 readiness 漏斗；
Clawith/Raft/FloatIM 对 Participant/mention 的证据进入下一阶段，但不会被写成本地已实现能力。

没有向飞书、企微、任何群聊、bot 或 webhook 发送消息；没有操作语雀；没有记录完整 API key。
