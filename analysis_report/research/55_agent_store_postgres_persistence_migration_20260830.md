# Agent Store PostgreSQL 持久化 migration（2026-08-30）

状态：`local_pending`。本文件与代码先保存在本地/Git，按当前调度不上传 Notion。

## 结论

第 11 个 PostgreSQL migration `0011_agent_store_control_plane` 已落地到
`apps/im-api/internal/platform/postgres/migrations/sql/`，把 Agent Store 从“仅内存 synthetic
状态”推进到可供 durable repository 接入的数据库边界。它只保存可审计的 catalog、release、Trust
Passport 和 installation 快照，不保存模型密钥、provider 密钥、连接串或其他秘密材料。

这不是“生产 Agent Store 已完成”的声明。当前仍缺少 Go repository/UoW、安装命令的 durable receipt、
action-time resolver、provider outbox/reconcile 和真实 Clerk/RongCloud 适配器；migration 是这些组件
可以共同依赖的第一层持久化契约。

## 持久化对象

| 表 | 作用 | 关键约束 |
|---|---|---|
| `agent_definitions` | 租户范围内的 definition claim 与展示元数据 | `agd_`/`hpr_`/`pub_` 身份、状态枚举、revision 单调字段形状 |
| `agent_releases` | immutable release 的版本、三类 SHA-256 摘要、能力/路线声明 | `agr_` 身份、非零 digest、SemVer 形状、published/draft 时间一致性 |
| `agent_passports` | release 对应的 Trust Passport 与 attestations 投影 | 与 tenant/definition/release 复合外键绑定，状态与最小三项 attestations |
| `agent_installation_heads` | installation 当前 revision 指针 | tenant + `ins_` 复合主键，current snapshot 使用 deferred FK |
| `agent_installation_snapshots` | installation 每个 revision 的不可变决策快照 | workspace/actor/principal/release 复合外键，能力/路线 JSON 数组，撤权时间形状 |

能力、prohibition、data route、attestation 和 granted capability 暂用受约束 `jsonb` 数组承载；它们
仍是声明/快照，不是凭据，也不直接授予 provider 或工具权限。后续 repository codec 必须在写入前按
`internal/agentstore` 的规范化顺序重建并校验这些数组，不能把任意 JSON 当作已批准能力。

## 租户隔离与写入边界

五张表全部启用并强制 Row-Level Security，策略只允许
`tenant_id = current_setting('wanwork.tenant_id', true)`。复合外键防止跨 tenant 的 definition、
release、workspace、actor 或 installation 拼接。安装 head 到 snapshot 的 current revision 关系使用
`DEFERRABLE INITIALLY DEFERRED`，支持“写新 snapshot，再推进 head”的单事务 CAS 形态。

migration 本身没有添加普通用户可执行的写函数；后续 durable repository 应仿照现有 authority function-only
模式，将 head/snapshot 写入放进精确参数的 `SECURITY DEFINER` CAS 函数，并把函数 ACL、幂等 receipt、
commit-unknown 和 fresh-connection reconcile 纳入同一个 postcondition gate。

## 迁移与验证

- `migrationSpecs` 现在是连续的 `0001..0011`，checksum 仍由 migration catalog 计算。
- migration runner 新增第 11 版 postcondition：五张表存在、均为强制 RLS，并且各有精确租户策略。
- authority access manifest 将五张表纳入 owner/runtime 对象清单；runtime 只有只读表权限，不能因表出现而
  获得写入或 `MAINTAIN` 权限。
- 旧的 authority cutover 测试 fixture 已改为读取当前 catalog 长度；golden digest 按新 migration catalog
  重新冻结，避免测试继续隐含旧版本 10。

已执行：

```text
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./... -count=1
```

结果：IM API 全部 Go package 通过。PostgreSQL integration test 需要显式
`WANWORK_TEST_POSTGRES_ADMIN_URL`，当前环境未配置，因此本次没有伪造 PG18 实证；上线前必须在一次 disposable
PostgreSQL 18 环境执行完整 migration、postcondition、RLS/ACL、rollback fixture 和重启读回。

另外已用本机 PostgreSQL 18.6 的临时实例逐个执行 `0001..0011` 的全部 `up.sql`；所有 DDL、复合外键、
JSONB 检查和 5 条 Agent Store 租户策略均成功落库，实例随后停止。这个 raw DDL smoke 只证明 SQL 可应用，
不替代带 owner/runtime role、migration runner postcondition 和真实数据读写的集成门禁。

## 下一步顺序（仍本地 pending）

1. 在 `internal/agentstore` 增加严格 snapshot codec，确保 Go value 与五张表的 JSON 表示可逆、排序稳定、
   digest 不漂移。
2. 在 `internal/platform/postgres/agentstore` 实现 tenant-bound repository 与 Unit of Work；任何 CAS
   冲突、重复 receipt、commit-unknown 或 head/snapshot 不一致都 fail-closed。
3. 将 localdemo 安装/撤权命令切换到 repository seam，同时保留 fake provider 作为零网络验收 fixture。
4. 接入 action-time capability resolver、provider outbox/reconcile 后，再决定是否把真实 IM provider
   接入 acceptance gate。

## 证据边界

本阶段证明的是“数据库 schema 与 authority catalog 已有持久化落点”，不是“Agent Store 已能在生产运行”。
在 repository、真实身份、provider effect、灾备恢复和安全 gate 全部通过前，发布文档必须继续标记为 No-Go。
