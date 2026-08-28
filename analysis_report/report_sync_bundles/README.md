# Report sync checkpoint 索引

本目录保存按阶段生成的确定性、本地只读 report sync 库存。checkpoint 不是 Notion 或语雀
同步器，也不是远端状态探针；所有 `liveReadbackPerformed` 均为 `false`。文件名中的阶段只描述
本地库存边界，不构成远端写入、远端回读或生产晋级证明。

## 当前 checkpoint

| 文件 | 状态 | 本地库存口径 | 说明 |
|---|---|---|---|
| `checkpoint-20260829-gate-a0-cutover-plan-local-pending.json` | `current` | 70 个本地 source、71 个 target mapping、55 个 Notion source mapping、16 个语雀 local-pending mapping、35 项图像 | `3d81013` 本地源基线；纳入 Topic 35、Gate A0 production authority、immutable specification、canonical cutover plan/strict decoder、Topic 36 SVG/PNG 与同步源治理；生成后执行 `--verify`。全部 `liveReadbackPerformed=false`，Notion/语雀均未写入，明确为 `local_pending` |

`current` 是本目录唯一应被当前文档、发布检查和人工审阅当作 latest 的 checkpoint。生成与验证
命令见仓库根 [`README.md`](../../README.md)；不得在生成后继续修改它所覆盖的报告、语雀传输源、
截图或三个 control manifest 而不重新生成一个新文件名的 checkpoint。

## 已被取代的历史 checkpoint

| 文件 | 状态 | 生成 commit | 历史边界 |
|---|---|---|---|
| `checkpoint-20260828-w2-postgres-function-only-exact-access-notion-readback.json` | `superseded` | `7bb324a` / `cd92ea5` | Topic 34、31 项图像和六页 Notion 回读的最近远端已核验基线；早于 Topic 35、Gate A0 authority/plan 与 35 项图像。本地 latest 已前移，但该文件仍是当前最近完成 Notion 回读的历史证据 |
| `checkpoint-20260828-w2-postgres-notion-readback-final.json` | `superseded` | `c8b8cd1` | Topic 33 / `0001–0004` 最终批次；早于 `0005` 五函数、exact access、Topic 34、31 项图像和 `7bb324a` 六页 Notion 回读 |
| `checkpoint-20260828-w2-postgres-notion-readback.json` | `superseded` | `25d4d6c` | Topic 33 首轮七页回读与 SVG bundle 支持检查点；早于最终 bookkeeping 回写和截图证据页 29 项库存/附件回读 |
| `checkpoint-20260828-w1-im-contract-remote-backup-readback.json` | `superseded` | `74a01a7` | W1 IM identity/codec 远端备份回读；早于 PostgreSQL authority migration、repository、UoW 与 Topic 33 |
| `checkpoint-20260828-w1-im-identity-codec-notion-readback.json` | `superseded` | `e10ac5e` | 首轮 identity/conversation/provider metadata 合同 Notion 回读；早于远端备份检查和 W2 PostgreSQL 切片 |
| `checkpoint-20260828-w1-p1-7-notion-readback.json` | `superseded` | `f7e84b1` | Volatile Memory EventStore 与 23–31 专题同步；早于 identity/codec 与 PostgreSQL authority persistence |
| `checkpoint-20260828-native-im-early-integration-readback.json` | `superseded` | `9e44daf` | `f99f176` 提前接入批次：33 页 Notion 镜像、备份分支/标签与 E0–E5 marker；仍是 `main` 历史恢复证据，但不描述本开发分支当前 W2 状态 |
| `checkpoint-20260827-notion-v2-readback-final.json` | `superseded` | `1d399e5` 之前的同步基线 | 29 页固定库存与派生分支目录的最终 v2 回读；早于 V1 合同、提前接入检查点和 E0–E5 执行计划进入 33 页镜像 |
| `checkpoint-20260827-notion-v2-readback.json` | `superseded` | `fdd4bc9` | 首次 v2 回读库存；曾把最终生成的分支目录纳入固定摘要，形成目录/manifest 自引用风险，已由 final 版本改为独立派生页登记 |
| `checkpoint-20260827-v0-stage-acceptance-final.json` | `superseded` | `d67036a` | 审计修正后的最终 v0版阶段库存；早于用户重新授权 Notion 全量同步、29 页回读和 v2 manifest |
| `checkpoint-20260827-v0-stage-acceptance.json` | `superseded` | `d06af05` | 首次 v0版阶段验收库存；早于 Gate 措辞修正、远端镜像 opt-in 政策及活动报告路径收口 |
| `checkpoint-20260827-clawith-delivery-blueprint.json` | `superseded` | `bc72d07` | Scoped atomic start/worker authority 既有边界，加上 Clawith 部门样板、运行时/发布源码复核和第 26 张官网证据；早于 v0版术语统一、Result Receipt/Observed 安全停点及阶段收口文档 |
| `checkpoint-20260827-scoped-start-clawith-worker-authority.json` | `superseded` | `e5c68e2` | Scoped atomic start、worker authority 和前 26 张图片的库存；早于部门级交付样板、第 26 张 Clawith 增量图片及后续源码复核 |
| `checkpoint-20260827-atomic-start-clawith-qa.json` | `superseded` | `6327b18` | Atomic invocation start 发布证据、Clawith QA 修正及截图 20–25 收口时的库存；早于后续 scoped-start 与 worker authority 文档修正 |
| `checkpoint-20260827-clawith-local-sync-ledger.json` | `superseded` | `15f24af` | Clawith 本地同步台账收口时的 schema v3 库存；早于 atomic-start 发布证据、Clawith QA 修正和截图 20–25 |
| `checkpoint-20260827-clawith.json` | `superseded` | `15a77d3` | Clawith 传输源后续更新前的 schema v3 库存；其 Yuque mapping control 也早于 local-sync-ledger |

`superseded` 只表示该文件不再描述当前 HEAD，也不得再被选作 latest；它不表示文件损坏或历史
证据失效。旧 JSON 必须保持原样：不得覆盖、删除、重命名或手工修改。后续每个阶段继续使用
新的 checkpoint 文件名，保留完整时间序列。

## 验证历史 checkpoint

旧 checkpoint 绑定生成时的报告、截图和 control manifest 字节，因此通常不应在当前 HEAD 上
强行验证。需要复核时，在独立 detached worktree 中检出表内生成 commit，并使用该 checkout 的
生成器验证对应文件；不要切换或清理正在工作的主目录。

```bash
git worktree add --detach <temporary-worktree> <generating-commit>
python3 <temporary-worktree>/scripts/report_sync_bundle.py \
  --repository-root <temporary-worktree> \
  --verify analysis_report/report_sync_bundles/<checkpoint-file>.json
git worktree remove <temporary-worktree>
```

验证成功只证明历史 checkpoint 与该历史 checkout 的本地库存一致。它不会进行实时远端回读，
也不能证明 Notion、语雀或任何外部系统当前仍与本地内容一致。
