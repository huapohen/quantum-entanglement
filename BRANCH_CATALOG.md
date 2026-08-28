# Quantum Entanglement 分支与 Worktree 导航

> 结论先行：**日常开发、启动体验和后续集成都只使用 `main`。** 除非是在做历史审计或定点恢复，不要直接在 `codex/*`、`agent/*`、`gate-*` 或 `archive/*` 上继续开发，也不要把这些分支整条合并回 `main`。

## 你现在应该用哪个

| 场景 | 应使用的引用 | 说明 |
| --- | --- | --- |
| 日常开发、验收、继续主线任务 | `main`（目录基线 `9e44dafecf5e`） | 唯一正式主分支；目录 `/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement`。 |
| 复现当前本地试用版本 | `v0.1.0-local-trial.2` | 固定版本标签，不会随 `main` 后续提交移动。 |
| 查看上一试用检查点 | `v0.1.0-local-trial.1` | 已被 `.2` 取代，仅用于对比。 |
| 恢复某项历史实现 | 先从 `main` 新建分支，再挑选提交 | 优先 `git cherry-pick` 单个已审阅提交，不直接合并历史分支。 |
| 事故取证或找回孤立提交 | `archive/*` | 只读保险引用，禁止作为新开发起点。 |

## 分支数量为什么看起来很多

远端当前共有 **58** 个分支引用：1 个正式主线、8 个历史开发/证据候选、49 个只读取证归档。`archive/*` 中有不少只是同一历史节点的保险副本，并不代表同时维护的产品版本。

Git 本身不保存可靠的“分支创建时间”。下表的“节点时间”是该分支尖端提交的提交时间，这是能够审计的时间节点；不能把它冒充为分支创建时间。`领先/落后` 以目录基线为准；若 `origin/main` 最新提交只更新本目录，生成器会使用其父提交，避免目录提交导致自身立即过期。

## 命名和生命周期

- `main`：唯一正式主线，受 CI 和发布检查约束。
- `codex/*`：阶段性实现、修复或集成候选；当前统一按历史只读处理。
- `agent/*`：工程证据账本或 Agent 专项产物分支；当前统一按历史只读处理。
- `gate-*`：安全门禁候选；不等于门禁已经批准。
- `archive/<日期>/*`：为避免历史节点丢失而建立的冻结保险引用。
- `v*` 标签：不可移动的验收/发布检查点；复现版本时优先用标签而不是猜分支。

## 所有非归档开发分支

| 节点时间 | 分支 | 用途 | 相对 main | 差异 | Worktree |
| --- | --- | --- | --- | --- | --- |
| 2026-08-28T11:29:06+08:00 | `mainline_continue_quantum_entanglement`<br>`d80226040090` | 独立推进原生 IM E1–E5 的人工审阅分支；不自动合并回 main，保留专用 worktree 和独立 Notion 审阅空间。 | 未直接并入 main | 领先 106 / 落后 0 | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/mainline_continue_quantum_entanglement` |
| 2026-08-28T09:38:04+08:00 | `dev_wanwork_quantum_entanglement`<br>`74a01a794ee3` | 用途待补充；当前节点主题：docs: checkpoint remote backup Notion readback | 未直接并入 main | 领先 82 / 落后 0 | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_wanwork_quantum_entanglement` |
| 2026-08-28T02:01:09+08:00 | `main`<br>`9e44dafecf5e` | 唯一正式主线；当前可验收版本、后续开发起点和发布集成都以此为准。 | 主线目录基线 | 领先 0 / 落后 0 | `/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement` |
| 2026-08-27T18:07:00+08:00 | `backup_0827_200010`<br>`1d399e555fb0` | 2026-08-27 提前接入原生 IM 前恢复分支；固定指向 main@1d399e5，不在此分支继续开发。 | 已作为祖先进入 main | 领先 0 / 落后 12 | — |
| 2026-08-23T18:04:39Z | `dependabot/pip/mypy-2.3.1`<br>`fce21d09e51b` | 用途待补充；当前节点主题：build(deps-dev): bump mypy from 1.19.1 to 2.3.1 | 未直接并入 main | 领先 1 / 落后 240 | — |
| 2026-08-23T18:04:33Z | `dependabot/pip/ruff-0.16.4`<br>`bf627bc8a49f` | 用途待补充；当前节点主题：build(deps): bump ruff from 0.16.3 to 0.16.4 | 未直接并入 main | 领先 1 / 落后 240 | — |
| 2026-08-23T18:04:23Z | `dependabot/pip/setuptools-84.0.0`<br>`f6af9b49fb5e` | 用途待补充；当前节点主题：build(deps): bump setuptools from 82.0.1 to 84.0.0 | 未直接并入 main | 领先 1 / 落后 240 | — |
| 2026-08-23T18:04:16Z | `dependabot/pip/build-1.5.0`<br>`003a2dfd6eba` | 用途待补充；当前节点主题：build(deps): bump build from 1.4.4 to 1.5.0 | 未直接并入 main | 领先 1 / 落后 240 | — |
| 2026-08-23T18:03:59Z | `dependabot/pip/pytest-asyncio-1.4.0`<br>`09a365f20145` | 用途待补充；当前节点主题：build(deps-dev): bump pytest-asyncio from 1.2.0 to 1.4.0 | 未直接并入 main | 领先 1 / 落后 240 | — |

