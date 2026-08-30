# Shadow equality canary runtime wiring（2026-08-30）

## 交付

`1e94f8d` 将 provider-neutral `CompareMessageReaders` 接入 Go HTTP composition：

- `WANWORK_IM_MESSAGE_SHADOW` 是显式开关，默认关闭，接受空值/`false`/`true`，其它值 fail closed；
- PostgreSQL composition 在开关打开时构造 replay reader + materialized reader，并把比较 callback 注入
  `RuntimeDependencies`；primary 仍是 bounded EventStore replay；
- 认证消息 route 在首个 page（空 cursor）通过同一已解析的 tenant/conversation/access revision
  运行 shadow；两边 cursor 完全独立，不交换 opaque cursor；
- mismatch 直接返回 internal，store unavailable 映射 dependency unavailable，不把 replay 结果当作
  fallback；带 cursor 的后续 page 不重复执行全量 shadow，避免每页放大 4096-page bounded replay；
- ACL 校验和 shadow 均在读取业务 page 前完成，shadow 失败时 primary repository 不被调用。

## 验证

Go app/config 专项通过，新增测试证明 mismatch 会阻断 primary read；此前真实 PG18
projector/reader integration（research/69）通过。默认配置 snapshot 明确返回
`messageShadowEnabled=false`。

## 边界

本节点只提供可审计的 opt-in runtime seam；不会自动 backfill、不会切换 materialized primary、不会
连接真实 IM provider，也不构成 production cutover。进入生产前仍需真实 applied-schema/权限/备份
证明、故障矩阵、长期 equality telemetry、rollback receipt 和 Gate A–E 审批。
