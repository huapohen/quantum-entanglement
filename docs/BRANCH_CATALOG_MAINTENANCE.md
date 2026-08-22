# 分支目录维护说明

完整分支导航按用户指定存放在本机：

```text
/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement/BRANCH_CATALOG.md
```

该文档由 `scripts/branch_catalog.py` 根据 Git 引用和 worktree 状态生成。分支的人工用途说明
维护在 `docs/branch_catalog_metadata.json`，因此说明来源与生成工具都随主仓库提交、评审和推送；
生成结果由 `execute/infinite/quantum_entanglement` 下独立的本地管理仓库保存历史，不把大体积
worktree 或评审产物提交进去。

更新远端引用并重新生成：

```bash
./scripts/update_branch_catalog.sh --fetch
```

只检查当前目录是否已经与 Git 状态一致：

```bash
./scripts/update_branch_catalog.sh --check
```

新分支如果没有人工说明，会自动进入目录并标记为“用途待补充”。在元数据 JSON 的 `branches`
对象中加入分支名和用途后重新生成即可。

注意：Git 不保存可靠的分支创建时间。目录里的“节点时间”是分支尖端提交时间，不能解释为
分支创建时间。
