# Agent Store provider-effect PostgreSQL schema checkpoint（2026-08-31）

状态：`local_pending`。按当前调度，本阶段先保存在本地仓库，Notion/语雀在截止时间前统一上传；没有向飞书或企微发送消息。

## 本阶段结果

Commit `8114bc0` 新增 PostgreSQL migration 0014：

`apps/im-api/internal/platform/postgres/migrations/sql/0014_agent_provider_effect_outbox.up.sql`

建立 `wanwork_im.agent_provider_effects`，用于把外部 IM/provider 副作用从 Agent Store 的平台状态中分离出来。表的最小持久化面包括：

- tenant、可选 workspace、installation、effect、effect kind、provider realm 和可选 provider subject；
- tenant 内唯一的 `operation_key`，以及只保存引用和 SHA-256 的 `request_ref`/`request_sha256`；
- `queued | sent | committed | replayed | unknown | failed` 六态、尝试次数和 lease 到期时间；
- receipt digest、provider external id、错误码和 sent/commit 时间线；
- 创建/更新时间，以及只保存 digest 的 `lease_token_digest`。

## 数据完整性边界

数据库约束与本地 `ProviderEffectRecord.Validate` 合同保持一致：

1. `tenant_id + effect_id` 为主键，`tenant_id + operation_key` 为唯一键，避免同一副作用在同一租户内重复派发。
2. 所有 opaque id 限制为 1–256 字节的受限 ASCII grammar；effect kind 和 state 使用闭集，防止未经评审的 provider 操作或状态进入数据库。
3. request/receipt/lease digest 必须是小写 64 位十六进制；receipt digest 与 external id 必须成对出现。
4. `queued` 不得有尝试或 lease；`sent` 必须有尝试、发送时间、lease digest 和未过期 lease；`failed`/`unknown` 可重试或等待人工/readback，但不得带 lease；`committed`/`replayed` 必须有 receipt evidence 和 committed timestamp。
5. `unknown` 不会被数据库约束自动升级为 committed；其 receipt evidence（若已有）仍只作为证据，最终状态需要显式 reconcile。
6. 时间线约束保证 first sent、last attempt、commit 和 lease expiry 不会倒退；installation/workspace 使用 tenant 复合外键，删除采用 `RESTRICT`。

## 隔离与权限

- 表启用并强制 `ROW LEVEL SECURITY`。
- policy `agent_provider_effects_exact_tenant` 同时对 `USING` 和 `WITH CHECK` 绑定 `current_setting('wanwork.tenant_id', true)`。
- authority access manifest 将该关系加入完整对象清单和 runtime read 清单；runtime 目前只有 `SELECT`，没有表级写权限。
- `0014` 的 down migration 先删除 due index，再删除表；未使用 `IF NOT EXISTS` 或宽泛 `CASCADE`。
- schema 中没有 provider token、API key、`ext_info`、原始请求正文、连接串或 payload 字段；worker 只能通过受控 `request_ref` 重建请求。

## 验证证据

`f641ffb` 增加静态 schema contract 测试，覆盖字段、主键/操作键、外键、状态形状、digest、RLS、due index、authority manifest 和 down migration。当前通过：

```text
cd apps/im-api
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./internal/platform/postgres/migrations -count=1
```

本机没有设置 `WANWORK_TEST_POSTGRES_ADMIN_URL`，因此本轮未声称真实 PostgreSQL integration test 已运行；migration runner 的现有 integration tests 会在具备测试数据库时覆盖完整 apply/repeat/postcondition 流程。

## 尚未完成的生产边界

0014 只建立 schema/RLS/index/authority contract，尚未提供：

- 与 `TenantUnitOfWork.Execute` 同事务的 outbox enqueue function；
- tenant-bound provider-effect repository、lease claim/receipt/reconcile SQL API；
- provider worker、真实 RongCloud adapter、callback authenticity/readback；
- installation/offboard CAS 与 required effect receipts 的事务绑定。

下一小步应单独新增 function-only writer migration（建议 0015），然后实现 repository/UoW vertical slice；在这些完成前，不能把本地 file fixture 或本 migration 宣称为 IM 生产闭环。