### 如何理解“未直接并入 main”

这不等于该分支的价值没有进入主线。部分修复曾通过重写、cherry-pick 或在更新基线上重新实现，因此旧分支尖端不会成为 `main` 的祖先。不要仅凭这个字段执行 merge；应先比较具体提交和测试证据。

## 所有 archive 取证分支

| 节点时间 | 归档引用 | 类型 | 保存内容 | 相对 main |
| --- | --- | --- | --- | --- |
| 2026-08-21T23:29:23+08:00 | `archive/2026-08-27/codex/py39-test-isolation-fix`<br>`04298b429a34` | 归档：开发分支副本 | 只读取证副本：Python 3.9 fork 探针前回收旧 Store 周期的测试隔离修复候选。 | 未直接并入 main；领先 1 / 落后 266 |
| 2026-08-21T23:21:46+08:00 | `archive/2026-08-27/codex/ci-linux-test-fix`<br>`bbef2e02642b` | 归档：开发分支副本 | 只读取证副本：Linux 文件路径所有者与受保护祖先校验的 CI 修复候选。 | 未直接并入 main；领先 3 / 落后 270 |
| 2026-08-21T22:53:52+08:00 | `archive/2026-08-27/codex/ci-package-fix`<br>`7755d83bd092` | 归档：开发分支副本 | 只读取证副本：源码分发包测试标记与 sdist 契约的 CI 修复候选。 | 未直接并入 main；领先 2 / 落后 270 |
| 2026-08-21T20:33:35+08:00 | `archive/2026-08-27/codex/recovery-integrate-current`<br>`e21654a06663` | 归档：开发分支副本 | 只读取证副本：恢复机制向当时最新主线集成后的阶段检查点；其节点已进入 main。 | 已作为祖先进入 main；领先 0 / 落后 313 |
| 2026-08-21T19:35:53+08:00 | `archive/2026-08-27/codex/gate-a-operation-authorization`<br>`1dd4247b0a1b` | 归档：开发分支副本 | 只读取证副本：Gate A 操作时授权、令牌消费和重授权边界候选。 | 未直接并入 main；领先 42 / 落后 464 |
| 2026-08-21T19:11:57+08:00 | `archive/2026-08-27/codex/local-product-trial-v1`<br>`8dfff5e99cf8` | 归档：开发分支副本 | 只读取证副本：当前本地产品体验页面、启动脚本、教程和验收截图的第一版阶段分支。 | 未直接并入 main；领先 16 / 落后 403 |
| 2026-08-21T16:40:21+08:00 | `archive/2026-08-27/codex/event-store-process-binding-v1`<br>`80dad143e7d5` | 归档：开发分支副本 | 只读取证副本：Event Store 与创建进程身份绑定的第一版候选。 | 未直接并入 main；领先 25 / 落后 403 |
| 2026-08-21T15:12:27+08:00 | `archive/2026-08-27/codex/backup-v2-on-canonical-v1`<br>`0b044d0dd63a` | 归档：开发分支副本 | 只读取证副本：把备份 v2 候选重放到当时规范主线上并补齐回滚证据。 | 未直接并入 main；领先 26 / 落后 403 |
| 2026-08-21T10:39:41+08:00 | `archive/2026-08-21/codex/backup-v2-snapshot-derivation`<br>`3946847b1ece` | 归档：开发分支副本 | 只读取证副本：备份快照派生、导入边界和生命周期验证。 | 未直接并入 main；领先 18 / 落后 443 |
| 2026-08-21T08:25:58+08:00 | `archive/2026-08-21/codex/backup-v2-exact-topology`<br>`bc95912a7407` | 归档：开发分支副本 | 只读取证副本：备份 v2 的精确拓扑、所有权与恢复关系候选。 | 未直接并入 main；领先 5 / 落后 443 |
| 2026-08-21T08:07:46+08:00 | `archive/2026-08-21/codex/event-store-audit-on-foundation`<br>`e896c2de8758` | 归档：开发分支副本 | 只读取证副本：基于进程身份基础版本开展 Event Store 审计。 | 未直接并入 main；领先 7 / 落后 443 |
| 2026-08-21T07:55:21+08:00 | `archive/2026-08-21/codex/event-store-process-audit`<br>`a5eaff9b4bda` | 归档：开发分支副本 | 只读取证副本：Event Store 跨进程使用、继承和失效边界审计。 | 未直接并入 main；领先 7 / 落后 445 |
| 2026-08-21T00:01:11+08:00 | `archive/2026-08-21/codex/attempt-recovery-on-process-v3`<br>`af8a6499f397` | 归档：开发分支副本 | 只读取证副本：在进程身份约束基础上继续验证调用恢复和运行时证据同步。 | 未直接并入 main；领先 33 / 落后 444 |
| 2026-08-20T23:45:11+08:00 | `archive/2026-08-21/codex/invocation-on-canonical-v4`<br>`622dbaa977cb` | 归档：开发分支副本 | 只读取证副本：调用生命周期上下文解绑的第四版规范候选；其节点已成为 main 的祖先。 | 已作为祖先进入 main；领先 0 / 落后 443 |
| 2026-08-20T23:24:26+08:00 | `archive/2026-08-21/codex/notion-sync-v2`<br>`cded75eb5341` | 归档：开发分支副本 | 只读取证副本：Notion 证据同步与运行时回读的第二版实验分支。 | 未直接并入 main；领先 30 / 落后 452 |
| 2026-08-20T23:23:15+08:00 | `archive/2026-08-21/codex/process-identity-foundation`<br>`ca02903b38f5` | 归档：开发分支副本 | 只读取证副本：进程身份、fork 继承拒绝和资源所有权的基础分支；其节点已进入 main。 | 已作为祖先进入 main；领先 0 / 落后 444 |
| 2026-08-20T23:11:13+08:00 | `archive/2026-08-21/reflog/authorization-boundary-hardening`<br>`a06d7d290109` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：docs: record authorization boundary hardening | 未直接并入 main；领先 14 / 落后 464 |
| 2026-08-20T22:25:54+08:00 | `archive/2026-08-21/codex/attempt-recovery-integrated-v2`<br>`8d82dd7e672d` | 归档：开发分支副本 | 只读取证副本：调用尝试恢复与事务一致性的第二版集成候选。 | 未直接并入 main；领先 24 / 落后 452 |
| 2026-08-20T22:25:36+08:00 | `archive/2026-08-21/codex/attempt-recovery-safe-v2`<br>`7f9530010037` | 归档：开发分支副本 | 只读取证副本：调用恢复第二版的保守安全候选，用于隔离验证失败边界。 | 未直接并入 main；领先 25 / 落后 453 |
| 2026-08-20T21:27:04+08:00 | `archive/2026-08-21/worktree/opauth-opaque-operation-integration`<br>`e99f809f218a` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：feat: issue opaque authorized operations | 未直接并入 main；领先 2 / 落后 453 |
| 2026-08-20T21:17:53+08:00 | `archive/2026-08-21/worktree/replay-structured-dispatch-draining`<br>`b877b6dd786c` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：docs: define structured dispatch draining | 未直接并入 main；领先 16 / 落后 455 |
| 2026-08-20T21:06:06+08:00 | `archive/2026-08-21/worktree/attempt-error-consistency`<br>`2f7117307faa` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：docs: define attempt error consistency | 未直接并入 main；领先 8 / 落后 455 |
| 2026-08-20T21:01:35+08:00 | `archive/2026-08-21/reflog/issuer-fork-identity`<br>`1aa82c972f94` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：style: format issuer fork identity assertion | 未直接并入 main；领先 11 / 落后 464 |
| 2026-08-20T20:54:55+08:00 | `archive/2026-08-21/codex/attempt-recovery-v1`<br>`e05357bebde6` | 归档：开发分支副本 | 只读取证副本：调用恢复第一版实验，重点验证 invocation 事务证据认证。 | 未直接并入 main；领先 60 / 落后 617 |
| 2026-08-20T20:40:04+08:00 | `archive/2026-08-21/codex/backup-v2-codec`<br>`bcba6a419fbe` | 归档：开发分支副本 | 只读取证副本：精确备份 manifest v2 编解码与可复现序列化实验。 | 未直接并入 main；领先 3 / 落后 455 |
| 2026-08-20T20:15:56+08:00 | `archive/2026-08-21/worktree/opauth-process-bound-compose`<br>`23183fd0cde1` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：feat: compose process-bound action-time operation authorization | 未直接并入 main；领先 3 / 落后 464 |
| 2026-08-20T19:37:55+08:00 | `archive/2026-08-21/reflog/fork-inherited-invocation-store`<br>`d0f93b393e98` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：fix: reject fork-inherited invocation stores | 未直接并入 main；领先 58 / 落后 617 |
| 2026-08-20T19:18:34+08:00 | `archive/2026-08-21/worktree/operation-auth-boundary`<br>`3609ab806fc7` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：docs: define protected operation boundary | 未直接并入 main；领先 4 / 落后 457 |
| 2026-08-20T19:04:10+08:00 | `archive/2026-08-21/reflog/hostile-auth-fault-boundary`<br>`bddecdf5d4c1` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：docs: specify hostile authorization fault boundary | 未直接并入 main；领先 6 / 落后 464 |
| 2026-08-20T18:20:58+08:00 | `archive/2026-08-21/reflog/action-time-reauthorization`<br>`3af4ded12c6d` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：docs: require action-time operation reauthorization | 未直接并入 main；领先 7 / 落后 464 |
| 2026-08-20T17:34:42+08:00 | `archive/2026-08-21/worktree/recovery-durable-invocation`<br>`276913ce69ae` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：fix: reconcile durable invocation transactions | 未直接并入 main；领先 17 / 落后 464 |
| 2026-08-20T17:15:21+08:00 | `archive/2026-08-21/reflog/invocation-enqueue-reconcile`<br>`eaab78181289` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：fix: reconcile invocation enqueue commits | 未直接并入 main；领先 56 / 落后 617 |
| 2026-08-20T15:58:05+08:00 | `archive/2026-08-21/codex/request-context-audit-20260820`<br>`a6b4a4947138` | 归档：开发分支副本 | 只读取证副本：请求上下文、凭据异常和 traceback 泄露边界审计。 | 未直接并入 main；领先 6 / 落后 470 |
| 2026-08-20T15:07:35+08:00 | `archive/2026-08-21/gate-a-trusted-context-foundation`<br>`c95635beb726` | 归档：Gate A 副本 | 只读取证副本：Gate A 可信上下文、凭据擦除失败和请求边界的基础候选。 | 未直接并入 main；领先 21 / 落后 559 |
| 2026-08-20T10:41:51+08:00 | `archive/2026-08-21/codex/supply-policy-latest-audit`<br>`11dfa301de69` | 归档：开发分支副本 | 只读取证副本：依赖采用、回滚和风险晋级规则的较新审计候选。 | 未直接并入 main；领先 15 / 落后 589 |
| 2026-08-20T10:35:28+08:00 | `archive/2026-08-21/codex/supply-policy-current-audit`<br>`ff663e9165e7` | 归档：开发分支副本 | 只读取证副本：依赖风险策略在当时当前代码上的审计与数值边界修复。 | 未直接并入 main；领先 14 / 落后 617 |
| 2026-08-20T09:50:26+08:00 | `archive/2026-08-21/codex/safe-logging-current-20260820090738`<br>`f683ef508547` | 归档：开发分支副本 | 只读取证副本：安全日志候选在当时当前代码上的集成与取消异常收敛。 | 未直接并入 main；领先 16 / 落后 623 |
| 2026-08-20T09:47:15+08:00 | `archive/2026-08-21/codex/supply-policy-v1`<br>`28959b497c86` | 归档：开发分支副本 | 只读取证副本：依赖风险晋级与供应链策略第一版。 | 未直接并入 main；领先 9 / 落后 653 |
| 2026-08-20T09:08:23+08:00 | `archive/2026-08-21/codex/safe-logging-v1`<br>`88c3d921f31c` | 归档：开发分支副本 | 只读取证副本：复合凭据字段脱敏和安全日志第一版。 | 未直接并入 main；领先 18 / 落后 665 |
| 2026-08-20T08:48:58+08:00 | `archive/2026-08-21/agent/evidence-ledger-current`<br>`0cdfaaa8a8ab` | 归档：Agent 分支副本 | 只读取证副本：面向当时较新代码节点刷新的工程证据账本候选。 | 未直接并入 main；领先 2 / 落后 654 |
| 2026-08-20T08:48:34+08:00 | `archive/2026-08-21/codex/supply-chain-docs-v1`<br>`165a92c1c173` | 归档：开发分支副本 | 只读取证副本：供应链安全里程碑、证据顺序和后续门禁文档。 | 未直接并入 main；领先 4 / 落后 653 |
| 2026-08-20T08:41:43+08:00 | `archive/2026-08-21/codex/service-config-v1`<br>`525161ed70bf` | 归档：开发分支副本 | 只读取证副本：服务配置校验、环境边界和配置测试第一版。 | 未直接并入 main；领先 10 / 落后 665 |
| 2026-08-20T08:38:36+08:00 | `archive/2026-08-21/agent/evidence-ledger`<br>`7d46958f18ec` | 归档：Agent 分支副本 | 只读取证副本：早期工程证据账本分支，用于汇总测试、审计和阶段结论。 | 未直接并入 main；领先 3 / 落后 665 |
| 2026-08-20T08:26:01+08:00 | `archive/2026-08-21/codex/service-boundary-v1`<br>`819b74cd1b45` | 归档：开发分支副本 | 只读取证副本：服务运行边界、信任假设和安全策略绑定第一版。 | 未直接并入 main；领先 7 / 落后 668 |
| 2026-08-20T08:24:30+08:00 | `archive/2026-08-21/codex/backup-manifest-v1`<br>`87bd23a7a6bd` | 归档：开发分支副本 | 只读取证副本：备份状态证据和 manifest v1 边界的早期实现。 | 未直接并入 main；领先 4 / 落后 668 |
| 2026-08-20T08:14:17+08:00 | `archive/2026-08-21/dangling/approval-recovery-chains`<br>`7bd31f947bdc` | 归档：孤立提交 | 保存当时无分支引用的提交，防止垃圾回收后丢失。 节点主题：validate staged approval recovery chains | 未直接并入 main；领先 1 / 落后 671 |
| 2026-08-20T08:10:46+08:00 | `archive/2026-08-21/dangling/approval-decision-atomicity`<br>`ab0ef94b8bf7` | 归档：孤立提交 | 保存当时无分支引用的提交，防止垃圾回收后丢失。 节点主题：validate staged approval decision atomicity | 未直接并入 main；领先 1 / 落后 673 |
| 2026-08-20T08:06:15+08:00 | `archive/2026-08-21/dangling/approval-request-atomicity`<br>`f596a7becf08` | 归档：孤立提交 | 保存当时无分支引用的提交，防止垃圾回收后丢失。 节点主题：validate staged approval request atomicity | 未直接并入 main；领先 1 / 落后 674 |
| 2026-08-20T08:02:44+08:00 | `archive/2026-08-21/dangling/approval-snapshots`<br>`c2d8ba70bf93` | 归档：孤立提交 | 保存当时无分支引用的提交，防止垃圾回收后丢失。 节点主题：validate staged approval snapshots | 未直接并入 main；领先 1 / 落后 675 |

