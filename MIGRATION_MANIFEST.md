# Quantum Entanglement 工作区迁移清单

迁移日期：2026-08-22（Asia/Shanghai）

## 提升到根目录的正式主线

```text
/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement
branch: main
扁平化时 HEAD: 75d02192c842b358bdff6a5af18c69dec3044cb1
```

下面出现的两个旧路径只用于记录已经完成的迁移历史，**不是当前入口，也不能复制到脚本或操作
命令中**：主线仓库先从 `execute/quantum_entanglement` 移入
`infinite/quantum_entanglement/main`，随后提升为本文件上方列出的正式根仓库。两次移动后均执行
`git worktree repair`，修复全部 linked worktree 的 common Git directory 指针。迁移没有切换
分支、改写提交或改变远端。

## 迁入 worktrees 的 13 个历史工作区

所有工作区迁移前均为干净状态。迁移使用 `git worktree move`，分支名、HEAD 和 Git 管理关系
保持不变。

| 原目录名 | 分支 | 迁移时 HEAD | 新位置 |
| --- | --- | --- | --- |
| `qe-attempt-recovery-on-process-v3` | `codex/attempt-recovery-on-process-v3` | `af8a6499f397` | `worktrees/qe-attempt-recovery-on-process-v3` |
| `qe-attempt-recovery-safe-v2` | `codex/attempt-recovery-safe-v2` | `7f9530010037` | `worktrees/qe-attempt-recovery-safe-v2` |
| `qe-backup-v2-exact-topology` | `codex/backup-v2-exact-topology` | `bc95912a7407` | `worktrees/qe-backup-v2-exact-topology` |
| `qe-backup-v2-on-canonical-v1` | `codex/backup-v2-on-canonical-v1` | `0b044d0dd63a` | `worktrees/qe-backup-v2-on-canonical-v1` |
| `qe-backup-v2-snapshot-derivation` | `codex/backup-v2-snapshot-derivation` | `3946847b1ece` | `worktrees/qe-backup-v2-snapshot-derivation` |
| `qe-ci-linux-test-fix` | `codex/ci-linux-test-fix` | `bbef2e02642b` | `worktrees/qe-ci-linux-test-fix` |
| `qe-ci-package-fix` | `codex/ci-package-fix` | `7755d83bd092` | `worktrees/qe-ci-package-fix` |
| `qe-event-store-process-binding-v1` | `codex/event-store-process-binding-v1` | `80dad143e7d5` | `worktrees/qe-event-store-process-binding-v1` |
| `qe-gate-a-operation-auth` | `codex/gate-a-operation-authorization` | `1dd4247b0a1b` | `worktrees/qe-gate-a-operation-auth` |
| `qe-local-product-trial-v1` | `codex/local-product-trial-v1` | `8dfff5e99cf8` | `worktrees/qe-local-product-trial-v1` |
| `qe-notion-sync-v2` | `codex/notion-sync-v2` | `cded75eb5341` | `worktrees/qe-notion-sync-v2` |
| `qe-py39-test-isolation-fix` | `codex/py39-test-isolation-fix` | `04298b429a34` | `worktrees/qe-py39-test-isolation-fix` |
| `qe-recovery-integrate-current` | `codex/recovery-integrate-current` | `e21654a06663` | `worktrees/qe-recovery-integrate-current` |

这些分支默认全部只读。继续产品主线时使用 `main`，不要在这些历史 worktree 中接着开发。

## 迁入 artifacts 的非 Git 目录

| 原目录 | 性质 | 新位置 |
| --- | --- | --- |
| `execute/qe-opauth-review-v6` | 操作授权评审覆盖层、复现脚本与多版本 pycache；不是 Git worktree | `artifacts/qe-opauth-review-v6` |
| `execute/qe_release_evidence` | 两份早期 release-evidence JSON；不是 Git worktree | `artifacts/qe_release_evidence` |
| 外层分支管理仓库 | 扁平化前的 7 个本地管理提交及完整 `.git` 历史 | `artifacts/branch-hub-legacy-repo` |

## 清理的失效登记

迁移后执行了 `git worktree prune --verbose`，清除了 29 条指向已经不存在的 `/private/tmp`
路径的失效 worktree 管理记录。清理前已逐一验证：每个 HEAD 均至少被一个 GitHub 远端分支或
归档引用包含，因此没有删除提交或丢失历史。

仍然存在的 `/private/tmp/qe-main-*-package.*/source-*` 是实际存在、状态干净的 detached 发布
复现工作区，不位于 `execute` 根目录，本次没有擅自删除。
