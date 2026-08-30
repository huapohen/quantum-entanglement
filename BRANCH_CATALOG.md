# Quantum Entanglement 分支与 Worktree 导航

> 结论先行：**正式开发、发布和后续集成使用 `main`；当前未合并的 Web IM/Agent Store 阶段验收使用 `dev_wanwork_quantum_entanglement`。** 除非是在做历史审计或定点恢复，不要直接在 `codex/*`、`agent/*`、`gate-*` 或 `archive/*` 上继续开发，也不要把这些分支整条合并回 `main`。

## 你现在应该用哪个

| 场景 | 应使用的引用 | 说明 |
| --- | --- | --- |
| 正式开发、发布、后续集成 | `main`（目录基线 `56bd364bddd7`） | 唯一正式主分支；目录 `/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement`。 |
| 当前 Web IM/Agent Store 阶段验收 | `dev_wanwork_quantum_entanglement` | 未合并到 main 的独立验收分支；worktree 位于统一 `worktrees/quantum_entanglement` 目录。 |
| 复现当前本地试用版本 | `v0.1.0-local-trial.2` | 固定版本标签，不会随 `main` 后续提交移动。 |
| 查看上一试用检查点 | `v0.1.0-local-trial.1` | 已被 `.2` 取代，仅用于对比。 |
| 恢复某项历史实现 | 先从 `main` 新建分支，再挑选提交 | 优先 `git cherry-pick` 单个已审阅提交，不直接合并历史分支。 |
| 事故取证或找回孤立提交 | `archive/*` | 只读保险引用，禁止作为新开发起点。 |

## 分支数量为什么看起来很多