## 本机 Worktree 目录

`main` 固定在 `/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement`。当前另有 2 个辅助 linked worktree；它们统一位于 `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement`，完成后必须合并、推送并移除。

| 状态 | 分支/模式 | HEAD | 路径 |
| --- | --- | --- | --- |
| 正式主线工作区 | `main` | `9e44dafecf5e` | `/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement` |
| 存在、有未提交修改 | `dev_wanwork_quantum_entanglement` | `d3915cae9ad3` | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_wanwork_quantum_entanglement` |
| 存在、有未提交修改 | `mainline_continue_quantum_entanglement` | `d80226040090` | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/mainline_continue_quantum_entanglement` |

## 固定版本标签

| 标签 | 指向提交 | 标签对象 | 时间 |
| --- | --- | --- | --- |
| `pre-native-im-20260827-200010` | `1d399e555fb0` | `cf0ff334e4a9` | 2026-08-27T20:00:45+08:00 |
| `v0.1.0-local-trial.2` | `3f9feaa72dfd` | `655ac8c1eb8e` | 2026-08-21T23:57:36+08:00 |
| `v0.1.0-local-trial.1` | `297d53203211` | `8efc888b434e` | 2026-08-21T22:00:15+08:00 |

## 以后如何新增分支而不弄乱 execute

