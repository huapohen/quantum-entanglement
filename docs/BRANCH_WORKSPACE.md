# Quantum Entanglement 分支工作区总入口

这里集中管理 Quantum Entanglement 的分支、临时 worktree 与本地评审证据，避免继续占满
`/Users/lwblx/huapohen/agent/execute` 根目录。正式产品主线仍是 `main`；当前另外保留
`mainline_continue_quantum_entanglement` 专用 worktree，作为原生 IM E2 provider-bundle 离线闭环的
人工评审空间。该评审分支不会自动合并回 `main`，需要用户验收后再决定后续集成。

## 首先看什么

- [`BRANCH_CATALOG.md`](../BRANCH_CATALOG.md)：58 个远端分支的时间节点、用途、相对 `main`
  的关系、推荐用法以及本机 worktree 路径。
- 正式仓库根：唯一正式主线仓库；日常开发、启动体验和恢复已批准的主线任务使用这里。
- `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/mainline_continue_quantum_entanglement`：
  当前原生 IM 人工评审工作区；在用户验收和明确决定前保留，不合并 `main`、不删除。
- `/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/`：其他临时阶段工作区的统一
  容器；是否合并必须按各分支的评审结论处理，不能机械合并。
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
2. 每个提交先推送对应远端分支，确保评审节点可恢复。
3. 用户明确批准集成后，才把已审阅提交合并或挑选进入 `main`，并重新执行与风险相称的回归检查。
4. 需要保留独立历史尖端时，建立并核对同 SHA 的 `archive/*`；随后才可删除远端阶段 active 分支。
5. 删除本地 worktree 前必须确认状态干净、提交已推送且用户不再需要该评审空间。

当前例外：`mainline_continue_quantum_entanglement` 是保留中的人工评审分支。它已推送远端，但在用户
验收前不执行合并、归档、删分支或删 worktree。

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
