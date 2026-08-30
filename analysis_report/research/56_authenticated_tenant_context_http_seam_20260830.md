# Authenticated tenant context HTTP seam 证据（2026-08-30）

> 代码提交：`8c4fb3f`；helper 回归：`f226ab5`
> 分支：`mainline_continue_quantum_entanglement`
> 运行边界：本地 fake verifier / fake identity authority；不连接真实 Clerk、融云、飞书、企微或任何 outbound

## 结论

本节点把已经存在的 `auth.ResolveTrustedRequestContext` 和 PostgreSQL
`IdentityAuthorityRepository` 接入 Go HTTP composition root，形成一个可运行、只读、fail-closed 的
请求边界：

```text
Bearer verifier
  -> verified Clerk-shaped identity (no token in context)
  -> exact X-WanWork-Tenant-ID candidate
  -> same TenantUnitOfWork.Read snapshot
  -> binding -> human principal -> active tenant membership -> human Actor
  -> /api/v1/auth/context safe projection
```

这不是生产认证完成，也不是业务路由/IM 接入完成。PostgreSQL runtime 当前仍使用空 fixture 的
reject-all fake verifier；真实 Clerk/JWKS、可信密钥轮换、conversation ACL、Agent installation、
Task/Attempt/Artifact/Needs You durable projection 和真实 provider 均保持关闭。

## 交付内容

- `RuntimeDependencies.Now` 支持受控 UTC 时钟，生产默认 `time.Now().UTC()`；
- `trustedRequestContextMiddleware` 只接受单个、无空白、严格解析的 `X-WanWork-Tenant-ID`；tenant
  只是路由候选，不是权限凭据；
- middleware 在 `TenantUnitOfWork.Read` 的同一 read snapshot 中调用
  `ResolveTrustedRequestContext`，要求外部身份 binding、global principal、active tenant membership
  和 active human Actor 全部一致；
- 新增 `GET /api/v1/auth/context`，只返回 provider/subject、principal、tenant、Actor、membership role
  和 revision 摘要，不返回 bearer token、session secret 或 authority handle；
- 缺 header/重复 header/非法 tenant 在 authority 查询前返回 malformed；无 active membership 返回
  forbidden；authority unavailable 返回 dependency unavailable；完整性异常返回 internal；
- `auth.WithTrustedRequestContext` / `TrustedRequestContextFromContext` 使用私有 context key，zero
  context 和伪造的外部 context 均不能通过。

## 验证证据

```text
cd apps/im-api
go test ./internal/auth ./internal/app ./internal/adapters/httpapi
PASS

go test ./...
PASS

go vet ./...
PASS

git diff --check
PASS
```

新增负向/正向断言包括：

1. 合法 bearer + 合法 tenant 只执行一次 tenant-scoped read，并返回 `user_alice` → `hpr_alice` →
   `ten_alpha` → `usr_alice` 的一致链；
2. 缺 tenant header 不触发 persistence read；
3. removed membership 不进入 handler，返回稳定 `CodeForbidden`；
4. context helper 拒绝 nil/zero，且只 round-trip 一个已解析的 immutable snapshot；
5. 既有 readiness barrier、bearer ambiguity、provider failure 和所有 Go package tests 保持通过。

## 仍未闭合的边界

- `authfake.Verifier` 仅用于本地测试；`cmd/im-api` 的 PostgreSQL 组合仍是 reject-all，未声称 Clerk
  JWKS signature/issuer/audience/key-rotation production proof；
- tenant header 还不是正式 route/path consistency 与 host-owned realm selection 合同；真正生产入口
  需要由 edge/router 注入已验证的 tenant candidate，并在 request context 中绑定 realm/profile revision；
- `/api/v1/auth/context` 是只读 identity introspection，不是 conversation/message/Task business API；
- `TenantUnitOfWork.Read` 已能提供同一 snapshot 的 identity authority，但尚未将 conversation membership,
  access, Agent installation, mandate, capability, budget 和 action-time PEP 接到请求；
- 不改变 Gate A–E：真实认证、durable business projection、worker dispatch、provider exchange、
  multi-instance recovery 和外部 outbound 仍为 NO-GO。

## 下一步

1. 在不引入真实凭据的前提下，为 conversation/actor/membership/access resolver 补只读 repository
   contract 与跨 tenant/property negative matrix；
2. 把 context 作为 action-time resolver 的输入，先闭合一个 durable conversation read route，禁止
   caller-supplied Actor/role 覆盖 authority snapshot；
3. 再进入 authenticated event read、cursor/snapshot 与 Task/Agent thread projection；每批保持
   fake/zero-network、专项门禁和独立 commit；
4. 只有真实 Clerk/JWKS、PostgreSQL durable projection、provider contract 和 crash/recovery 证据均齐备，
   才重新评估 Gate A–E，不因本节点的 HTTP 200 或 fake 测试升级生产结论。
