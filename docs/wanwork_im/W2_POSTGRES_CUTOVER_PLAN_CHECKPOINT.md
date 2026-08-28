# W2 PostgreSQL authority plan、可信文件与 detached approval 检查点

日期：2026-08-29

分支：`dev_wanwork_quantum_entanglement`

实现基线：`d2f1bf0`（已推送 `origin/dev_wanwork_quantum_entanglement`）

同步状态：`local_pending`。按 2026-08-29 最新决策，本地代码、测试、报告和 GitHub 先全部收口，Notion
最后统一批量同步并逐页回读；本检查点当前不得解释为 Notion 已更新。

## 1. 结论

Gate A0 已把原来只存在于验证器和文档中的 PostgreSQL authority 期望值收敛成 immutable production
specification，并交付 canonical cutover plan、严格 decoder、descriptor-based 可信文件加载和 detached Ed25519
approval verifier。plan 现在还绑定真实 PostgreSQL 物理 cluster identity、专属 cell 的 transient cutover authority
graph 和由代码唯一派生的五阶段 workflow。plan 能稳定绑定 Git source、
release artifact、migration catalog、authority manifest/specification、目标数据库和 TLS 身份、三类 credential
generation reference、备份、回滚边界、空/非空分类、五阶段步骤、审批引用、expiry 与 evidence destination；
verified approval 进一步绑定 exact plan、审批人、deployment/cell/reference scope、key generation/fingerprint、
policy revision 和短时有效期。

本检查点**不是 Gate A0 完成**。尚未交付 authenticated approval policy snapshot、bootstrap/cutover executor、durable
receipt/reconcile store、hardened secret file provider、clean Linux IaC、remote authenticated-TLS 正向 E2E、
empty/non-empty production cutover 和 restore 演练。当前代码只能构建/验证计划和审批、读取受信文件，不能执行
数据库写入；`VerifiedApproval` 也不是 single-use lease，必须由后续 executor 在 durable fence 中原子消费。

## 2. 已交付代码

### 2.1 单一 authority specification

`migrations.CurrentAuthorityAccessSpecification` 是 detached snapshot，每次调用返回新的 slice backing array。
它同时驱动 migration/runtime validator 的期望值，覆盖：

- PostgreSQL major；
- database owner preflight boundary；
- LOGIN/NOLOGIN、NOINHERIT、cluster attribute、connection limit、valid-until 与 role setting；
- exact membership、grantor、ADMIN/INHERIT/SET option；
- database/schema/relation/function inventory 与 owner；
- database/schema/table/function/default privilege；
- function identity arguments，避免未来同名 overload 误判；
- unexpected object/privilege、column ACL、metadata function 等 fail-closed 负约束；
- migration catalog digest、authority manifest digest；
- executor/validator compatibility version。

validator 不再单独拼接 role、membership、object 或 ACL 期望数组。实际 PostgreSQL catalog readback 会转换为
同一组 specification value 后精确比较；function ACL readback 也包含完整 identity arguments。

### 2.2 Domain-separated digests

当前提供四个 authority 确定性摘要入口：

- `DigestAuthorityAccessManifest`：LOGIN role 数组按 semantic set 排序；
- `CurrentMigrationCatalogDigest`：绑定有序 version/name、up checksum 和独立 down checksum；
- `DigestAuthorityAccessSpecification`：只接受已验证、已排序 specification。
- `DigestAuthorityCutoverSpecification`：独立绑定 transient provisioner/database-owner/grantor/CONNECT 图，
  不与 managed access specification 共用摘要域。

摘要统一使用 `sha256:<64 lowercase hex>`，并使用不同 domain prefix，防止相同 JSON bytes 在 manifest、
catalog、specification 与 plan 之间被当作同一语义对象。

### 2.3 Canonical cutover plan v4

`authoritycutover.BuildPlan` 不接受调用方自报的 catalog/manifest/specification digest，也不接受调用方提供
`Steps` 或 `AbortConditions`。它从 production
migration package 重新解析 manifest、catalog 和 specification，再写入 plan。v4 plan 明确绑定：

- `planId`、source commit/tree、release artifact digest；
- migration catalog、authority manifest/specification 和 executor/validator compatibility；
- transient cutover specification、专属 cluster-cell topology 和 exact IaC/bootstrap grantor；
- deployment/cell/database/server identity、PostgreSQL 18 与 `verify-full` CA/server-name binding；
- probe 返回的 database/login/server/CA scope、primary、`pg_control_version`、`catalog_version_no` 与经过独立
  domain hash 的 physical `system_identifier`；raw system identifier 不进入 plan；
