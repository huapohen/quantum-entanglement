# Tenant-scoped durable conversation read route 证据（2026-08-30）

> 代码提交：`49e2cf9`
> 前置身份 seam：`8c4fb3f`；helper 回归：`f226ab5`
> 分支：`mainline_continue_quantum_entanglement`
> 运行边界：本地 fake authority 与可注入 UoW；不触碰真实 provider、模型、飞书、企微或 outbound

## 结论

主线现在有一个最小、只读、tenant-scoped 的 durable business route：

```text
Bearer + tenant header
  -> trusted request context (middleware)
  -> path tenant == trusted tenant
  -> fresh action-time identity resolve in UoW read snapshot
  -> conversation head/snapshot
  -> current conversation membership
  -> current read access
  -> safe conversation projection
```

入口：

```text
GET /api/v1/tenants/:tenantId/conversations/:conversationId
```

`tenantId` path 与 `X-WanWork-Tenant-ID` header 不一致直接 forbidden；caller 不能提交 Actor、role 或
permission 来覆盖 repository authority。conversation、membership、access 均从同一 action-time
`TenantUnitOfWork.Read` 快照读取，只有 active conversation + active membership + `read` permission
全部成立才进入 handler。

## 交付内容

- 新增 conversation read route 与安全 projection（id、tenant、type、status、workspace、revision、
  membership role/revision、access permissions/revision）；
- route 内再次调用 `ResolveTrustedRequestContext`，不把 middleware 早先的 Actor revision 当作最终授权；
- path/tenant consistency、conversation ID syntax、missing row、revoked membership、empty access、
  closed conversation 和 cross-tenant path 均 fail closed；
- PostgreSQL `ConversationRepository`、`ConversationAuthorityRepository` 已有对应 current snapshot
  查询，route 只使用 read-only interface，不调用 provider 或写函数；
- readiness barrier 与 bearer ambiguity 仍位于 route 之前，数据库不 ready 时不会触发认证或 repository
  查询。

## 验证证据

```text
cd apps/im-api
go test ./internal/app
PASS

go test ./...
PASS

go vet ./...
PASS

git diff --check
PASS
```

新增 app 负向/正向矩阵：

1. valid bearer + `ten_alpha` + `cnv_room`：返回 group/active、revision 3、membership revision 4、
   access revision 5 与 read/send 权限；
2. path tenant `ten_other` 与 trusted/header tenant 不一致：返回 forbidden，不进入 durable read；
3. access snapshot 为空：返回 forbidden，不产生成功 projection；
4. middleware 与 action-time route 各执行一次 tenant-scoped read，证明 route 不只依赖早期 context；
5. 所有既有 Go 包 test 与 vet 保持绿色。

## 边界与下一步

这只是 durable conversation **read**，仍不是 production business surface：

- `cmd/im-api` 仍使用 reject-all fake verifier，未接入真实 Clerk/JWKS、JWKS rotation 或可信 edge
  tenant injection；
- 尚无 conversation list/message/event cursor、inbox-to-message projection、agent_thread、Task、
  Attempt、Artifact、Needs You 或 action receipt 的 PostgreSQL durable route；
- 当前只验证 read permission，invoke/publish/send 仍需 installation/mandate/capability/budget 与
  action-time PEP；
- route 返回 HTTP 200 envelope 的业务 code 语义不等于 provider delivery、exactly-once 或 GA；
- Gate A–E、真实 IM、外部 outbound、multi-instance recovery 和 Notion/Yuque 同步状态不变。

下一批优先实现 authenticated event/message read contract（cursor/snapshot + tenant/revision/dedupe），
再以同样的 action-time authority seam 接入 message/mention/Agent thread；任何写入前先补 command
identity、atomic UoW、receipt/readback 与 crash boundary。
