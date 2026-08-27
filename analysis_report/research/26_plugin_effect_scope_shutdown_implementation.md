# Plugin Effect Scope Shutdown：迟到资源不能逃逸回收

> 状态：W1 P1-2 已在 `0f00b47` 实现。
>
> 边界：本阶段冻结 effect 注册与精确重试语义；callback panic、忽略 context 的强制隔离和 Host
> lifecycle callback 持锁仍在后续提交。

## 1. 研究证据与产品合同

`2output/more/deepseek-harness/research_report.md:203-216` 提炼 Cordis temporal composability：组件移除时
必须完整撤销副作用；DSH 的 Service/event/effect 随插件卸载回滚。
`deepseek-harness/research_report.md:793-840` 又把残留 listener、timer、进程、MCP、credential 与网络
列入生产风险。Agent 安全治理报告
`tech-agent-security-governance/research_report.md:267-276,282-307` 要求 runtime enforcement、kill、撤回、
隔离与 cleanup 形成实际执行点，而不是只有声明。

由此派生的硬合同是：Host 一旦开始 shutdown，就不能再接受不在本轮 cleanup snapshot 中的资源；
cleanup 失败可以重试，但不能把 scope 重新打开。

## 2. 失败模式

旧实现流程是：

```text
cleanup 开始
  -> 复制 effects
  -> 解锁并执行 callbacks
  -> 期间另一个 goroutine 仍可 Defer(new effect)
  -> 只删除旧 snapshot 中成功项
  -> Host 进入 stopped，但 new effect 永久残留
```

插件也可能在 Drain/Stop 中获取新 listener/lease，或 cleanup callback 递归注册/递归 cleanup。只靠“插件
不应这样做”无法形成生产不变量。

## 3. 新状态机

```text
open
  Defer allowed
  beginClosing / cleanup
        |
        v
closing
  Defer denied
  one cleanup execution at a time
  failure -> retain only failed effects -> closing
  retry success -> closed
        |
        v
closed
  Defer denied
  cleanup idempotently succeeds
```

`stopPlugins` 在调用任何 Drain 前，先把全部 started plugin effect scopes 转为 `closing`。因此：

- Drain/Stop 不能注册新 effect；
- cleanup snapshot 形成前已经关闭 registration；
- callback 内的 `Defer` 失败；
- concurrent/recursive cleanup 返回 `ErrInvalidEffect`，不会重复执行同一 callback；
- 成功 callback 从 scope 删除，失败 callback 原序保留；
- retry 只运行保留的失败 callback；
- 空 scope cleanup 也进入 `closed`。

## 4. 可复核验收

| 测试 | 证明 |
|---|---|
| `TestEffectScopeRejectsLateRegistrationDuringAndAfterCleanup` | closing、cleanup-in-progress、closed 三个时点均拒绝迟到注册；closed cleanup 幂等 |
| `TestEffectScopeRejectsRecursiveCleanup` | callback 不能递归执行同一 scope |
| `TestStopPluginsClosesEffectRegistrationBeforeDrain` | Host 在 Drain 前关闭注册，初始 effect 仍完整清理 |
| `TestEffectScopeRetainsOnlyFailedCleanupForRetry` | 失败后 scope 不重开；retry 只执行失败项，成功项不重复 |
| 既有 Host rollback/retry suite | start/ready/cleanup failure 与取消 context 的原语义保持 |

通过：

```text
go test ./apps/im-api/...
go test -race ./apps/im-api/internal/plugins
go vet ./apps/im-api/...
git diff --check
```

## 5. 仍未完成

1. Host `Start`/`Stop` 当前仍在持有 Host mutex 时调用 plugin callback，恶意 reentrancy 可死锁；
2. `callWithTimeout` 只传递 context，callback 忽略 context 时不能强制返回；
3. callback panic 尚未统一转换为安全错误并保证 rollback；
4. trusted built-in 与第三方 plugin 仍需不同 process/UID/container/microVM 隔离等级；
5. effect label 仍只是进程内清理 identity，不是 durable external action receipt。

下一提交先移除 Host mutex 下的外部 callback，并冻结 concurrent Start/Stop/State 和 reentrant call 的
状态机；随后再处理 panic/忽略 context 的进程隔离边界。
