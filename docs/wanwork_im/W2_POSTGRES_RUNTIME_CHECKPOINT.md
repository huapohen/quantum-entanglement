# W2 PostgreSQL Attested Runtime Composition 工程检查点

> 当前代码证据基线：`2d0c4a069ef016c085541b3ec26426ccd6ace70b`
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

### 3.1 Connection policy

- URL 必须显式携带 user、host/Unix socket、port、database、sslmode；
- 远程 URL 还必须显式携带非空 password；passwordless 只允许显式开启的 loopback/Unix-socket 测试；
- database 与 login 必须出现在 exact manifest；
- DSN 只允许 connectivity/credential/TLS keys；
- pgx 识别的 24 个 `PG*` 环境变量只要存在就拒绝，包括空值；
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

## 5. Go / No-Go

| 能力 | 状态 | 决策 |
|---|---|---|
| 默认 fake/no-outbound API 本地验收 | 已实现 | Go |
| PostgreSQL runtime config/pool/readiness 专项验收 | 已实现 | Go（仅 disposable local/exact fixture） |
| one-shot migrator config/process boundary | 已实现 | Go（尚非 production bootstrap） |
| production ownership/grant cutover | 未实现 | No-Go |
| production credential rotation/old-session drain | 未实现 | No-Go |
| production restore/restart/rolling upgrade | 未实现 | No-Go |
| trusted Clerk tenant/action-time permission | 未实现 | No-Go |
| RongCloud inbound/outbound | 未实现且 outbound disabled | No-Go |
| `@Agent` durable child thread/Task/Invocation | 未实现 | No-Go |
| production IM / production multitenancy | 未实现 | No-Go |

## 6. 下一提交序列

### Gate A 收尾

1. `feat(im): freeze authority cutover plan format`
2. `test(im): canonicalize cutover plan digest`
3. `feat(im): add provisioner preflight`
4. `feat(im): apply transactional object ownership cutover`
5. `test(im): inject grantor and column acl drift`
6. `feat(im): record cutover receipts and reconcile nontransactional steps`
7. `test(im): run empty-db migrate-cutover-runtime e2e`
8. `feat(im): add bounded readiness monitor and max staleness`
9. `feat(im): expose explicit draining gate state`
10. `feat(im): stage runtime credential rotation`
11. `test(im): prove old sessions cannot survive revoke`
12. `test(im): exercise dump restore restart and rolling shutdown`

### Trusted tenant

13. `feat(im): define Go trusted request context`
14. `feat(im): verify Clerk JWT and rotating JWKS`
15. `feat(im): resolve realm binding principal membership and actor`
16. `test(im): reject every identity and revision drift`
17. `feat(im): require trusted tenant for persistence operations`
18. `feat(im): enforce conversation action-time permissions`
19. `test(im): freeze four-subject participant authorization matrix`

### Mention/thread/provider fake

20. Agent installation authority；
21. `agent_thread` persistence/independent ACL；
22. durable mention inbox/dedupe；
23. single mention deterministic dispatch；
24. fake provider subgroup receipt/reconcile；
25. Go→Python single-use narrow authorization；
26. duplicate/out-of-order/ACK-loss/crash E2E。

## 7. 研究约束

本检查点继续以 `automation/2026/05_08/1/2output` 与 `more` 为一级调研根。DeepSeek Harness 的
everything-is-a-plugin 被实现为 capability seam 和 final-composition validation，不允许插件持有 tenant/
Action/Task/Artifact 不变量；Sandbase/AgentSpace 的核心教训被转化为真实 entrypoint 与 readiness 漏斗；
Clawith/Raft/FloatIM 对 Participant/mention 的证据进入下一阶段，但不会被写成本地已实现能力。

没有向飞书、企微、任何群聊、bot 或 webhook 发送消息；没有操作语雀；没有记录完整 API key。
