# Plugin Lifecycle Panic Containment：失败隔离、继续回收与 payload 脱敏

> 状态：W1 P1-4 已在 `2d97f0a` 实现。
>
> 边界：本阶段只隔离发生在当前 lifecycle callback goroutine 内的 panic。插件自行创建的 goroutine、
> `runtime.Goexit`、进程崩溃、OOM/fatal signal、忽略 context 的永久阻塞和第三方代码强隔离不在本提交内。

## 1. 从一级调研到实现合同

一级证据根：

```text
/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output/more
```

直接相关证据：

1. `deepseek-harness/research_report.md:575-579` 记录 Workflow 的可靠性纪律：取消后即使脚本不
   settle 也要有界收敛；单个 lifecycle listener 抛错必须隔离，不能饿死后续 listener；
2. 同报告 `568-570,813-816` 记录 non-compliant producer 可卡 teardown、动态插件/Jobs 的故障会
   放大为容量与宿主生命周期问题；
3. `tech-agent-security-governance/research_report.md:267-276` 要求插件治理有 runtime enforcement、
   kill、撤回和 quarantine；`900-911` 要求明确 failure mode，并用真实故障实验而非模块名证明恢复；
4. `omnigent/research_report.md:281,357-364,877-901` 将 orphan callback、session/host lifecycle 错位和
   session 卡死列为 P0 状态一致性问题。

派生出的 W1 硬合同：一个可信内建插件 callback 的 panic 不能撞垮 API 进程当前调用栈，不能跳过其他
插件的 Drain/Stop/cleanup，也不能把可能含凭据、消息或用户数据的 panic value 写进 error。对于第三方
代码，这仍只是一道进程内 hygiene，生产安全边界必须是独立进程/UID/container/microVM。

对应 `docs/wanwork_im/RESEARCH_TRACEABILITY.md` 的 RQ-008 与 RQ-042。

## 2. 修复前的失败模式

此前所有 callback 已在 `3b8e02e` 移出 `Host.mu`，但 callback panic 仍会直接展开 Go 栈：

```text
Configure panic -> Host.Start panic，状态停在 starting
Start/Ready panic -> failAndRollback 永远不执行
Drain panic -> 后续 Drain/Stop/cleanup 全部跳过
Stop panic -> 后续 Stop/cleanup 全部跳过
cleanup panic -> 当前 scope 状态可能停在 cleanupInProgress
```

若直接使用 `fmt.Errorf("...: %v", recover())`，恶意或误用插件还可把 Secret、消息正文、URL、文件内容
或任意高敏对象塞进 panic payload，随后进入日志、HTTP error、trace 或 Notion。因此“recover 了”本身
不满足合同。

## 3. 实现语义

### 3.1 固定错误，不格式化 panic value

新增固定 sentinel：

```text
ErrLifecyclePanic = "plugin lifecycle callback panicked"
```

`configurePlugin` 捕获 `Factory.Configure` 的当前 goroutine panic，清空返回 instance，只返回 sentinel。
`callWithDeadline` 捕获 Start/Ready/Drain/Stop/effect cleanup 的当前 goroutine panic，同样只返回 sentinel。
该函数已在后续 `eafd3da` 更名，以明确它只提供 cooperative deadline；详见专题 29。

外层仍可添加 host-owned plugin ID 与 phase，例如 `drain plugin runtime.fake.v1`；recover value 的类型、
字符串和 stack 不进入返回错误。

### 3.2 Start/Ready panic 仍走 rollback

每个 plugin 在调用 Start 前已进入 `host.started`。因此：

- Start panic 被转换为 error 后，当前半启动 plugin 和此前 started plugins 全部逆序回收；
- Ready panic 发生时全部 plugin 已 started，全部进入逆序 Drain/Stop/cleanup；
- rollback 期间 Host 公开状态为 `stopping`，完成后发布 `failed`。

### 3.3 Shutdown panic 不饿死后续回收

`stopPlugins` 对每个 Drain、Stop 和每个 effect cleanup 独立调用 `callWithDeadline`。单项 panic 转成 error
并加入 `errors.Join`，循环继续：

```text
all Drain attempts
  -> all Stop attempts
  -> all effect cleanup attempts
  -> joined error
```

cleanup panic 被视为该 effect 清理失败；scope 只保留失败项，后续 Stop 可精确重试，不会把 scope
重新打开。

## 4. 可复核验收

| 测试 | 证明 |
|---|---|
| `TestConfigurePanicBecomesCodeOnlyLifecycleFailure` | Configure panic 不逃逸，Host 进入 failed，错误命中 sentinel 且不含 canary payload |
| `TestStartAndReadyPanicsRollbackWithoutPayloadDisclosure` | Start/Ready panic 均逆序 Drain/Stop/cleanup；错误不回显 payload |
| `TestShutdownPanicsDoNotSkipRemainingCallbacks` | Drain/Stop/cleanup 任一阶段 panic 时，三个插件的所有后续回收 callback 仍被调用 |
| P1-3 callback locking suite | recover 没有重新引入 Host mutex 下 callback，State/reentrancy/single-owner 语义保持 |
| effect retry suite | panic cleanup 作为失败项保留，不影响其他 effect/plugin cleanup |

门禁通过：

```text
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go test ./apps/im-api/... -count=1
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go test -race ./apps/im-api/internal/plugins -count=1
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go vet ./apps/im-api/...
git diff --check
```

## 5. 诚实边界

1. Go `recover` 只覆盖同一 goroutine；插件自行启动的 goroutine panic 仍可撞垮整个进程；
2. `runtime.Goexit`、fatal runtime error、SIGKILL、OOM 与 process exit 不会被这层 recover 转成 error；
3. `callWithDeadline` 是 cooperative cancellation；callback 忽略 context 时不会因为 deadline 自动返回；
4. `Factory.Configure` 仍没有 context/timeout，且合同要求 Configure 不产生外部副作用；
5. callback panic 可能发生在外部 effect 已经提交之后，panic error 不能证明“动作未发生”，仍需
   ActionIntent/Receipt/Unknown/Reconcile；
6. 当前不返回 stack，避免 payload 外泄；生产诊断应使用受控 crash/evidence channel、分类与脱敏，
   不能重新把 raw panic value 写入普通日志；
7. 本阶段没有开放真实融云 outbound，也没有向飞书、企微、机器人或 webhook 发送消息。

## 6. 下一步

1. Timeout honesty 已在 `eafd3da` 冻结，证据见
   [`29_plugin_lifecycle_cooperative_deadline_contract.md`](29_plugin_lifecycle_cooperative_deadline_contract.md)；
2. 下一步为第三方/不可信 plugin 冻结 supervisor + process/UID/container/microVM kill boundary 合同；
3. 再实现明确标记 volatile 的 deterministic MemoryFake EventStore；
4. 后续继续 source-only diff drift、NFC/跨语言 canonical vectors 与完整阶段门禁。
