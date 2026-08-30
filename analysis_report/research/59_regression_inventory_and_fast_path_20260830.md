# 回归库存与快速路径说明（2026-08-30）

## 结论

“2,951/2,964/2,969/2,975 项”是不同提交节点的 pytest **库存盘点数**，不是每个小改动的执行清单。
当前分支实测为 **2,975 tests collected**。测试库存随新增覆盖、参数化用例和回归选择器测试变化，
历史证据中的数字保留其当时的事实，不应互相覆盖。

## 默认执行矩阵

| 变更 | 默认门禁 | 预期反馈 |
|---|---|---:|
| 仅 Markdown/报告/HTML | `git diff --check`（含 cached） | 小于 1 秒 |
| 一个 Python 模块 | 直接同名测试/已登记高风险专项 + focused Ruff | 通常 1–10 秒 |
| 已登记的 store、lease、projection、runtime 高风险模块 | 组合专项 + focused Ruff | 通常 10–90 秒 |
| Go 源码 | `go test ./...` + `go vet ./...`（仅 Go 模块） | 按 Go 模块耗时 |
| Web 源码 | `npm run build` + Web-first synthetic | 按前端构建耗时 |
| 无法映射的 Python runtime | fail-closed 升级 Python 全量 | 只在确实无法判断影响面时 |
| 阶段封板/跨层合并/用户验收 | 显式 `--full` | 一次性全量 |

## 可复制命令

```bash
# 先看选择结果，不执行测试
PYTHONPATH=src .venv/bin/python scripts/regression_gate.py --dry-run

# 按当前工作区变更执行最小充分门禁
PYTHONPATH=src .venv/bin/python scripts/regression_gate.py

# 只审一个提交相对其父提交的影响面
PYTHONPATH=src .venv/bin/python scripts/regression_gate.py --base HEAD~1

# 仅在阶段封板或用户验收前执行
PYTHONPATH=src .venv/bin/python scripts/regression_gate.py --full
```

## 本节点证据

- `pytest -q tests/test_regression_gate.py`：7 项 selector 回归通过；
- 干净工作区运行 `--dry-run`：只选 `diff-check`、`cached-diff-check`，未选 pytest；
- 当前库存盘点：`pytest --collect-only` 报告 2,975 项；盘点本身约 0.3 秒，不执行测试体；
- 最近一次跨领域封板 `4783f61` 的 2,964 项全量通过约 152 秒。后续小 commit 不重复支付这笔成本。

## 10 小时交付纪律

开发循环固定为“一个可回滚小 commit → focused gate → push”。将同一功能的多个小 commit 视为一个
阶段，阶段结束时再跑一次全量并记录证据。除非变更无法映射到任何运行时测试，否则不得因为看到
库存数字而主动执行全量；这保证回归反馈是秒级/分钟级，而不是把 10 小时耗在重复测试上。

## 边界

快速门禁降低反馈时间，不降低安全边界：未知运行时映射仍 fail-closed；真实 IM、飞书、企微、模型
出网和 outbound 仍不启动。全量绿也只表示当前本地代码和断言通过，不等于生产 Gate A–E 已开启。
