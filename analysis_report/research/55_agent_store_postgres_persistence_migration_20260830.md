# Agent Store PostgreSQL 持久化 migration（2026-08-30）

状态：`local_pending`。本文件与代码先保存在本地/Git，按当前调度不上传 Notion。

## 结论

第 11 个 PostgreSQL migration `0011_agent_store_control_plane` 已落地到
`apps/im-api/internal/platform/postgres/migrations/sql/`，把 Agent Store 从“仅内存 synthetic
状态”推进到可供 durable repository 接入的数据库边界。它只保存可审计的 catalog、release、Trust
Passport 和 installation 快照，不保存模型密钥、provider 密钥、连接串或其他秘密材料。

随后在 `77e9ac0` 增加了 `internal/agentstore/codec.go`：definition、release、Trust Passport 和
installation 都有严格 canonical JSON 编解码；解码会拒绝未知字段、尾随值、非 canonical 空白、无效
身份/摘要/时间，并通过现有构造器重验。installation 解码必须提供与行一致的 Trust Passport，不能只凭
数据库里的 release ID 把普通记录提升成可执行授权。

在本轮增量中，第 12 个 migration `0012_agent_store_write_functions` 增加了四个精确参数的
`SECURITY DEFINER` CAS 函数：definition、release、Trust Passport 和 installation revision。runtime
只获得这些函数的 `EXECUTE` 与五张 Agent Store 表的 `SELECT`，不获得 Agent Store 原始
`INSERT`/`UPDATE`/`DELETE` 权限；installation 函数在一个事务内写入 head 与 snapshot，并保留
deferred current-snapshot 外键。

Go PostgreSQL repository/UoW 已接入 `TenantRepositories.AgentStore()`：所有四类写入都经由
0012 函数，读取会重建并重验 domain snapshot，CAS 冲突映射为独立 typed error，数据库错误保持
脱敏。JSONB object-key 顺序按 PostgreSQL 18 的读回语义处理，但数组元素、字段集合、身份、摘要、
时间和 domain 构造器仍逐项校验；timestamptz 读回统一归一到 UTC，避免机器本地时区改变授权语义。

action-time capability resolver 已提升为 `agentstore.ResolveGrantedCapabilities` 公共契约，localdemo
安装路径现在使用同一实现：nil 请求表示完整 reviewed 集合，显式集合必须是 Trust Passport 当前允许
能力的严格子集，输入会 canonical 排序并拒绝重复/空集合；每次决策都绑定调用时的 UTC 时钟并重新
检查 Passport 有效期。这样后续 durable runtime 不需要复制一套“安装时允许、执行时另一套”的能力解析逻辑。

durable command 入口也已收口：`imstore.NewAgentStoreCommand` 只接受 `agent.*` 命名空间，
`UnitOfWork.ExecuteAgentStore` 复用现有 serializable transaction、advisory lock、receipt 写函数、
重放读取和 commit-unknown fresh-connection reconcile。真实 PostgreSQL 集成测试现在证明同一
Agent Store create command 的第二次调用返回 `replayed=true`、同一 result digest，且 operation body
不会再次执行；这一步把“已有 receipt 表”推进成 Agent Store 明确可调用的 durable seam。

这不是“生产 Agent Store 已完成”的声明。当前仍缺少安装命令的 durable receipt、action-time resolver、
provider outbox/reconcile、真实 Clerk/RongCloud 适配器、灾备恢复和完整 IM provider effect gate；migration
与 repository 是这些组件可以共同依赖的持久化契约。

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

- `migrationSpecs` 现在是连续的 `0001..0012`，checksum 仍由 migration catalog 计算。
- migration runner 新增第 11 版 postcondition：五张表存在、均为强制 RLS，并且各有精确租户策略。
- authority access manifest 将五张表纳入 owner/runtime 对象清单；runtime 只有只读表权限，不能因表出现而
  获得写入或 `MAINTAIN` 权限。
