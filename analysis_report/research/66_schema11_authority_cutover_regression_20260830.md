# migration 11 后 authority cutover 回归修复（2026-08-30）

## 结论

PostgreSQL message projection migration 11 注册后，`authoritycutover` 测试夹具仍固定使用
`ToSchemaVersion=10`。`BuildPlan` 按设计要求目标 schema 必须等于当前 catalog 尾版本，因此旧夹具被
fail-closed 拒绝；这不是运行时代码回归，也不是放宽校验的理由。

## 修复

- `plan_test.go` 的 `validPlanInput` 改为 `ToSchemaVersion: 11`；
- 重新计算并固定 `TestPlanGoldenDigest`：
  `sha256:8c5958737a8b78eb98d4c1684866016b7bb41c5c9db2c5605663fef9b877b503`；
- 重新计算并固定 `TestPreflightReportGoldenDigest`：
  `sha256:8b7673f1d76ea0093a6525a20252c24bc75fa6619a043bb3ef8d55a16768cd9b`；
- 未修改 `BuildPlan` 的 fail-closed 语义，未引入 caller-supplied migration digest 旁路。

## 验证

在 `apps/im-api` 模块执行并通过：

```text
go test ./internal/platform/postgres/authoritycutover -count=1  pass
go test ./...                                                    pass
go vet ./...                                                     pass
git diff --check                                                 pass
```

代码提交：`af3bd43 fix(postgres): refresh authority cutover plan after schema migration`。
已推送：`origin/mainline_continue_quantum_entanglement`。
精确备份：`backup_0830_174637`，同样已推送远端。

## 当前边界

migration 11 只完成 heads/snapshots schema、RLS、catalog/access-manifest/postcondition 注册和
inactive reader 候选。projector writer、checkpoint 与 projection 同事务、shadow equality、
crash/restore/rollback 证据仍未完成；默认读取继续使用 bounded EventStore replay，Gate A–E 和真实
IM/outbound 继续关闭。
