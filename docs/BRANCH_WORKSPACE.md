# Quantum Entanglement 分支工作区总入口

这里集中管理 Quantum Entanglement 的分支、临时 worktree 与本地评审证据，避免继续占满
`/Users/lwblx/huapohen/agent/execute` 根目录。当前所有辅助 linked worktree 已清理，本机只保留
正式 `main` 工作树。

## 首先看什么

- [`BRANCH_CATALOG.md`](../BRANCH_CATALOG.md)：55 个远端分支的时间节点、用途、相对 `main`
  的关系、推荐用法以及本机 worktree 路径。
- 当前目录根：唯一正式主线仓库；日常开发、启动体验和恢复主线任务只使用这里。
- `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/`：只用于未完成的
  临时阶段工作区；完成后必须合并、推送并移除。
- `artifacts/qe-opauth-review-v6/`：操作授权评审遗留资料；不是 Git worktree。
- `artifacts/qe_release_evidence/`：早期发布证据 JSON；不是 Git worktree。

正式主线仍然位于：

```text
/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement
```

日常开发、启动体验和恢复主线任务时，只使用该目录的 `main` 分支。

## 更新分支目录

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement
./scripts/update_branch_catalog.sh --fetch
```

脚本会更新仓库根目录的 `BRANCH_CATALOG.md`。新分支如果需要人工用途说明，编辑
`docs/branch_catalog_metadata.json` 后重新运行脚本。说明元数据、生成工具、目录和迁移清单
现在全部由同一个主代码仓库管理并推送 GitHub；临时 worktree 统一位于仓库外的
`/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/`，仓库内
`artifacts/` 保持本地忽略。

## Worktree 收尾规则

1. 在 worktree 内完成小步提交和阶段验证。
2. 合并回 `main`，重新执行与风险相称的回归检查。
3. 先推送 `main`；需要保留独立历史尖端时，再建立并核对同 SHA 的 `archive/*`。
4. 删除远端阶段 active 分支。
5. 删除本地 worktree 和已合并本地分支；最终 `git worktree list` 只保留正式 `main`。

只检查文档是否需要更新：

```bash
./scripts/update_branch_catalog.sh --check
```

## 检查脚本和文档中的路径

任何启动脚本、维护脚本或操作文档都必须把仓库根目录作为唯一当前入口。提交前运行：

```bash
./scripts/check_workspace_paths.py
```

该检查会扫描 Git 跟踪的文本文件并拒绝已经废弃的目录写法。只有
`MIGRATION_MANIFEST.md` 中明确标注的迁移历史，以及验证旧布局兼容性的测试夹具可以保留旧
路径；它们都不是当前操作指令。
