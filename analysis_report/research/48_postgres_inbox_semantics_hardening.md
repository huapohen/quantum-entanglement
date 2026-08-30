# 48｜PostgreSQL native IM inbox：semantics hardening migration

> 状态：本地实现检查点；未同步 Notion。
> 代码分支：`dev_wanwork_quantum_entanglement`
> 证据边界：仅证明本地 migration/catalog 与 disposable PostgreSQL 18.6 集成测试；不宣称生产数据库已升级。

## 1. 发现

`0009_native_im_inbox` 的表约束保证了字段类型、长度、摘要外形和 payload kind，但旧的
`admit_native_im_inbox` function 对格式合法的毒性输入仍会放行，例如：

- 全零 event/payload SHA-256 字符串；
- inline JSON 的 `payload_digest` 与实际 payload bytes 不一致；
- provider/channel/event/verification 标识包含控制字符；
- reference payload 的 storage/reference/byte length 组合不符合 Go opaque payload contract；
- `delivery_count` 达到 bigint 上限后继续递增导致异常路径。

这类行会在 Go readback 时变成 integrity/unavailable，造成可被直接调用 function 放大的存储污染。
既有 migration checksum 不可改写，因此没有修改 0009 文件。

## 2. Migration 0010

新增 `0010_native_im_inbox_semantics`，采用 drop/create/revoke 明确替换同签名 function：

- 继续强制 exact tenant GUC、`SECURITY DEFINER`、`search_path=pg_catalog` 和无 PUBLIC execute；
- 在写入前校验 tenant/workspace/provider/transport identity 的边界和控制字符；
- 拒绝全零摘要；
- inline payload 用 PostgreSQL 18 的 `pg_catalog.sha256(convert_to(...))` 与传入 digest 比对；
- reference payload 校验 storage/reference/length 形状；
- replay increment 增加 bigint 上限保护；
- 不改变 inbox 主键或 v9 表 schema，因此 v9 表 postcondition 仍可验证，v10 只切换 function definition digest。

`function_postconditions.go` 保留 v9 digest，并为最新 authority manifest 使用 v10 digest。应用 v10 时，
旧的 v9 postcondition 在 schemaVersion=10 下验证当前强化函数，避免“升级成功但历史 postcondition
立即失败”的陷阱；空库/重复 Apply/未来 ledger 与 down SQL 均有覆盖。

## 3. 验证

本地 PostgreSQL 18.6（`WANWORK_TEST_POSTGRES_ADMIN_URL` 指向 disposable instance）通过：

```bash
cd apps/im-api
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55488/postgres?sslmode=disable' \
  GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
  go test ./internal/platform/postgres/migrations -count=1 -timeout=10m
```

集成矩阵包含 fresh/repeat migration、完整历史 postcondition、role/access manifest、两个 migrator
串行、down SQL disposable rollback，以及“全零 payload digest 被 SQLSTATE 22023 拒绝、真实 digest 可插入”的
正负向测试。migration catalog 仍是连续、带 checksum、不可变的 0001..0010。

## 4. 边界

该 migration 只约束数据库 admission function；它不能独立重算包含 stream/actor/time 的完整
`event_digest`，这仍由 `InboxEventProjection.Event()` 和同事务 atomic bridge 负责。真实 callback
signature/nonce、Clerk trusted request context、RongCloud adapter、outbox/action receipt 与生产 cutover
仍是 No-Go。Notion 继续本地优先，待代码与证据阶段收口后再批量同步并回读。
