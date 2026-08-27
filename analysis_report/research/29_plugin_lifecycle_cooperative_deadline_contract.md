# Plugin Lifecycle Cooperative Deadline：deadline 不是强杀

> 状态：W1 P1-5 已在 `eafd3da` 冻结。
>
> 边界：本阶段把 in-process lifecycle 的 timeout 语义改成诚实、可测试的 cooperative deadline；
> 没有实现第三方插件进程、supervisor grace/kill、cgroup 或 microVM，也没有宣称能终止忽略 context 的代码。

## 1. 一级研究证据

证据根：

```text
/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output/more
```

直接证据：

1. `deepseek-harness/research_report.md:516-519` 记录 browser run 没有自身 timeout 时可能一直悬挂；
2. 同报告 `568-573` 记录 producer cancel 后不 settle 会卡住 teardown 并长期占用容量；
3. 同报告 `575-579` 说明真正的强制有界收敛依赖 worker-thread provider 可终止 worker，不能从一个
   cancel signal 推导 callback 已退出；
4. `deepseek-harness/research_report.md:813-816` 将动态插件悬挂和 Jobs 取消不 settle 列为 P1 风险；
5. `omnigent/research_report.md:355-366` 区分 provider cancel、停止读流、SIGINT、child process 继续、
   callback orphan 与外部副作用已经发生；
6. `omnigent/research_report.md:1240-1260` 的 failure matrix 要求 interrupt 明确“模型停/工具未停/
   副作用未知”，并要求 managed host 启动后不注册时 timeout 清理孤儿资源。

因此本阶段不能通过在同 goroutine 上调用 `context.WithTimeout`，就宣称获得了 DSH Workflow 那种
worker-thread 强制 settle。对应 RQ-008、RQ-011、RQ-012 与 RQ-042。

## 2. 四个不能混用的事实

```text
deadline expired
  != callback returned
  != process terminated
  != external effect did not happen
```

- deadline expired：context 的 `Done()` 已关闭；
- callback returned：插件代码合作地返回控制权；
- process terminated：supervisor 从 OS/容器层确认执行主体退出；
- external effect did not happen：必须有 provider negative finality 或 reconcile 证据。

把这四者压成一个 `timeout` 会产生两类生产事故：宿主以为资源已释放而重复启动；Action Plane 以为
写操作失败而盲重试，导致重复消息、付款、发布或数据库写。

## 3. 代码合同

### 3.1 `callWithTimeout` 更名为 `callWithDeadline`

原名暗示函数能在 timeout 时返回。实现实际是同 goroutine：

```text
context.WithTimeout
  -> operation(ctx)
  -> 等 operation 自己返回
```

现更名为 `callWithDeadline`，注释明确：

- 只提供 cooperative cancellation；
- 不强制 callback 返回；
- non-cooperative/untrusted code 需要 process isolation。

`LifecycleTimeouts` 类型注释同步冻结同一语义。Manifest 的 Start/Ready/Drain/Stop duration 仍进入
canonical digest 和 admission diff，但它是 callback context deadline，不是 OS execution limit。

### 3.2 Deadline 后 lifecycle owner 不释放

Callback 未返回时：

- Start 路径保持 `HostStateStarting`；
- Drain/Stop 路径保持 `HostStateStopping`；
- State 仍可观察；
- reentrant/concurrent Start/Stop 立即返回 `ErrInvalidLifecycle`；
- Host 不启动第二个 owner，不提前发布 failed/stopped，不假装 cleanup 完成。

Callback 最终返回 `context.DeadlineExceeded` 后，唯一 owner 才继续 rollback 或发布 cleanup failure。

## 4. 可复核验收

| 测试 | 证明 |
|---|---|
| `TestLifecycleTimeoutTriggersRollback` | 合作 callback 在 context deadline 后返回，Host 进入 rollback |
| `TestLifecycleDeadlineIsCooperativeDuringStart` | callback 已观察 deadline 但未返回时，外层 Start 仍阻塞、State 为 starting、并发 Start/Stop 被拒绝；释放 callback 后才 failed |
| `TestLifecycleDeadlineIsCooperativeDuringStop` | Drain 已观察 deadline 但未返回时，外层 Stop 仍阻塞、State 为 stopping、并发 Stop 被拒绝；释放后才 failed |
| P1-3/P1-4 suites | Deadline 诚实语义不破坏 lock-free callback、single owner、panic containment 与继续 cleanup |

门禁通过：

```text
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go test ./apps/im-api/... -count=1
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go test -race ./apps/im-api/internal/plugins -count=1
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go vet ./apps/im-api/...
git diff --check
```

## 5. 生产隔离设计输入

后续第三方执行边界至少需要：

1. `ProcessInstance` 与 generation/fence，不能只保存 goroutine；
2. cooperative cancel → bounded grace → supervisor kill → wait/reap 的明确状态机；
3. 独立 UID/container/microVM、只读 rootfs、最小 mount、default-deny egress、resource quota；
4. stdout/stderr、panic、exit signal 与 Artifact 的分类、限额、脱敏和证据链；
5. kill 只证明进程退出，不证明外部 effect 未发生；Action receipt 缺失时进入 unknown/reconcile；
6. API/Gateway 不持 Docker socket 或 privileged runtime 控制权，executor broker 独立准入；
7. cleanup/reap 超时形成 operator-visible quarantine，不把残留资源静默标成 stopped。

## 6. 未完成与下一步

1. 当前仍只运行可信内建插件；第三方 executable package 尚未开放；
2. `Factory.Configure` 没有 context，合同仍要求它是无外部副作用的快速构造；
3. 本阶段没有进程 supervisor、kill receipt、resource accounting 或跨重启 recovery；
4. 下一小阶段先冻结第三方 `ExecutionIsolationProfile/RuntimeGrant/ProcessInstance` 的最小 Go 合同和
   hostile fake conformance tests，再选择本地 process/container/microVM provider；
5. 真实融云 outbound 继续关闭；未向飞书、企微、机器人或 webhook 发送消息。