从主线创建分支时，直接把 worktree 放进统一目录：

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement
git fetch origin
git worktree add /Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/<目录名> \
  -b codex/<任务名> origin/main
```

完成后刷新本文档：

```bash
./scripts/update_branch_catalog.sh --fetch
```

新分支会自动出现在表格里。如果希望用途说明不是“待补充”，在 `docs/branch_catalog_metadata.json` 的 `branches` 中增加一条说明后再次运行更新脚本。

检查目录是否过期而不写文件：

```bash
./scripts/update_branch_catalog.sh --check
```

## 管理规则

1. `main` 永远是唯一默认主线；阶段分支不能自封为发布分支。
2. 每个小改动继续独立提交；阶段完成后合并回 `main` 并推送远端。
3. 新 worktree 一律建在 `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement`。
4. 推送成功后删除已完成的本地 worktree 和本地阶段分支，不长期堆积。
5. 删除远端 active 分支前，必须确认提交已进入 `main` 或已有同 SHA 的 `archive/*` 冻结引用。
6. `archive/*` 只用于保全证据，不在其中开发、不移动其尖端。
7. 删除 worktree 前先确认状态干净、提交已推送；使用 `git worktree remove`，不要直接删目录。
8. 每次新增、移动或删除分支/worktree 后运行目录更新脚本并提交生成结果。