- provisioner/migration/runtime 三个互不共享的 typed `secret/...` reference 和 generation；
- migration/runtime login 必须属于 manifest，provisioner login 必须与受管 authority role 隔离；
- from/to schema version、`empty|non_empty` 显式分类；
- preflight/bootstrap/migrate/cutover/runtime-proof 恰好五阶段，action 固定为 `read-authority`、
  `create-authority`、`apply-catalog`、`converge-ownership`、`attest-runtime`；
- 每步 action、executor、transaction class、pre/post/abort digest；
- required backup、rollback artifact/boundary、abort condition set、evidence destination；
- approval identity/reference、UTC second expiry 与 exact plan digest。

preflight 的 expectation/pass/abort policy 分别使用三个独立摘要域，三者都显式
`mutationAuthorized=false`；unknown 结果阻断，观察最长 60 秒。Decode 会从 plan 其余字段重新派生 workflow 并
exact compare，unknown/extra/missing/reordered action、executor、事务类型、条件摘要或检查项全部拒绝。旧 v1/v2/v3
plan 不做隐式升级。

### 2.4 Plan digest 自绑定规则

为避免把 self-referential digest 定义成不可计算对象，v4 固定如下算法：

1. 先完成全部 normalization 与语义验证；
2. 将顶层 `planDigest` 和 `approval.exactPlanDigest` 同时置为空字符串；
3. 对完整、固定字段顺序的 canonical JSON 计算 domain-separated SHA-256；
4. 把同一个结果写入上述两个字段；
5. 再生成最终 canonical bytes；
6. decode 时重复 1–4，并与两个已携带值精确比较。

approval reference 仍需要后续 deployment controller/receipt verifier 验证；plan 自绑定不能冒充人类审批签名。

### 2.5 严格 decoder

`authoritycutover.DecodePlan` 在 typed decode 前执行 duplicate-aware token pass，并固定拒绝：

- 空输入和超过 256 KiB；
- 非 UTF-8、非 NFC、replacement rune；
- 未知字段、duplicate key、trailing value；
- `null`、fraction/exponent number、integer overflow；
- 超过 32 层、单 object 4096 keys、单 array 4096 items；
- 缺字段产生的隐式零值、nil/empty 混淆；
- 非 canonical lowercase identity/digest/Git ID；
- 非 `verify-full`、CA/server identity 漂移；
- catalog/manifest/specification/compatibility/digest 任一漂移。

decoder 不访问数据库、filesystem、secret manager 或网络。执行时的新鲜度、approval legitimacy、backup
existence 和 external artifact existence 仍由后续 preflight/executor 负责。

### 2.6 Descriptor-based 可信文件入口

`LoadPlanFile` 与 `VerifyApprovalFile` 从 filesystem root 开始逐段执行 `openat + O_NOFOLLOW`，并固定要求：

- absolute、clean、UTF-8/NFC path；
- parent/final component 均不跟随 symlink；
- regular file、单链接、exact UID/GID、`0400|0440`；
- 有界大小、descriptor read 前后 device/inode/mode/link/owner/size/mtime/ctime 完全一致；
- Darwin/Linux 使用真实 descriptor 实现，其他平台明确 `ErrUnsupportedFilePlatform`，不回退到普通 read。

错误不包含 path 或 raw bytes。`VerifyApprovalFile` 读取后立即验证且只返回 `VerifiedApproval`，raw signature 不越过
API 边界。

### 2.7 Detached Ed25519 approval

approval signing bytes 与 evidence/key fingerprint 分别使用独立 domain。canonical envelope 只允许 fixed schema、
exact field order、RawURL strict Base64、UTC 整秒和最长 15 分钟 TTL；decoder 拒绝 whitespace/reorder 所形成的第二种
byte representation、unknown/duplicate/trailing/null、非 UTF-8/NFC 和超限输入。

trusted key policy 明确绑定：

- exact `KeyID -> ApproverIdentity`；
- deployment、cell 与 `approval/.../` reference namespace；
- key not-before/not-after、generation、policy revision 与 domain-separated public-key fingerprint；
- revoked tombstone 不能进入 active verifier；
- `.`、`..`、空 segment 和 sibling-prefix reference 固定拒绝。