远端当前共有 **81** 个分支引用：1 个正式主线、31 个历史开发/证据候选、49 个只读取证归档。`archive/*` 中有不少只是同一历史节点的保险副本，并不代表同时维护的产品版本。

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
| 2026-08-30T15:46:52+08:00 | `main`<br>`56bd364bddd7` | 唯一正式主线；当前可验收版本、后续开发起点和发布集成都以此为准。 | 主线目录基线 | 领先 0 / 落后 0 | `/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement` |
| 2026-08-30T15:43:16+08:00 | `dev_wanwork_quantum_entanglement`<br>`facc7e319009` | WanWork IM Web/PWA Agent Store 验收主线；包含群聊、Agent 子群、Workboard、消息搜索、安装/撤权和局域网跨端体验。保持独立于正式 main，当前用户验收应使用此分支，完成阶段验收后再决定是否合并。 | 未直接并入 main | 领先 512 / 落后 28 | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_wanwork_quantum_entanglement` |
| 2026-08-30T15:43:16+08:00 | `backup_0830_154546`<br>`facc7e319009` | 2026-08-30 15:45 Agent Store offboard 数据处置响应回显与完整 Web-first 门禁通过后的备份；仅用于恢复和审计。 | 未直接并入 main | 领先 512 / 落后 28 | — |
| 2026-08-30T15:37:30+08:00 | `mainline_continue_quantum_entanglement`<br>`0d996fcaf29c` | E3 Result Authority 人工评审分支；opt-in store-owned result acceptance、fresh-ACK AcceptedV2、heartbeat acceptance seam 与 result-only business projection 候选已完成，生产 worker、认证 projection、真实 IM 与 outbound 仍关闭；不自动合并回 main。 | 未直接并入 main | 领先 922 / 落后 28 | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/mainline_continue_quantum_entanglement` |
| 2026-08-30T15:35:24+08:00 | `backup_0830_153743`<br>`12e7c524bef2` | 2026-08-30 15:37 Agent Store capability gate、数据处置选择和 offboard 响应回显后的完整备份；仅用于恢复和审计。 | 未直接并入 main | 领先 511 / 落后 28 | — |
| 2026-08-30T15:15:03+08:00 | `backup_0830_151535`<br>`6e26c2298d12` | 2026-08-30 15:15 Agent Store offboard 文档与状态台账备份；仅用于恢复和审计。 | 未直接并入 main | 领先 508 / 落后 28 | — |
| 2026-08-30T15:13:29+08:00 | `backup_0830_151334`<br>`0a8640ab1834` | 2026-08-30 15:13 Agent Store offboard 阶段状态备份；仅用于恢复和审计。 | 未直接并入 main | 领先 507 / 落后 28 | — |
| 2026-08-30T15:09:03+08:00 | `backup_0830_150912`<br>`5a3a6a4d4b7b` | 2026-08-30 15:09 Agent Store offboard 文档收口备份；仅用于恢复和审计。 | 未直接并入 main | 领先 506 / 落后 28 | — |
| 2026-08-30T15:04:30+08:00 | `backup_0830_150452`<br>`f1d557e3298b` | 2026-08-30 15:04 Agent Store offboard 验收实现备份；仅用于恢复和审计。 | 未直接并入 main | 领先 505 / 落后 28 | — |
| 2026-08-30T14:20:59+08:00 | `backup_0830_142104`<br>`cf58af1d7f36` | 2026-08-30 14:21 Web/PWA IM 当前备份；固定指向 action-time Trust Passport 加固及全量门禁通过节点，仅用于恢复和审计。 | 未直接并入 main | 领先 501 / 落后 28 | — |
| 2026-08-30T14:15:32+08:00 | `backup_0830_141602`<br>`1f86e5a2647d` | 2026-08-30 14:16 Web/PWA IM 当前备份；固定指向 Agent Store action-time Trust Passport 准入加固节点，仅用于恢复和审计。 | 未直接并入 main | 领先 500 / 落后 28 | — |
| 2026-08-30T13:57:34+08:00 | `backup_0830_135748`<br>`b53180cbffd0` | 2026-08-30 13:57 Web/PWA IM 当前阶段备份；固定指向 Agent Store 安装、Artifact 引用发布及文档收口节点，仅用于恢复和审计。 | 未直接并入 main | 领先 498 / 落后 28 | — |
| 2026-08-30T13:48:30+08:00 | `backup_0830_135259`<br>`536395c73db4` | 2026-08-30 13:52 Web/PWA IM 最新备份；固定指向 Agent Store 安装闭环与 Artifact 引用发布节点，仅用于恢复和审计。 | 未直接并入 main | 领先 497 / 落后 28 | — |
| 2026-08-30T13:30:55+08:00 | `backup_0830_133349`<br>`8095c3ba4a45` | 2026-08-30 13:33 Web/PWA IM Agent Store 安装闭环备份；固定指向安装/幂等回放门禁节点，仅用于恢复和审计。 | 未直接并入 main | 领先 494 / 落后 28 | — |
| 2026-08-30T13:05:07+08:00 | `backup_0830_130522`<br>`931ff67daa51` | 2026-08-30 13:05 Agent Store 验收文档备份；固定指向独立验收证据落盘节点，仅用于恢复和审计。 | 未直接并入 main | 领先 491 / 落后 28 | — |
| 2026-08-30T12:59:52+08:00 | `backup_0830_125930`<br>`ec9ae12425de` | 2026-08-30 12:59 Agent Store 专项证据备份；固定指向安装治理说明节点，仅用于恢复和审计。 | 未直接并入 main | 领先 490 / 落后 28 | — |
| 2026-08-30T12:37:25+08:00 | `backup_0830_123730`<br>`c3118f46d7f2` | 2026-08-30 Web/PWA IM 最新验收备份；固定指向 c3118f4，仅用于恢复和审计。 | 未直接并入 main | 领先 489 / 落后 28 | — |
| 2026-08-30T12:21:52+08:00 | `backup_0830_122157`<br>`cee65799732a` | 2026-08-30 Web/PWA IM 阶段备份；固定指向 cee6579，仅用于恢复和审计。 | 未直接并入 main | 领先 488 / 落后 28 | — |
| 2026-08-30T12:19:35+08:00 | `backup_0830_121946`<br>`208b6a993c19` | 用途待补充；当前节点主题：docs(status): record notion mirror checkpoint | 未直接并入 main | 领先 487 / 落后 28 | — |
| 2026-08-30T12:14:31+08:00 | `backup_0830_301220`<br>`a526d4afb768` | 用途待补充；当前节点主题：docs(status): refresh latest web-first checkpoint | 未直接并入 main | 领先 486 / 落后 28 | — |
| 2026-08-30T12:12:44+08:00 | `backup_0830_301215`<br>`6c877f479199` | 用途待补充；当前节点主题：docs(report): add visual 10-hour execution status | 未直接并入 main | 领先 485 / 落后 28 | — |
| 2026-08-30T12:07:12+08:00 | `backup_0830_301202`<br>`9faf96985185` | 用途待补充；当前节点主题：feat(web): expose direct conversation trial | 未直接并入 main | 领先 484 / 落后 28 | — |
| 2026-08-30T12:05:30+08:00 | `backup_0830_121258`<br>`d7235b0127e7` | 用途待补充；当前节点主题：docs: refresh branch catalog final checkpoint | 未直接并入 main | 领先 406 / 落后 28 | — |
| 2026-08-30T12:00:04+08:00 | `backup_0830_301145`<br>`f898ca8c4ca0` | 用途待补充；当前节点主题：test(postgres): strengthen v2 attempt and fence contract | 未直接并入 main | 领先 481 / 落后 28 | — |
| 2026-08-30T10:52:29+08:00 | `dev_im_persistence_accelerator_20260830`<br>`51dbb1e5bd9a` | 用途待补充；当前节点主题：docs(im): include durable projection checkpoint | 未直接并入 main | 领先 471 / 落后 28 | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_im_persistence_accelerator_20260830` |
| 2026-08-29T18:36:20+08:00 | `backup_0830_122508_main_pre_im_merge`<br>`19d4dc5506eb` | 用途待补充；当前节点主题：docs(branches): record projection identity hardening | 已作为祖先进入 main | 领先 0 / 落后 15 | — |
| 2026-08-27T18:07:00+08:00 | `backup_0827_200010`<br>`1d399e555fb0` | 2026-08-27 提前接入原生 IM 前恢复分支；固定指向 main@1d399e5，不在此分支继续开发。 | 已作为祖先进入 main | 领先 0 / 落后 41 | — |
| 2026-08-23T18:04:39Z | `dependabot/pip/mypy-2.3.1`<br>`fce21d09e51b` | 用途待补充；当前节点主题：build(deps-dev): bump mypy from 1.19.1 to 2.3.1 | 未直接并入 main | 领先 1 / 落后 269 | — |
| 2026-08-23T18:04:33Z | `dependabot/pip/ruff-0.16.4`<br>`bf627bc8a49f` | 用途待补充；当前节点主题：build(deps): bump ruff from 0.16.3 to 0.16.4 | 未直接并入 main | 领先 1 / 落后 269 | — |
| 2026-08-23T18:04:23Z | `dependabot/pip/setuptools-84.0.0`<br>`f6af9b49fb5e` | 用途待补充；当前节点主题：build(deps): bump setuptools from 82.0.1 to 84.0.0 | 未直接并入 main | 领先 1 / 落后 269 | — |
| 2026-08-23T18:04:16Z | `dependabot/pip/build-1.5.0`<br>`003a2dfd6eba` | 用途待补充；当前节点主题：build(deps): bump build from 1.4.4 to 1.5.0 | 未直接并入 main | 领先 1 / 落后 269 | — |
| 2026-08-23T18:03:59Z | `dependabot/pip/pytest-asyncio-1.4.0`<br>`09a365f20145` | 用途待补充；当前节点主题：build(deps-dev): bump pytest-asyncio from 1.2.0 to 1.4.0 | 未直接并入 main | 领先 1 / 落后 269 | — |

### 如何理解“未直接并入 main”

这不等于该分支的价值没有进入主线。部分修复曾通过重写、cherry-pick 或在更新基线上重新实现，因此旧分支尖端不会成为 `main` 的祖先。不要仅凭这个字段执行 merge；应先比较具体提交和测试证据。

## 所有 archive 取证分支

| 节点时间 | 归档引用 | 类型 | 保存内容 | 相对 main |
| --- | --- | --- | --- | --- |
| 2026-08-21T23:29:23+08:00 | `archive/2026-08-27/codex/py39-test-isolation-fix`<br>`04298b429a34` | 归档：开发分支副本 | 只读取证副本：Python 3.9 fork 探针前回收旧 Store 周期的测试隔离修复候选。 | 未直接并入 main；领先 1 / 落后 295 |
| 2026-08-21T23:21:46+08:00 | `archive/2026-08-27/codex/ci-linux-test-fix`<br>`bbef2e02642b` | 归档：开发分支副本 | 只读取证副本：Linux 文件路径所有者与受保护祖先校验的 CI 修复候选。 | 未直接并入 main；领先 3 / 落后 299 |
| 2026-08-21T22:53:52+08:00 | `archive/2026-08-27/codex/ci-package-fix`<br>`7755d83bd092` | 归档：开发分支副本 | 只读取证副本：源码分发包测试标记与 sdist 契约的 CI 修复候选。 | 未直接并入 main；领先 2 / 落后 299 |
| 2026-08-21T20:33:35+08:00 | `archive/2026-08-27/codex/recovery-integrate-current`<br>`e21654a06663` | 归档：开发分支副本 | 只读取证副本：恢复机制向当时最新主线集成后的阶段检查点；其节点已进入 main。 | 已作为祖先进入 main；领先 0 / 落后 342 |
| 2026-08-21T19:35:53+08:00 | `archive/2026-08-27/codex/gate-a-operation-authorization`<br>`1dd4247b0a1b` | 归档：开发分支副本 | 只读取证副本：Gate A 操作时授权、令牌消费和重授权边界候选。 | 未直接并入 main；领先 42 / 落后 493 |
| 2026-08-21T19:11:57+08:00 | `archive/2026-08-27/codex/local-product-trial-v1`<br>`8dfff5e99cf8` | 归档：开发分支副本 | 只读取证副本：当前本地产品体验页面、启动脚本、教程和验收截图的第一版阶段分支。 | 未直接并入 main；领先 16 / 落后 432 |
| 2026-08-21T16:40:21+08:00 | `archive/2026-08-27/codex/event-store-process-binding-v1`<br>`80dad143e7d5` | 归档：开发分支副本 | 只读取证副本：Event Store 与创建进程身份绑定的第一版候选。 | 未直接并入 main；领先 25 / 落后 432 |
| 2026-08-21T15:12:27+08:00 | `archive/2026-08-27/codex/backup-v2-on-canonical-v1`<br>`0b044d0dd63a` | 归档：开发分支副本 | 只读取证副本：把备份 v2 候选重放到当时规范主线上并补齐回滚证据。 | 未直接并入 main；领先 26 / 落后 432 |
| 2026-08-21T10:39:41+08:00 | `archive/2026-08-21/codex/backup-v2-snapshot-derivation`<br>`3946847b1ece` | 归档：开发分支副本 | 只读取证副本：备份快照派生、导入边界和生命周期验证。 | 未直接并入 main；领先 18 / 落后 472 |
| 2026-08-21T08:25:58+08:00 | `archive/2026-08-21/codex/backup-v2-exact-topology`<br>`bc95912a7407` | 归档：开发分支副本 | 只读取证副本：备份 v2 的精确拓扑、所有权与恢复关系候选。 | 未直接并入 main；领先 5 / 落后 472 |
| 2026-08-21T08:07:46+08:00 | `archive/2026-08-21/codex/event-store-audit-on-foundation`<br>`e896c2de8758` | 归档：开发分支副本 | 只读取证副本：基于进程身份基础版本开展 Event Store 审计。 | 未直接并入 main；领先 7 / 落后 472 |
| 2026-08-21T07:55:21+08:00 | `archive/2026-08-21/codex/event-store-process-audit`<br>`a5eaff9b4bda` | 归档：开发分支副本 | 只读取证副本：Event Store 跨进程使用、继承和失效边界审计。 | 未直接并入 main；领先 7 / 落后 474 |
| 2026-08-21T00:01:11+08:00 | `archive/2026-08-21/codex/attempt-recovery-on-process-v3`<br>`af8a6499f397` | 归档：开发分支副本 | 只读取证副本：在进程身份约束基础上继续验证调用恢复和运行时证据同步。 | 未直接并入 main；领先 33 / 落后 473 |
| 2026-08-20T23:45:11+08:00 | `archive/2026-08-21/codex/invocation-on-canonical-v4`<br>`622dbaa977cb` | 归档：开发分支副本 | 只读取证副本：调用生命周期上下文解绑的第四版规范候选；其节点已成为 main 的祖先。 | 已作为祖先进入 main；领先 0 / 落后 472 |
| 2026-08-20T23:24:26+08:00 | `archive/2026-08-21/codex/notion-sync-v2`<br>`cded75eb5341` | 归档：开发分支副本 | 只读取证副本：Notion 证据同步与运行时回读的第二版实验分支。 | 未直接并入 main；领先 30 / 落后 481 |
| 2026-08-20T23:23:15+08:00 | `archive/2026-08-21/codex/process-identity-foundation`<br>`ca02903b38f5` | 归档：开发分支副本 | 只读取证副本：进程身份、fork 继承拒绝和资源所有权的基础分支；其节点已进入 main。 | 已作为祖先进入 main；领先 0 / 落后 473 |
| 2026-08-20T23:11:13+08:00 | `archive/2026-08-21/reflog/authorization-boundary-hardening`<br>`a06d7d290109` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：docs: record authorization boundary hardening | 未直接并入 main；领先 14 / 落后 493 |
| 2026-08-20T22:25:54+08:00 | `archive/2026-08-21/codex/attempt-recovery-integrated-v2`<br>`8d82dd7e672d` | 归档：开发分支副本 | 只读取证副本：调用尝试恢复与事务一致性的第二版集成候选。 | 未直接并入 main；领先 24 / 落后 481 |
| 2026-08-20T22:25:36+08:00 | `archive/2026-08-21/codex/attempt-recovery-safe-v2`<br>`7f9530010037` | 归档：开发分支副本 | 只读取证副本：调用恢复第二版的保守安全候选，用于隔离验证失败边界。 | 未直接并入 main；领先 25 / 落后 482 |
| 2026-08-20T21:27:04+08:00 | `archive/2026-08-21/worktree/opauth-opaque-operation-integration`<br>`e99f809f218a` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：feat: issue opaque authorized operations | 未直接并入 main；领先 2 / 落后 482 |
| 2026-08-20T21:17:53+08:00 | `archive/2026-08-21/worktree/replay-structured-dispatch-draining`<br>`b877b6dd786c` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：docs: define structured dispatch draining | 未直接并入 main；领先 16 / 落后 484 |
| 2026-08-20T21:06:06+08:00 | `archive/2026-08-21/worktree/attempt-error-consistency`<br>`2f7117307faa` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：docs: define attempt error consistency | 未直接并入 main；领先 8 / 落后 484 |
| 2026-08-20T21:01:35+08:00 | `archive/2026-08-21/reflog/issuer-fork-identity`<br>`1aa82c972f94` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：style: format issuer fork identity assertion | 未直接并入 main；领先 11 / 落后 493 |
| 2026-08-20T20:54:55+08:00 | `archive/2026-08-21/codex/attempt-recovery-v1`<br>`e05357bebde6` | 归档：开发分支副本 | 只读取证副本：调用恢复第一版实验，重点验证 invocation 事务证据认证。 | 未直接并入 main；领先 60 / 落后 646 |
| 2026-08-20T20:40:04+08:00 | `archive/2026-08-21/codex/backup-v2-codec`<br>`bcba6a419fbe` | 归档：开发分支副本 | 只读取证副本：精确备份 manifest v2 编解码与可复现序列化实验。 | 未直接并入 main；领先 3 / 落后 484 |
| 2026-08-20T20:15:56+08:00 | `archive/2026-08-21/worktree/opauth-process-bound-compose`<br>`23183fd0cde1` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：feat: compose process-bound action-time operation authorization | 未直接并入 main；领先 3 / 落后 493 |
| 2026-08-20T19:37:55+08:00 | `archive/2026-08-21/reflog/fork-inherited-invocation-store`<br>`d0f93b393e98` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：fix: reject fork-inherited invocation stores | 未直接并入 main；领先 58 / 落后 646 |
| 2026-08-20T19:18:34+08:00 | `archive/2026-08-21/worktree/operation-auth-boundary`<br>`3609ab806fc7` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：docs: define protected operation boundary | 未直接并入 main；领先 4 / 落后 486 |
| 2026-08-20T19:04:10+08:00 | `archive/2026-08-21/reflog/hostile-auth-fault-boundary`<br>`bddecdf5d4c1` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：docs: specify hostile authorization fault boundary | 未直接并入 main；领先 6 / 落后 493 |
| 2026-08-20T18:20:58+08:00 | `archive/2026-08-21/reflog/action-time-reauthorization`<br>`3af4ded12c6d` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：docs: require action-time operation reauthorization | 未直接并入 main；领先 7 / 落后 493 |
| 2026-08-20T17:34:42+08:00 | `archive/2026-08-21/worktree/recovery-durable-invocation`<br>`276913ce69ae` | 归档：临时 worktree | 从临时或 detached worktree 保存的历史节点，仅供追溯。 节点主题：fix: reconcile durable invocation transactions | 未直接并入 main；领先 17 / 落后 493 |
| 2026-08-20T17:15:21+08:00 | `archive/2026-08-21/reflog/invocation-enqueue-reconcile`<br>`eaab78181289` | 归档：reflog 救援 | 从本地 reflog 恢复的历史节点，仅供追溯。 节点主题：fix: reconcile invocation enqueue commits | 未直接并入 main；领先 56 / 落后 646 |
| 2026-08-20T15:58:05+08:00 | `archive/2026-08-21/codex/request-context-audit-20260820`<br>`a6b4a4947138` | 归档：开发分支副本 | 只读取证副本：请求上下文、凭据异常和 traceback 泄露边界审计。 | 未直接并入 main；领先 6 / 落后 499 |
| 2026-08-20T15:07:35+08:00 | `archive/2026-08-21/gate-a-trusted-context-foundation`<br>`c95635beb726` | 归档：Gate A 副本 | 只读取证副本：Gate A 可信上下文、凭据擦除失败和请求边界的基础候选。 | 未直接并入 main；领先 21 / 落后 588 |
| 2026-08-20T10:41:51+08:00 | `archive/2026-08-21/codex/supply-policy-latest-audit`<br>`11dfa301de69` | 归档：开发分支副本 | 只读取证副本：依赖采用、回滚和风险晋级规则的较新审计候选。 | 未直接并入 main；领先 15 / 落后 618 |
| 2026-08-20T10:35:28+08:00 | `archive/2026-08-21/codex/supply-policy-current-audit`<br>`ff663e9165e7` | 归档：开发分支副本 | 只读取证副本：依赖风险策略在当时当前代码上的审计与数值边界修复。 | 未直接并入 main；领先 14 / 落后 646 |
| 2026-08-20T09:50:26+08:00 | `archive/2026-08-21/codex/safe-logging-current-20260820090738`<br>`f683ef508547` | 归档：开发分支副本 | 只读取证副本：安全日志候选在当时当前代码上的集成与取消异常收敛。 | 未直接并入 main；领先 16 / 落后 652 |
| 2026-08-20T09:47:15+08:00 | `archive/2026-08-21/codex/supply-policy-v1`<br>`28959b497c86` | 归档：开发分支副本 | 只读取证副本：依赖风险晋级与供应链策略第一版。 | 未直接并入 main；领先 9 / 落后 682 |
| 2026-08-20T09:08:23+08:00 | `archive/2026-08-21/codex/safe-logging-v1`<br>`88c3d921f31c` | 归档：开发分支副本 | 只读取证副本：复合凭据字段脱敏和安全日志第一版。 | 未直接并入 main；领先 18 / 落后 694 |
| 2026-08-20T08:48:58+08:00 | `archive/2026-08-21/agent/evidence-ledger-current`<br>`0cdfaaa8a8ab` | 归档：Agent 分支副本 | 只读取证副本：面向当时较新代码节点刷新的工程证据账本候选。 | 未直接并入 main；领先 2 / 落后 683 |
| 2026-08-20T08:48:34+08:00 | `archive/2026-08-21/codex/supply-chain-docs-v1`<br>`165a92c1c173` | 归档：开发分支副本 | 只读取证副本：供应链安全里程碑、证据顺序和后续门禁文档。 | 未直接并入 main；领先 4 / 落后 682 |
| 2026-08-20T08:41:43+08:00 | `archive/2026-08-21/codex/service-config-v1`<br>`525161ed70bf` | 归档：开发分支副本 | 只读取证副本：服务配置校验、环境边界和配置测试第一版。 | 未直接并入 main；领先 10 / 落后 694 |
| 2026-08-20T08:38:36+08:00 | `archive/2026-08-21/agent/evidence-ledger`<br>`7d46958f18ec` | 归档：Agent 分支副本 | 只读取证副本：早期工程证据账本分支，用于汇总测试、审计和阶段结论。 | 未直接并入 main；领先 3 / 落后 694 |
| 2026-08-20T08:26:01+08:00 | `archive/2026-08-21/codex/service-boundary-v1`<br>`819b74cd1b45` | 归档：开发分支副本 | 只读取证副本：服务运行边界、信任假设和安全策略绑定第一版。 | 未直接并入 main；领先 7 / 落后 697 |
| 2026-08-20T08:24:30+08:00 | `archive/2026-08-21/codex/backup-manifest-v1`<br>`87bd23a7a6bd` | 归档：开发分支副本 | 只读取证副本：备份状态证据和 manifest v1 边界的早期实现。 | 未直接并入 main；领先 4 / 落后 697 |
| 2026-08-20T08:14:17+08:00 | `archive/2026-08-21/dangling/approval-recovery-chains`<br>`7bd31f947bdc` | 归档：孤立提交 | 保存当时无分支引用的提交，防止垃圾回收后丢失。 节点主题：validate staged approval recovery chains | 未直接并入 main；领先 1 / 落后 700 |
| 2026-08-20T08:10:46+08:00 | `archive/2026-08-21/dangling/approval-decision-atomicity`<br>`ab0ef94b8bf7` | 归档：孤立提交 | 保存当时无分支引用的提交，防止垃圾回收后丢失。 节点主题：validate staged approval decision atomicity | 未直接并入 main；领先 1 / 落后 702 |
| 2026-08-20T08:06:15+08:00 | `archive/2026-08-21/dangling/approval-request-atomicity`<br>`f596a7becf08` | 归档：孤立提交 | 保存当时无分支引用的提交，防止垃圾回收后丢失。 节点主题：validate staged approval request atomicity | 未直接并入 main；领先 1 / 落后 703 |
| 2026-08-20T08:02:44+08:00 | `archive/2026-08-21/dangling/approval-snapshots`<br>`c2d8ba70bf93` | 归档：孤立提交 | 保存当时无分支引用的提交，防止垃圾回收后丢失。 节点主题：validate staged approval snapshots | 未直接并入 main；领先 1 / 落后 704 |

## 本机 Worktree 目录

`main` 固定在 `/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement`。当前另有 6 个辅助 linked worktree；它们统一位于 `/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement/worktrees`，完成后必须合并、推送并移除。

| 状态 | 分支/模式 | HEAD | 路径 |
| --- | --- | --- | --- |
| 正式主线工作区 | `main` | `56bd364bddd7` | `/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement` |
| 存在、干净 | `dev_im_persistence_accelerator_20260830` | `51dbb1e5bd9a` | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_im_persistence_accelerator_20260830` |
| 存在、干净 | `dev_research_docs_accelerator_20260830` | `b0e5611aeb6c` | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_research_docs_accelerator_20260830` |
| 存在、干净 | `dev_wanwork_quantum_entanglement` | `facc7e319009` | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_wanwork_quantum_entanglement` |
| 存在、干净 | `dev_web_first_accelerator_20260830` | `aa1daf471ab8` | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_web_first_accelerator_20260830` |
| 存在、有未提交修改 | `mainline_continue_quantum_entanglement` | `0d996fcaf29c` | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/mainline_continue_quantum_entanglement` |
| 存在、干净 | `scoped_lease_process_matrix` | `4fd6588e8ca2` | `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/scoped_lease_process_matrix` |

