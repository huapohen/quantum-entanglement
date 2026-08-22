# Quantum Entanglement 分支工作区总入口

这里集中保存 Quantum Entanglement 的历史分支 worktree 与本地评审证据，避免继续占满
`/Users/lwblx/huapohen/agent/execute` 根目录。

## 首先看什么

- [`BRANCH_CATALOG.md`](../BRANCH_CATALOG.md)：75 个远端分支的时间节点、用途、相对 `main`
  的关系、推荐用法以及本机 worktree 路径。
- 当前目录根：唯一正式主线仓库；日常开发、启动体验和恢复主线任务只使用这里。
- `worktrees/`：长期保留的历史分支工作区。默认只读，不要把它们当作新主线。
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
现在全部由同一个主代码仓库管理并推送 GitHub；`worktrees/` 与 `artifacts/` 保持本地忽略。

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
