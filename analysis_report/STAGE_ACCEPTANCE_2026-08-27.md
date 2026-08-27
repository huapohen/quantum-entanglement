# 阶段验收检查点：Worktree 收口、v0版命名与 Result Observation

> 检查点日期：2026-08-27（Asia/Shanghai）  
> 正式分支：`main`  
> 正式目录：`/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement`  
> 阶段结论：**可以停下来做阶段验收；不是生产发布，Gate A–E 全部保持关闭。**

## 1. 结论先行

本轮已经收在一个没有半开写路径的安全边界：

1. 本机只保留正式 `main` worktree 和 `main` 本地分支；
2. 远端不再保留 `agent/*`、`codex/*`、`gate-*` 等活动临时开发分支；
3. 历史尖端先按同 SHA 保存到 `archive/*`，再删除活动分支，不丢证据；
4. schema-2 result request、evidence、terminal transition、receipt 与 capability-free
   `ObservedV2` 已形成完整纯值契约；
5. `ObservedV2` 已通过 exact codec、复制、全 pickle protocol、篡改、实例方法遮蔽和
   `object.__new__` 失败关闭测试；
6. 真正的 result store writer、fresh-COMMIT `AcceptedV2`、migration 5 和 worker dispatch
   仍未启用，避免把“合法数据形状”误当成“已持久化权限”；
7. 当前主仓活动文本统一使用“v0版”，并增加自动术语门禁；
8. Notion 和私人语雀的本次更名由用户手动完成，本轮不再访问或修改这两个远端。

因此，现在可以先加入其他参考项目并重新综合评估，不需要担心下一轮从一个半完成的数据库
迁移或半开放的执行权限路径继续。

## 2. Git 与 Worktree 验收事实

### 2.1 本机

| 检查项 | 当前状态 | 验收标准 |
| --- | --- | --- |
| 正式 worktree | 1 个 | 只存在正式主目录 |
| 辅助 linked worktree | 0 个 | `.git/worktrees` 无残留 |
| 本地分支 | 仅 `main` | 无已完成阶段分支 |
| 工作区 | 阶段交付提交后应 clean | `git status --short` 无输出 |
| 主入口 | `main` | 日常开发、启动、验收都从这里开始 |

### 2.2 GitHub 远端

远端清理后的 55 个分支引用分为：

| 类型 | 数量 | 用途 |
| --- | ---: | --- |
| `main` | 1 | 唯一正式主线 |
| `archive/*` | 49 | 冻结、只读取证，不在其中继续开发 |
| `dependabot/*` | 5 | 仍关联依赖升级候选/开放 PR，不属于 worktree |
| 其他活动临时分支 | 0 | 已完成分支均已归档后删除 |

权威目录见仓库根部 `BRANCH_CATALOG.md`；生命周期规则见
`docs/BRANCH_WORKSPACE.md`。后续临时 worktree 只能放在仓库内 `worktrees/`，完成后必须按
“提交 → 验证 → 合回 main → 推送 → 必要时同 SHA 归档 → 删除 worktree 和阶段分支”的顺序
收口。

## 3. 本阶段代码边界

### 3.1 已完成的 result contract

```text
ScopedInvocationResultAcceptanceRequestV2
        │ exact request / manifest / ordered artifacts
        ▼
ScopedInvocationResultEvidenceV2
        │ PURE + retryClass=never + start/attempt/fence binding
        ▼
ScopedInvocationResultTerminalTransitionV2
        │ RUNNING@revision → COMPLETED@revision+1
        ▼
ScopedInvocationResultReceiptV2
        │ self-verifying digest + full coordinate graph
        ▼
ScopedInvocationResultObservedV2
        └ capability-free durable-observation value

        [尚未连接：store writer / fresh COMMIT / AcceptedV2]
```

关键提交：

| Commit | 交付 |
| --- | --- |
| `075e849` | exact scoped result acceptance request |
| `1caa7c4` | request/result drift 矩阵 |
| `c761c0c` | result evidence snapshot 加固 |
| `1fc41cf` | scoped terminal transition |
| `a36032e` | self-verifying Result ReceiptV2 |
| `3b1766f` | durable observation value `ObservedV2` |
| `4efec6a` | 当前活动报告统一为“v0版” |
| `f47a3f7` | 自动术语门禁 |

### 3.2 `ObservedV2` 能证明什么

它能证明：

- wire shape 是 exact schema；
- 内含 ReceiptV2 的自摘要和内部绑定关系正确；
- 默认 copy/deepcopy/trusted pickle 重建会重新验证 receipt；
- 被篡改的 receipt 不能跨过 codec、复制或 pickle 边界；
- 它不携带 fresh-COMMIT authority，也不被包顶层公开导出。

它不能单独证明：

- 数据来自某个真实 SQLite durable row；
- 本次调用亲自完成了一次新写入并正常收到 COMMIT ACK；
- 调用者可以据此执行 worker dispatch、connector 副作用或生产晋级。

生产代码未来只能在完整 store readback 成功后返回 `ObservedV2`。任何调用方自行构造一个
codec-valid 对象，都不能把它升级为持久化事实或权限。

### 3.3 有意保持关闭的能力

