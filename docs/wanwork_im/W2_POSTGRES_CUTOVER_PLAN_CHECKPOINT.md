# W2 PostgreSQL authority specification 与 cutover plan 检查点

日期：2026-08-29

分支：`dev_wanwork_quantum_entanglement`

实现基线：`ad608590f709fe769035c9db97e77da755b0cb48`

## 1. 结论

Gate A0 已把原来只存在于验证器和文档中的 PostgreSQL authority 期望值收敛成 immutable production
specification，并交付第一版 canonical cutover plan builder 与严格 decoder。plan 现在能稳定绑定 Git source、
release artifact、migration catalog、authority manifest/specification、目标数据库和 TLS 身份、三类 credential
generation reference、备份、回滚边界、空/非空分类、五阶段步骤、审批引用、expiry 与 evidence destination。

本检查点**不是 Gate A0 完成**。尚未交付 provisioner preflight、bootstrap/cutover executor、durable
receipt/reconcile store、hardened secret file provider、clean Linux IaC、remote authenticated-TLS 正向 E2E、
empty/non-empty production cutover 和 restore 演练。当前 plan 只能被构建、canonicalize、digest、严格解码和
离线验证，不能执行数据库写入。

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

当前提供三个确定性摘要入口：

- `DigestAuthorityAccessManifest`：LOGIN role 数组按 semantic set 排序；
- `CurrentMigrationCatalogDigest`：绑定有序 version/name、up checksum 和独立 down checksum；
- `DigestAuthorityAccessSpecification`：只接受已验证、已排序 specification。

摘要统一使用 `sha256:<64 lowercase hex>`，并使用不同 domain prefix，防止相同 JSON bytes 在 manifest、
catalog、specification 与 plan 之间被当作同一语义对象。

### 2.3 Canonical cutover plan

`authoritycutover.BuildPlan` 不接受调用方自报的 catalog/manifest/specification digest。它从 production
migration package 重新解析 manifest、catalog 和 specification，再写入 plan。第一版 plan 明确绑定：

- `planId`、source commit/tree、release artifact digest；
- migration catalog、authority manifest/specification 和 executor/validator compatibility；
- deployment/cell/database/server identity、PostgreSQL 18 与 `verify-full` CA/server-name binding；
- provisioner/migration/runtime 三个互不共享的 typed `secret/...` reference 和 generation；
- migration/runtime login 必须属于 manifest，provisioner login 必须与受管 authority role 隔离；
- from/to schema version、`empty|non_empty` 显式分类；
- preflight/bootstrap/migrate/cutover/runtime-proof 五阶段有序步骤；
- 每步 action、executor、transaction class、pre/post/abort digest；
- required backup、rollback artifact/boundary、abort condition set、evidence destination；
- approval identity/reference、UTC second expiry 与 exact plan digest。

semantic set 只限 abort conditions、credential inventory 和 manifest login roles；这些集合重排不改变 canonical
bytes。steps 保留业务顺序，重排会改变语义或直接违反 phase order。

### 2.4 Plan digest 自绑定规则

为避免把 self-referential digest 定义成不可计算对象，v1 固定如下算法：

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

## 3. 提交分解

| Commit | 内容 | 独立边界 |
|---|---|---|
| `cb28fb46d480f37920918fd4996aa20a351ccb04` | immutable authority specification；validator 共用 expected sets | 无 plan/executor |
| `61d8afaf7a19333f264247007e373414d12539ce` | manifest/catalog/spec digest 与 compatibility binding | 无数据库写入 |
| `c08abb717489a93688b8c51d61ac1757a754d0fc` | canonical plan、semantic validation、自绑定 digest、golden digest | 只 build/seal |
| `ad608590f709fe769035c9db97e77da755b0cb48` | duplicate-aware strict JSON decoder | 只离线 decode/verify |

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
- authoritycutover/migrations/imstore/runtimepool 的 JSON 事件统计为 `skip_events=0 fail_events=0`；
- golden plan digest：`sha256:7d833045167e66b8270513c25a1aac3a24ad7272b5d5423fa5131785bc82a564`；
- staged credential/旧称 canary 通过；没有写入完整 API key、DSN 或 credential material。

该 loopback fixture 不构成 remote authenticated-TLS 或 clean-host production evidence。

## 5. 当前 Go/No-Go

| 能力 | 状态 | 解释 |
|---|---|---|
| authority specification snapshot/digest | Go | production code、detached、validator 共用 |
| canonical cutover plan build/decode | Go | offline contract only |
| exact plan approval verification | No-Go | 只有 identity/ref/digest binding，没有 deployment controller verifier |
| provisioner preflight | No-Go | 未连接 cluster/backup/artifact/receipt readback |
| bootstrap/cutover executor | No-Go | 不存在任何 plan 驱动的 production SQL write path |
| receipt/unknown-result reconcile | No-Go | durable receipt schema/store/state machine 未实现 |
| production secret injection | No-Go | hardened file/FD provider 未实现 |
| remote TLS production evidence | No-Go | 当前只验证 strict policy 和本地 loopback fixture |
| live credential rotation | No-Go | 必须等待 Gate C0 explicit draining |
| Clerk/融云真实接入 | No-Go | Gate A0 及后续 trusted tenant/resolver 尚未闭合 |

## 6. 下一执行顺序

1. 为 plan 增加 operator-facing bounded file loader 和 detached approval verifier contract；
2. 实现 read-only provisioner preflight report，先做 identity/version/database/role/plan drift；
3. 冻结 receipt format/state transition、append/readback 与 unknown-result reconcile；
4. executor 只消费同一 specification，按事务/非事务 boundary 拆步，默认 dry-run；
5. 在 PG18 fixture 做 empty/repeat/concurrent/drift/unknown-result 零跳过；
6. 实现 hardened secret file/FD provider；
7. 建 remote private-CA `verify-full` E2E 和 clean Linux cell；
8. 完成 non-empty upgrade、backup/restore 与 old/future binary/schema 演练；
9. Gate A0 全部证据收口后，才进入 Clerk trusted request context。

任何一步都不得为了测试便利把 provisioner/migration secret 注入 API，或把 integration fixture 当作 production
IaC。Notion 在本地代码、测试、文档和 Git 阶段全部收口后再批量镜像并逐页回读。
