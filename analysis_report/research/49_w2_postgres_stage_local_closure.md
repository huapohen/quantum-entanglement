# 49｜W2 PostgreSQL 当前阶段本地收口检查点

> 状态：本地阶段收口，待批量同步 Notion。  
> 代码分支：`dev_wanwork_quantum_entanglement`  
> 代码基线：`c2e3266`（测试时 clean；后续仅新增本地证据与文档）  
> 证据边界：仅证明本机 Go 代码、migration 合同和 disposable PostgreSQL 18.6；不宣称生产数据库、远程 TLS、真实 provider 或 Notion 已更新。

## 1. 本轮完成内容

### 1.1 v10 migration 之后的权威切换夹具对齐

`0010_native_im_inbox_semantics` 将当前 migration catalog 从 v9 推进到 v10。原有
authority-cutover 测试夹具仍使用 `ToSchemaVersion=9`，因此 `BuildPlan` 按设计拒绝了旧夹具，
表现为整包 `invalid PostgreSQL authority cutover plan`。本轮将夹具推进到 v10，并更新受该
语义变化绑定的 plan/preflight golden digest。生产校验逻辑没有放宽到接受过期 schema。

### 1.2 runtime pool 权限夹具对齐

v9/v10 后数据库关系从旧夹具期望的 26 张增加为 28 张，新增关系为
`event_projection_checkpoints` 与 `native_im_inbox`；函数数量从 6 增加为 8，新增/纳入
`admit_native_im_inbox` 及其 v10 定义。runtime fixture 现在按最新 authority manifest 给
这两张表授予 runtime `SELECT`，并按 8 个受控函数授予 `EXECUTE`。这使测试夹具与代码拥有的
exact runtime access specification 一致。

### 1.3 commit-unknown fresh readback 的 deadline 边界

`ApprovalExecutionFencer` 的 fresh readback 使用 caller-independent、bounded reconciliation
context。旧循环在 timeout 已到达后仍可能再发起一次 `Load`，使 fake/真实 store 看到一个已经
取消的 read context，并在 `-race` 下产生时序性失败。本轮在每次 `Load` 前检查 reconciliation
context；到达截止时间后立即返回 not-found，不再启动新的已取消数据库调用。该改动保持 fail-closed：
仍然不会把未知 commit 当成成功，也不会返回 capability fence。

## 2. 验证矩阵

在本机 PostgreSQL 18.6（Homebrew，disposable loopback，passwordless 仅测试开关）执行：

```bash
cd apps/im-api
WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55488/postgres?sslmode=disable' \
  GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
  go test ./... -count=1 -timeout=15m

WANWORK_TEST_POSTGRES_ADMIN_URL='postgresql://127.0.0.1:55488/postgres?sslmode=disable' \
  GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
  go test -race ./... -count=1 -timeout=20m

GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off go vet ./...
```

结果：25 个 package 全部通过；normal 与 race 各 1,444 个 test pass event、0 skip、0 fail；
`go vet` 无输出且退出码为 0。此前发现的 authoritycutover 与 runtimepool 两个失败已在
本轮修复后重新执行并通过。机器可读摘要见同阶段 checkpoint：
`docs/production/evidence/checkpoints/w2-postgres-c2e3266.local-run.json`。

migration 专项和 native IM atomic bridge 专项仍由 Topic 47/48 报告覆盖；本轮没有修改已发布
的 0009 checksum，也没有连接真实 RongCloud、Clerk、飞书、企微或任何外部消息通道。

## 3. Notion 与报告同步策略

为保证实现、测试和证据之间的时间顺序，本阶段采用本地优先：

1. 代码、报告、截图 manifest、Yuque 本地镜像和 report-sync checkpoint 先在当前 worktree 收口；
2. 每个代码/测试小改动独立 commit 并推送到 `origin/dev_wanwork_quantum_entanglement`；
3. 通过最终 clean-checkout 验证后，再一次性写入私人 Notion；
4. 写入完成后执行整页回读、字节摘要比对和链接核对，再把远端状态写入新的本地 checkpoint。

因此本报告和新增 checkpoint 在 Notion 未回读前必须标记为 `local_pending`。延迟同步不会影响
当前本地代码运行；相反，它避免网络、权限或页面逐段回读把测试/提交节奏打断。该策略不改变
用户已经授权的范围，也不向任何群聊或个人发送询问消息。

## 4. 当前仍然 No-Go 的生产边界

- production authority bootstrap/cutover、远程 authenticated TLS、credential rotation 和 old-session drain；
- Clerk verified claim 到 trusted tenant/request context，以及 action-time membership/permission resolver；
- provider callback signature/nonce/replay window、真实 RongCloud inbound/outbound；
- message/mention/Agent thread/Task/Attempt/Artifact/Acceptance 的完整 durable vertical slice；
- outbox、action receipt、effect-unknown reconcile、dead-letter、backup/restore/kill-9 演练；
- 性能容量、长时间 endurance、跨环境 reproducible release 和 CI immutable artifact；
- Notion/语雀远端实时一致性（本地 bundle 不能冒充远端回读）。

本地产品可以继续用零网络 fake/loopback 入口验收，但不得把本地 evidence 当成生产 readiness 或
真实 provider 送达证明。

## 5. 下一阶段入口

继续主线前，先按 `docs/wanwork_im/IMPLEMENTATION_PLAN.md` 的“W2 接真实 IM 前的 P0 顺序”执行：
先完成 Gate A0 production authority 与 rotation 合同，再做 Clerk trusted request context、
active membership/access resolver、显式 draining 与 rotation/restore 演练；之后才进入 durable
message/mention/Agent thread 与 W3 provider sandbox。Notion 批量同步属于本阶段证据收口动作，
不应插入这些代码门禁之间。