`VerifiedApproval` 不导出 signature、raw/canonical envelope 或 public key，只提供 bounded evidence metadata。它的
`ApprovalDigest` 是 non-authenticating evidence hash，不是签名替代品，也不防重复 Verify/重复执行。

### 2.8 Short-lived `PreflightReport`

`ObservePreflightReport` 在 exact plan 和 `VerifiedApproval` 绑定成立后，先验证现有连接的 `verify-full` TLS、server
name、verified root CA 与无 fallback，再在同一个 `REPEATABLE READ + READ ONLY` transaction 中执行固定 SQL。
它精确观察：

- `session_user=current_user=provisioner login`、目标数据库和只读事务属性；
- physical system identifier digest、PG major、control/catalog version 与 primary 状态；
- database owner 和 provisioner 的完整 cluster attributes、valid-until、global/database role setting；
- database-owner → provisioner 的唯一 `SET=true / ADMIN=false / INHERIT=false` membership 及 exact IaC grantor；
- provisioner 的唯一直接数据库 ACL；PG18 实测该 `CONNECT` ACL 的 catalog grantor 是 database owner，而 role
  membership grantor 才是稳定 IaC/bootstrap login，两者不得错误合并；
- target database owner、`datallowconn=true`、`datconnlimit=-1`、非 template；
- user schema、relation/function/type/operator/text-search 等 namespace object、extension/FDW/publication/subscription/
  event-trigger/default-ACL/large-object/user cast/language/transform 和 migration ledger；
- `empty` 必须是数据库已经存在且上述 schema/ledger/user object 全空；不是“数据库不存在”；
- backup attestation 与 release artifact 由注入的 authenticated artifact verifier 返回实际 digest。nil、error、panic
  或非 canonical 返回一律为 `unknown`，错误正文不进入报告。

报告固定包含十个有序 typed check，结果只能是 `pass|block|unknown`；block 优先于 unknown，任一非 pass 都不能通过
`ValidatePreflightReport`。expiry 精确取 `min(plan expiry, approval expiry, observedAt+60s)`。报告只保存每项
domain-separated evidence digest，不保存 DSN、password、raw system identifier、证书、SQL row 或 signed approval
envelope。即使全 pass，报告仍是 `mutationAuthorized=false`，后续 executor 必须在 durable fence 内重新验证并原子
消费 approval，不能把报告当成 write lease。

## 3. 提交分解

| Commit | 内容 | 独立边界 |
|---|---|---|
| `cb28fb46d480f37920918fd4996aa20a351ccb04` | immutable authority specification；validator 共用 expected sets | 无 plan/executor |
| `61d8afaf7a19333f264247007e373414d12539ce` | manifest/catalog/spec digest 与 compatibility binding | 无数据库写入 |
| `c08abb717489a93688b8c51d61ac1757a754d0fc` | canonical plan、semantic validation、自绑定 digest、golden digest | 只 build/seal |
| `ad608590f709fe769035c9db97e77da755b0cb48` | duplicate-aware strict JSON decoder | 只离线 decode/verify |
| `f82831a4a339a15ea449ceabb287643a3898f5d1` | trusted descriptor plan loader | 不验证 approval |
| `50366060fb1751612373200dae951a1b7f063b8f` | detached Ed25519 approval verifier | 不接 executor |
| `3db64d258bde9eac747416f687c5377134fe594b` | scoped key trust 与 policy evidence metadata | policy snapshot 仍需防回滚 |
| `c0382b76cd0b5a2e3ec5f148fdad9dcf3daf9bdc` | 拒绝歧义 approval namespace | 不改变 plan digest 算法 |
| `8c31736cf775d5785a4e3ebbd1f9f9b1e6830c09` | trusted approval file read+verify | 只返回 verified evidence |
| `d8f431b` | 固定 database-owner cluster attributes | 仅 managed specification |
| `939c6e7` | plan v2 绑定 physical PostgreSQL cluster | v1 严格拒绝 |
| `0a4a760` | authenticated-TLS cluster probe 与 opaque scoped identity | 只读 RR catalog probe |
| `879b6b4` | 独立 transient cutover authority specification/digest | 仅 dedicated cluster cell |
| `69cae6f` | plan v3 绑定 cutover graph 与 exact grantor | v1/v2 严格拒绝 |
| `91b42af` | plan v4 完全由代码派生五阶段 workflow 和 typed preflight policy | v1/v2/v3 严格拒绝 |
| `f9e2b24` | immutable short-lived PreflightReport、self digest、exact plan/approval/policy binding | 不授权 mutation |
| `104d61f` | 按 PG18 实测区分 membership grantor 与 database ACL grantor | 不放宽 exact membership |
| `d2f1bf0` | fixed-SQL RR/read-only observer、空库 inventory 与 artifact fail-closed seam | 无数据库写入 |

