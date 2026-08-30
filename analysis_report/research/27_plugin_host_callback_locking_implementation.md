# Plugin Host Callback Locking：外部回调不占用宿主状态锁

> 状态：W1 P1-3 已在 `3b8e02e` 实现。
>
> 边界：本阶段解决宿主 mutex 下调用 plugin callback 导致的死锁、状态不可观察与生命周期所有权争用；
> callback panic 已在后续提交 `2d97f0a` 完成，证据见专题 28；无视 context 的无限阻塞、第三方代码
> 进程隔离和 durable external effect receipt 尚未完成。

## 1. 一级研究证据如何转成合同

证据根固定为：

```text
/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output/more
```

不是从框架名字反推实现，而是从以下可复核事实派生本阶段合同：

1. `deepseek-harness/research_report.md:207-216` 说明 Cordis temporal composability 要求组件卸载时
   完整撤销 effect；长期运行系统不能残留 listener、timer、service 或状态；
2. 同报告 `516-519` 记录动态 browser run 缺少自身 timeout 时可能一直悬挂；`568-570` 记录
   non-compliant producer 可卡住 teardown 并长期占用容量；
3. 同报告 `575-579` 给出更强的正面纪律：取消即使遇到不 settle 的脚本也应在有界 grace 后收敛，
   单个 lifecycle listener 失败不能饿死其他 listener；
4. `tech-agent-security-governance/research_report.md:267-276` 要求插件治理必须落到运行期 enforce、kill、
   revoke 与 quarantine，而不只是静态声明；
5. `omnigent/research_report.md:281,357-364,877-901` 记录 session/host lifecycle 错位、orphan callback、
   取消与真实副作用不一致会导致 session 卡死或状态错误。

由此得到 W1 的最小硬合同：即使当前只加载平台准入的可信内建插件，宿主也不能在调用插件代码时
占用自身生命周期状态锁；慢插件可以占用唯一 lifecycle owner，但不能阻止其他线程观察状态，也不能
让误重入变成永久死锁。第三方/不可信插件仍必须在 W4/W7 使用独立进程、UID、container 或 microVM，
本阶段不能被解释成第三方代码已经安全。

对应 `docs/wanwork_im/RESEARCH_TRACEABILITY.md` 的 RQ-008 与 RQ-042。

## 2. 修复前的失败模式

旧 `Host.Start` 和 `Host.Stop` 从状态检查开始一直持有 `Host.mu`，并在锁内调用：

```text
Factory.Configure
Instance.Start
Instance.Ready
Instance.Drain
Instance.Stop
effect cleanup callback
```

因此任何 callback 调用 `Host.State()`、误调用 `Host.Start()`/`Host.Stop()`，都会等待当前 callback 自己
持有的 mutex。调用栈无法返回，timeout context 也无法让宿主抢回这把锁。并发运维线程同样无法区分：

- 宿主正在合法启动或停止；
- callback 已经卡死；
- 生命周期调用只是等待锁，还没有被状态机拒绝。

这会把一个局部插件错误放大为宿主控制面死锁。

## 3. 新的锁与所有权协议

每个公开 lifecycle 操作遵守同一模式：

```text
lock
  validate current state
  claim unique lifecycle ownership by publishing starting/stopping
  snapshot host-owned inputs
unlock

  invoke plugin callbacks without Host.mu

lock
  publish ready/stopped/failed and retained started set
unlock
```

状态机保持：

```text
new --Start owner--> starting --all ready--> ready
                         |
                         +--failure--> stopping --rollback--> failed

ready --Stop owner--> stopping --cleanup success--> stopped
failed + retained resources --Stop owner--> stopping --retry success--> stopped
                                             |
                                             +--failure--> failed
```

冻结的不变量：

1. `starting` 和 `stopping` 是公开可观察状态，也是唯一 lifecycle owner 的 claim；
2. 处于这两个状态时，reentrant/concurrent `Start` 或 `Stop` 立即返回 `ErrInvalidLifecycle`；
3. 只有已经完全进入 `stopped` 后，重复 `Stop` 才幂等成功；并发 Stop 不能伪装成“已经完成”；
4. 每个 plugin 在调用其 `Start` 前先进入 host-owned `started` 列表，因此本插件半启动失败也进入 rollback；
5. Stop/rollback 在锁内克隆 `started`，锁外按逆序执行 Drain、Stop 和 effect cleanup；
6. rollback callback 观察到的是 `stopping`，最终结果才发布为 `failed`；
7. callback 无权改变生命周期状态；它只能通过返回值让唯一 owner 决定后续转换。

## 4. 可复核验收证据

| 测试 | 证明 |
|---|---|
| `TestLifecycleCallbacksObserveStateWithoutHostLockAndRejectReentrancy` | Configure/Start/Ready/Drain/Stop/cleanup 均能调用 `State()`；每个 callback 内重入 Start/Stop 都快速、确定地拒绝 |
| `TestBlockedStartKeepsLifecycleStateObservableAndSingleOwned` | plugin Start 阻塞时可立即观察 `starting`；第二个 Start 和 Stop 不等待 callback，且不能抢占 owner |
| `TestBlockedStopKeepsLifecycleStateObservableAndSingleOwned` | Drain 阻塞时可立即观察 `stopping`；第二个 Stop 被拒绝，Drain 只有一次调用 |
| `TestRollbackCallbacksObserveStoppingStateWithoutHostLock` | Start 失败后的 Drain/Stop/cleanup 都在 `stopping` 下运行，最终状态为 `failed` |
| 原有 lifecycle/effect suite | 逆序 shutdown、取消后独立 cleanup context、失败项精确重试和 effective activation 语义不回退 |

通过门禁：

```text
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go test ./apps/im-api/... -count=1
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go test -race ./apps/im-api/internal/plugins -count=1
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go vet ./apps/im-api/...
git diff --check
```

## 5. 没有被本提交夸大的能力

1. `callWithDeadline` 只把 deadline 放入 context；callback 忽略 context 时，Go 无法安全强杀 goroutine；
2. `Factory.Configure` 接口当前没有 context/timeout；
3. 当前 goroutine callback panic 已由 `2d97f0a` 转成固定、无 payload 的 error 并保证 rollback/继续
   cleanup；插件自行创建的 goroutine、fatal runtime error 和 process crash 仍未隔离；
4. 可信内建 callback 仍在 API 进程内运行，CPU/memory exhaustion 和进程级故障半径未隔离；
5. effect cleanup identity 是进程内对象，不是跨重启 action receipt，也不能证明外部副作用已撤销；
6. 本阶段没有开放真实融云 outbound，也没有发送任何飞书、企微、机器人或 webhook 消息。

## 6. 下一步顺序

1. lifecycle callback panic 安全转换已在 `2d97f0a` 完成，证据见
   [`28_plugin_lifecycle_panic_containment_implementation.md`](28_plugin_lifecycle_panic_containment_implementation.md)；
2. Timeout honesty 已在 `eafd3da` 冻结，证据见
   [`29_plugin_lifecycle_cooperative_deadline_contract.md`](29_plugin_lifecycle_cooperative_deadline_contract.md)；
3. 下一步为第三方/不可信 plugin 冻结 process/UID/container/microVM 隔离与 supervisor kill boundary；
4. 随后实现明确标记为 volatile 的 deterministic MemoryFake EventStore，不能冒充 W2 持久化。
