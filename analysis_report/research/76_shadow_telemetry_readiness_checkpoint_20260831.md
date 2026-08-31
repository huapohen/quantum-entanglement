# Shadow equality telemetry 与 readiness latch 阶段检查点（2026-08-31）

## 阶段结论

本阶段把原有 default-off replay/materialized equality canary 从“单次请求 mismatch 失败”补强为
进程级、不可自动复位的 readiness 证据：只要当前进程观察到一次真实
`ErrShadowMismatch`，`/health/ready` 随后就持续 fail closed，直到运维保留证据、完成对账并替换进程。

实现提交为：

- `1f10fde feat(im): add shadow telemetry readiness monitor`；
- `79673d5 test(im): cover shadow readiness latch`。

该能力仍由 `WANWORK_IM_MESSAGE_SHADOW=true` 显式启用；未设置时不创建 monitor，也不改变当前
bounded EventStore replay primary。它不是 materialized primary cutover，也不是生产 GA 批准。

## 已交付合同

### 1. 进程内、无标识符 telemetry

`ShadowMonitor` 使用原子计数器维护以下累计值：

- `Runs`：进入 monitor 的完整 compare 尝试数；
- `Successes`：完整 equality compare 成功数；
- `Mismatches`：返回 `ErrShadowMismatch` 的 compare 数；
- `Failures`：依赖不可用、取消、非法输入等非 mismatch 失败数；
- `ComparedPages` / `ComparedMessages`：仅对完整成功的 equality run 累计已比较页数和消息数；
- `MismatchLatched`：当前进程是否曾观察到 mismatch。

快照不包含 tenant、workspace、conversation、cursor、message、provider、credential 或原始错误；
因此可以作为后续内部 metrics adapter 的安全输入，但当前没有长期 exporter、时序存储或告警系统。

### 2. mismatch sticky fail-closed

只有 `errors.Is(err, ErrShadowMismatch)` 会设置 sticky latch。latch 无 reset API，且一旦设置：

- monitor 后续即使出现 equality success，也不会恢复 readiness；
- `/health/ready` 先检查 PostgreSQL primary readiness，再检查 shadow latch；
- 数据库失败时立即返回数据库错误，不把 shadow probe 冒充数据库健康；
- 数据库恢复后，已设置的 mismatch latch 仍会返回 `ErrShadowUnhealthy`。

恢复动作必须是：保存 mismatch 证据、停止晋级、执行 replay/materialized 对账与 reconcile、替换进程，
而不是在原进程内清零计数器。

### 3. transient dependency failure 不 latch

普通 store unavailable、请求取消或其他非 mismatch 错误计入 `Failures`，但不自动设置
`MismatchLatched`。数据库可用性继续由 PostgreSQL readiness 负责；单个被取消的业务请求不能永久
关闭整个实例。该区分不把依赖失败降级为成功：原 compare 调用仍把原错误返回给请求路径。

## 验证证据

在 `mainline_continue_quantum_entanglement` 分支执行：

```bash
go test ./apps/im-api/internal/improjection
go test ./apps/im-api/cmd/im-api
go vet ./apps/im-api/internal/improjection
go vet ./apps/im-api/cmd/im-api
go test -race ./apps/im-api/internal/improjection ./apps/im-api/cmd/im-api
git diff --check
```

全部退出码为 0。测试覆盖：

- success / dependency failure / mismatch 的互斥计数；
- mismatch 后的 sticky readiness；
- `Compare()` 自动记录 equality 与 mismatch；
- 90 个并发 observer 的原子累计，并通过 race detector；
- nil monitor、nil/cancelled context 的 fail-closed；
- joined readiness 的数据库短路与 shadow latch 传播。

本轮按影响面执行定向 Go 门禁，没有因为四个 Go 文件的局部改动重跑约三千项 Python 全仓回归。

## 运维语义

1. 默认保持 shadow 关闭，materialized reader 不承接 primary 流量。
2. 在隔离或受控环境显式打开 shadow 后，观察 equality 请求与 readiness。
3. 首次 mismatch 后实例立即退出 ready 集合；禁止自动 fallback 合并两套结果，也禁止清 latch 后原地晋级。
4. 保存 scope 外部关联证据时必须走受控日志/审计系统；本 monitor 刻意不保存业务标识符。
5. 对账完成后由新进程重新开始 canary 窗口；cutover 必须有单独的 applied-schema、备份、drain、
   rollback receipt 和人工批准。

## 明确未交付

- 长期外部 metrics exporter、dashboard、SLO 与告警路由；
- durable telemetry、跨实例聚合、backfill orchestration 与自动 reconcile；
- 生产 applied-schema digest、权限、备份恢复、RPO/RTO、HA/failover 证明；
- materialized primary cutover、旧 reader drain 与 rollback receipt；
- 真实 Clerk/JWKS；
- Task、Artifact、Needs You durable projection；
- worker/provider bridge、action receipt 与 `effect_unknown` reconcile；
- 真实 IM provider、外部 outbound、飞书或企微消息发送。

## 阶段停止点

本文件是用户要求的阶段性验收边界。完成提交、远端备份、分支目录和 Notion 同步后必须停止；
不得继续 production applied-schema proof、materialized primary、真实认证、业务 projection、worker、
provider 或 outbound 实现，直到用户验收并明确下达下一阶段指令。