## 4. 本地验证证据

2026-08-29 在本机 PostgreSQL `18.6 (Homebrew)`、numeric loopback test fixture 上执行：

```bash
cd apps/im-api

WANWORK_TEST_POSTGRES_ADMIN_URL="$LOCAL_TEST_POSTGRES_URL" \
  GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off go test -count=1 ./...

WANWORK_TEST_POSTGRES_ADMIN_URL="$LOCAL_TEST_POSTGRES_URL" \
  GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off go test -race -count=1 ./...

GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off go vet ./...
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off go mod verify
```

结果：

- 全部 Go package normal 通过；
- 全部 Go package race 通过；
- `go vet ./...` 通过；
- `go mod verify` 返回 `all modules verified`；
- authoritycutover 的 Linux/Windows cross-build 通过；
- authoritycutover/migrations/imstore/runtimepool 的 JSON 事件统计为 `skip_events=0 fail_events=0`；
- 当前 v4 golden plan digest：`sha256:a8454c2351f8dee0fc8171961b3faf4c84a85151d31ed57f9f6272046557fd07`；
- cutover authority golden digest：`sha256:ab3d5e7755eb6d987e846fdced242b4049d1edf8659d5bb8679ad767e0983f07`；
- preflight report golden digest：`sha256:c1f1f8535a85043746edeed50d74dbd8deb1541443d2eacaf0df485a24bf6e6e`；
- staged credential/旧称 canary 通过；没有写入完整 API key、DSN 或 credential material。

该 loopback fixture 不构成 remote authenticated-TLS 或 clean-host production evidence。

## 5. 当前 Go/No-Go

| 能力 | 状态 | 解释 |
|---|---|---|
| authority specification snapshot/digest | Go | production code、detached、validator 共用 |
| canonical cutover plan build/decode | Go | offline contract only |
| exact plan approval verification | Go（bounded） | Ed25519、scope/key-policy/file trust 已实现；不是 single-use execution lease |
| policy snapshot freshness/archive | No-Go | policy 认证、防回滚、原子替换与 immutable archive 尚未实现 |
| trusted cluster identity probe | Go（bounded） | production API 强制现有连接为 verify-full TLS；本机正向只验证 catalog 语义，不构成 remote TLS 证据 |
| provisioner preflight report | Go（bounded） | fixed SQL、short TTL、typed checks、真实 PG18 empty/drift 路径已交付；artifact verifier 生产实现及 executor fence 尚未交付 |
| bootstrap/cutover executor | No-Go | 不存在任何 plan 驱动的 production SQL write path |
| receipt/unknown-result reconcile | No-Go | durable receipt schema/store/state machine 未实现 |
| production secret injection | No-Go | hardened file/FD provider 未实现 |
| remote TLS production evidence | No-Go | 当前只验证 strict policy 和本地 loopback fixture |
| live credential rotation | No-Go | 必须等待 Gate C0 explicit draining |
| Clerk/融云真实接入 | No-Go | Gate A0 及后续 trusted tenant/resolver 尚未闭合 |

## 6. 下一执行顺序

1. 实现认证、不可回滚、可原子替换和长期归档的 approval policy snapshot；
2. 冻结 receipt format/state transition、append/readback 与 unknown-result reconcile，并原子消费 approval digest；
3. executor 只消费同一 specification，按事务/非事务 boundary 拆步，默认 dry-run；
4. 在 PG18 fixture 做 empty/repeat/concurrent/drift/unknown-result 零跳过；
5. 实现 hardened secret file/FD provider；
6. 建 remote private-CA `verify-full` E2E 和 clean Linux cell；
7. 完成 non-empty upgrade、backup/restore 与 old/future binary/schema 演练；
8. Gate A0 全部证据收口后，才进入 Clerk trusted request context。

任何一步都不得为了测试便利把 provisioner/migration secret 注入 API，或把 integration fixture 当作 production
IaC。Notion 在本地代码、测试、文档和 Git 阶段全部收口后再批量镜像并逐页回读。
