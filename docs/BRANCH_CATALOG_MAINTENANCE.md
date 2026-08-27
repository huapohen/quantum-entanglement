# 分支目录维护说明

完整分支导航按用户指定存放在本机：

```text
/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement/BRANCH_CATALOG.md
```

该文档由 `scripts/branch_catalog.py` 根据 Git 引用和 worktree 状态生成。分支的人工用途说明
维护在 `docs/branch_catalog_metadata.json`。说明来源、生成工具和生成结果全部由主仓库提交、
评审并推送；临时 worktree 统一放在仓库外的
`/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/`，仓库内
`artifacts/` 仍由 `.gitignore` 排除。

为避免“生成目录—提交目录—main SHA 改变—目录立刻过期”的循环，若 `origin/main` 最新提交
只修改 `BRANCH_CATALOG.md`，生成器会把它的父提交作为目录基线。下一次真实代码或文档提交
仍会正常触发目录更新。

更新远端引用并重新生成：

```bash
./scripts/update_branch_catalog.sh --fetch
```

只检查当前目录是否已经与 Git 状态一致：

```bash
./scripts/update_branch_catalog.sh --check
```

检查所有已跟踪脚本和文档是否误用了迁移前路径：

```bash
./scripts/check_workspace_paths.py
```

退出码为 0 才表示路径审计通过。迁移清单中的旧路径只保留为历史证据，不能复制成新命令。

新分支如果没有人工说明，会自动进入目录并标记为“用途待补充”。在元数据 JSON 的 `branches`
对象中加入分支名和用途后重新生成即可。

注意：Git 不保存可靠的分支创建时间。目录里的“节点时间”是分支尖端提交时间，不能解释为
分支创建时间。