| 能力 | 当前状态 | 为什么停在这里 |
| --- | --- | --- |
| canonical stored-event envelope digest | 未实现 | receipt 里的 digest 仍是 capability-free opaque value |
| generic append reserved-event fence | 未实现 | writer 开放前必须先阻止旁路伪造 canonical result event |
| atomic result store writer | 未实现 | 还没有把 request/start/lease/artifact/event/receipt/outbox 组成同一事务 |
| `AcceptedV2` | 未实现 | 只能在 fresh COMMIT 正常 ACK 后由 store 签发 |
| migration 5 | 未注册 | fleet floor、backup-v2、非空恢复拓扑尚未闭合 |
| worker dispatch | disabled | result authority 和恢复边界尚未闭合 |
| 真实 IM/外部 connector | disabled | Gate A–E 与 action-time authorization 尚未通过 |

## 4. 术语与远端文档边界

当前展示名称统一为 **v0版**。详细规则见 `docs/TERMINOLOGY.md`。

- 当前主仓活动源码、报告、同步源、教程和新计划必须使用规范名称；
- 上游机器名 `agent_atore_demo`、commit、URL、Notion page ID 与语雀 slug 保持原样；
- 已有 Git 历史、`archive/*`、用户原始请求、automation 输入和冻结证据不重写；
- Notion/私人语雀由用户手动更名，本轮没有执行助手侧远端写入或回读；
- `analysis_report/yuque_sync/mapping.json` 只把标题变更标为
  `user_reported_remote_rename_not_read_back`，不伪造实时验证。

## 5. 自动验证基线

阶段收口提交前后执行以下门禁：

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement

.venv/bin/ruff format --check src tests scripts
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
PYTHONPATH=src .venv/bin/python -m pytest
git diff --check
```

ObservedV2 落地时的全量结果为：

- `1498 passed`；
- `75 warnings`，全部来自 Python 3.13 对 fork 的弃用提醒；
- ruff format/check 通过；
- mypy 通过；
- `git diff --check` 通过。

加入术语门禁和本阶段文档后，最终收口又完整执行了一次：

- `1499 passed, 75 warnings in 34.98s`；
- ruff format 检查 `139 files already formatted`；
- ruff lint 为 `All checks passed`；
- mypy 为 `Success: no issues found in 45 source files`；
- focused result/branch/terminology 组合为 `28 passed`。

两次全量运行的 75 条 warning 均是同一类 Python 3.13 fork 弃用提醒，没有新增测试失败或
未分类 warning。

### 5.1 只核验本轮新增能力

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_invocation_result_receipt.py \
  tests/test_invocation_result_observed.py \
  tests/test_terminology.py \
  tests/test_branch_catalog.py
```

### 5.2 核验分支和 worktree

```bash
git status --short
git worktree list --porcelain
git branch --format='%(refname:short)'
.venv/bin/python scripts/branch_catalog.py --check
```

`git worktree list` 应只显示正式主目录，`git branch` 应只显示 `main`。目录检查会把纯
catalog 提交前的产品基线作为 main baseline，避免生成文件引用自己的 commit 形成循环。

## 6. 产品验收入口

当前本地产品切片仍可用于阶段体验：

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement
./scripts/start_local_trial.sh
```

完整教程：`docs/LOCAL_PRODUCT_TRIAL.md`。验收重点：

1. 页面只监听 loopback；
2. 可以输入任意自定义指令，不是固定示例；
3. 配置有效模型后，分析、生成、复核 Agent 会形成可见任务链；
4. 页面展示三个版本化 Markdown Artifact、任务 DAG、模型 narration 和事件链；
5. 未连接飞书、企微或真实外部 connector；
6. 页面明确显示 `productionApproved=false` 与 `A-E closed`；
7. 结束时在启动终端按 `Ctrl-C`，服务关闭且临时页面 token 失效。

模型配置、凭据规则和故障排查都在教程中。任何排障输出只允许显示凭据前缀、长度和
SHA-256 短指纹，不得输出完整 Key。

## 7. 阶段验收清单

请按下面顺序验收：

- [ ] GitHub `main` 能看到本阶段所有提交；
- [ ] 本机只有一个正式 worktree 和一个本地 `main` 分支；
- [ ] `BRANCH_CATALOG.md` 的分类与远端 refs 一致；
- [ ] 当前主仓活动内容使用“v0版”；
- [ ] `tests/test_terminology.py` 通过；
- [ ] Result ReceiptV2/ObservedV2 focused tests 通过；
- [ ] 全量 pytest、ruff、mypy 通过；
- [ ] 本地试用脚本能启动，任意自定义指令可运行；
- [ ] UI 仍明确声明非生产、Gate A–E 关闭；
- [ ] 没有真实 connector、worker dispatch、migration 5 或 Accepted authority 被误启用；
- [ ] Notion/语雀不需要助手继续操作。

## 8. 已知限制与验收口径

“阶段完成”只表示：

- worktree/分支治理已收口；
- 当前 result 纯值契约与观察值边界已闭合；
- 可以安全暂停、加入新参考项目、重新评估后再继续。

它不表示：

- exactly-once 外部副作用已经实现；
- SQLite 结果图已经由原子 writer 写入；
- 多租户生产身份、KMS、HA、RPO/RTO、容量、SLO 或 Kubernetes 已验证；
- Gate A–E 任一已经开放或通过；
- 产品达到生产商用级别。

更完整的生产边界以 `docs/production/CURRENT_READINESS.md`、
`docs/production/RELEASE_GATES.md` 和 `docs/production/ADR_0005_ATOMIC_RESULT_AUTHORITY.md`
为准。

## 9. 继续开发的唯一入口

阶段验收或新增参考项目评估完成后，只从远端 `main` 最新节点继续。不要从 `archive/*`、
Dependabot 分支或旧本地副本直接续写。下一阶段的提交级计划见
`analysis_report/NEXT_STAGE_PLAN.md`。
