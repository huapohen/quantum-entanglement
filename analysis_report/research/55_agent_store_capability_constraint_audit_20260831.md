# Agent Store durable capability 约束复核与修复

> 日期：2026-08-31（Asia/Shanghai）
> 状态：代码与测试已提交到 `dev_wanwork_quantum_entanglement`，Notion 按当前批次策略保持 `local_pending`
> 复核范围：`0011_agent_store_control_plane`、`0012_agent_store_write_functions`、Agent Store PostgreSQL adapter 与 runtime role 负向测试

## 结论

已确认并修复一个生产级 durable-boundary 缺口：`write_agent_release_revision` 和
`write_agent_installation_revision` 是 `SECURITY DEFINER` 固定写入口，runtime role 按 authority
contract 可以执行它们，但原有数据库约束只验证 `jsonb` 顶层是数组及元素数量。调用方绕过 Go
constructor 后，以下 payload 可以通过 SQL writer：

- `requestedCapabilities` 或 `prohibitions` 含非字符串或不符合 capability grammar 的值；
- requested 与 prohibited 集合存在交集；
- installation 的 `grantedCapabilities` 含非法 capability。

这类行随后会在 `CurrentRelease`/`CurrentInstallation` 的 canonical decoder 处变成 integrity
failure，导致 durable authority 已写入但不能读取；更严重时会让“请求能力”和“禁止能力”语义失真。
只依赖 API 层的 `NewReleaseSnapshot`、`NewInstallationSnapshot` 不能保护数据库函数边界。

## 修复

新增 `0013_agent_store_capability_constraints`（只加不改历史 migration）：

1. 使用 PostgreSQL `jsonb_path_exists` 在数据库内拒绝非字符串和不符合与 Go
   `ParseCapability` 相同的 ASCII grammar 的 capability；
2. 拒绝 release requested/prohibitions 交集；
3. 对 installation granted capability 应用相同 grammar 约束；
4. 增加 migration postcondition，启动/重复迁移时要求四个约束全部存在；down migration 按固定顺序移除。

约束位于表层，因此不仅保护四个 `SECURITY DEFINER` writer，也保护未来受控 owner/migrator
路径。JSONB 仍只保存 canonical codec 允许的集合；长度、排序、去重和跨 release/passport subset
仍由 Go constructor/adapter 负责，属于后续独立工作，不在本修复中扩大范围。

## 验证证据

- migration catalog 单元测试通过，新增版本 13 连续、checksum 稳定、SQL marker 和 postcondition
  已覆盖；
- Agent Store PostgreSQL 集成测试新增 runtime writer 负向场景：把 `conversation.read` 改成
  `invalid capability!` 的 canonical release payload，数据库返回 SQLSTATE `23514`，且 readback
  确认没有产生 `agr_invalid_payload` 行；未设置 `WANWORK_TEST_POSTGRES_ADMIN_URL` 时该集成套件按既有
  约定 skip；
- `git diff --check` 通过。

## 未覆盖边界

- capability 字符串最大 128 字节、route/object shape、attestation shape/有效期和
  installation grant 必须属于当前 Passport requested 且不在 prohibitions，仍需要后续 SQL
  function/normalized child-table 设计；
- Passport 的 definition/release status binding 仍需要在下一项受控 writer contract 中补上；
- 本轮未操作 Notion、语雀、飞书或企微；Notion 保持 `local_pending`，等待用户指定截止批次统一上传。
