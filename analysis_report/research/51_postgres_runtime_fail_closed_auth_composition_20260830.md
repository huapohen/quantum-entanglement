# PostgreSQL 运行组合的拒绝式认证收口证据（2026-08-30）

> 提交：`89210ac`
> 分支：`mainline_continue_quantum_entanglement`
> 远端：`origin/mainline_continue_quantum_entanglement`

## 问题

`cmd/im-api` 在配置 PostgreSQL runtime 时已经要求 `app.NewRuntime` 提供 `auth.Verifier`，但
组合入口没有注入它，导致服务在数据库模式下启动即返回 `ErrInvalidRuntimeDependencies`。这不是
测试环境能忽略的路径：readiness、迁移后运行和停机生命周期都无法进入。

## 修复

新增 `newRejectAllVerifier()`，使用 provider-neutral fake auth profile，但 fixture 集合为空：

- 没有静态 token、API key 或外部网络调用；
- 健康检查和数据库 readiness 可以正常组合；
- 任意 bearer token 均返回 `ErrInvalidToken`；
- 服务关闭时显式关闭 verifier；
- 真实 Clerk/JWKS 未接入前，不能把该 verifier 当作认证完成证明。

## 专项证据

```text
(cd apps/im-api && go test ./cmd/im-api ./internal/app ./internal/adapters/auth/fake)
exit 0

(cd apps/im-api && go vet ./cmd/im-api ./internal/app ./internal/adapters/auth/fake)
exit 0
```

新增测试确认 profile 有有效 realm 且未配置 token 时拒绝标准形态 bearer token。该变更不改变
真实 IM outbound、PostgreSQL durable projection、tenant authority 或 Gate A–E 状态。