## 固定版本标签

| 标签 | 指向提交 | 标签对象 | 时间 |
| --- | --- | --- | --- |
| `v0.2.0-web-im-20260830` | `cee65799732a` | `a383e8bee700` | 2026-08-30T12:36:36+08:00 |
| `pre-native-im-20260827-200010` | `1d399e555fb0` | `cf0ff334e4a9` | 2026-08-27T20:00:45+08:00 |
| `v0.1.0-local-trial.2` | `3f9feaa72dfd` | `655ac8c1eb8e` | 2026-08-21T23:57:36+08:00 |
| `v0.1.0-local-trial.1` | `297d53203211` | `8efc888b434e` | 2026-08-21T22:00:15+08:00 |

## 以后如何新增分支而不弄乱 execute

从主线创建分支时，直接把 worktree 放进统一目录：

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement
git fetch origin
git worktree add /Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement/worktrees/<目录名> \
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

1. `main` 永远是唯一正式主线；阶段分支不能自封为发布分支。
2. Web IM/Agent Store 阶段在未合并前只在对应 `dev_*` worktree 验收；是否合并由用户审阅后决定。
3. 每个小改动继续独立提交；阶段完成且获准后才合并回 `main` 并推送远端。
4. 新 worktree 一律建在 `/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement/worktrees`。
5. 推送成功后删除已完成的本地 worktree 和本地阶段分支，不长期堆积。
6. 删除远端 active 分支前，必须确认提交已进入 `main` 或已有同 SHA 的 `archive/*` 冻结引用。
7. `archive/*` 只用于保全证据，不在其中开发、不移动其尖端。
8. 删除 worktree 前先确认状态干净、提交已推送；使用 `git worktree remove`，不要直接删目录。
9. 每次新增、移动或删除分支/worktree 后运行目录更新脚本并提交生成结果。