- migration 12 postcondition 会精确核对四个 Agent Store 写函数的参数、返回值、owner、`plpgsql`、
  `VOLATILE`、`STRICT`、`SECURITY DEFINER`、`PARALLEL UNSAFE`、固定 `search_path` 与安全 ACL；
  authority access integration fixture 同步转移四个函数 owner 并授予 runtime `EXECUTE`。
- `0011` DownSQL 先移除 installation head 的 deferred FK，再按依赖顺序删除 snapshot/head/catalog，
  可在 disposable 数据库完整回滚。
- 旧的 authority cutover 测试 fixture 已改为读取当前 catalog 长度；golden digest 按新 migration catalog
  重新冻结，避免测试继续隐含旧版本 10。

已执行：

```text
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./... -count=1
```

结果：常规本地门禁在 authoritycutover、Agent Store、IM Store 等包通过。此前无环境变量时 PostgreSQL
integration 会按设计 skip；本轮另外使用本机 PostgreSQL 18.6 disposable loopback 实例显式执行了
`WANWORK_TEST_POSTGRES_ADMIN_URL=postgresql://<redacted>`：migration 全量、authority access、DownSQL、
runtime ACL、repository definition/release/Passport/installation 创建与读取、状态 CAS 和 stale revision
冲突均通过。连接串、角色密码和任何密钥未写入报告、日志或提交。

另外已用本机 PostgreSQL 18.6 的临时实例逐个执行 `0001..0012` 的全部 `up.sql`，并单独回放 0012
函数调用；所有 DDL、复合外键、JSONB 检查、5 条 Agent Store 租户策略和 function-only 写入均成功落库，
实例随后停止。这个 disposable smoke 仍不替代线上拓扑、真实身份、provider effect、灾备恢复和备份演练。

最终门禁复核（2026-08-30）：

- `WANWORK_TEST_POSTGRES_ADMIN_URL=postgresql://<redacted> go test ./apps/im-api/internal/platform/postgres/... -count=1`：authoritycutover、connectionpolicy、eventstore、imstore、migrationrun 全部通过；此前 eventstore fixture 漏授共享 runtime read 表的问题已修正并由 `TestPostgresEventStoreAgainstPostgres` 覆盖。
- `cd apps/im-api && go test ./... -count=1`：全部 Go package 通过。
- `cd apps/im-api && go vet ./...`：通过。
- `cd clients/im-web && npm run build`：TypeScript/Vite production build 通过。
- `WANWORK_IM_VERIFY_PORT=18146 ./scripts/verify_web_first.sh`：构建、HTTP envelope、Agent Store 安装/幂等重放/撤权、统一 action-time capability resolver、子群隔离、Workboard 审阅闭环和零网络 synthetic 通过。

本阶段远端备份：`dev_wanwork_quantum_entanglement` 已推送至 `origin`，当前 HEAD 为 `63106e2`；此前的
`backup_0830_211508` 指向 resolver 之前的 `aa94515`，可用于精确回退。新增可回溯小阶段 commit 为：
`c69af4c`（共享 action-time capability resolver 与测试）、`63106e2`（resolver 证据与最新 Web gate 文档）、
`de8d2ae`（Agent Store durable command receipt 入口与 PostgreSQL replay 证据）。当前工作分支 HEAD 为
`de8d2ae`。

## 下一步顺序（仍本地 pending）

1. 将 localdemo 安装/撤权命令切换到 repository seam，并为安装/撤权补 durable command/effect receipt；
   保留 fake provider 作为零网络验收 fixture。
2. 接入 action-time capability resolver、provider outbox/reconcile、commit-unknown fresh readback 和
   灾备恢复演练，确保 Agent Store 决策不会被静态安装状态代替。
3. 完成真实 Clerk/RongCloud 身份与 provider effect gate 后，再决定是否把真实 IM provider 接入 acceptance
   gate。

## 证据边界

本阶段证明的是“数据库 schema、function-only write boundary 和 tenant-bound repository 已有可运行落点”，
不是“Agent Store 已能在生产运行”。在 durable command/effect receipt、action-time policy、真实身份、
provider effect、灾备恢复和安全 gate 全部通过前，发布文档必须继续标记为 No-Go。
