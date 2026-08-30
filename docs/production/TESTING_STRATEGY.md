# 测试与回归节奏

> 适用分支：`mainline_continue_quantum_entanglement`  
> 更新日期：2026-08-30

## 为什么有 2,975 项

2,975 是当前评审分支由 pytest 收集到的测试用例总数（截至 2026-08-30 实测），不是本轮每个 commit 都要执行的数量。
最近一次跨领域集成封板在 `4783f61` 记录了 2,964 项全量通过；随后回归选择器新增 5 项自身
选择逻辑测试，后续实现又新增了 6 项覆盖。测试库存会随参数化用例和测试文件合并而变化。测试库存的
增长只说明覆盖面变化，不能用来推导单次改动的回归范围。

**硬规则：小改动不跑库存总数。** 默认命令按变更路径选择最小充分门禁；只有明确使用
`--full`、阶段封板、跨层合并或用户验收前才跑全量。看到“2,975 collected”只代表盘点库存，
不是失败，也不是每个 commit 的待办。

## 分层门禁

每个 commit 不需要等待全量回归。提交按影响范围选择最小充分门禁：

| 变更类型 | 提交前门禁 | 典型耗时 | 何时跑全量 |
|---|---|---:|---|
| 单文件实现/修复 | 受影响测试文件或单测节点 + Ruff | 1–10 秒 | 阶段封板 |
| 跨模块运行 seam | 相关组合测试 + Ruff + strict mypy/compile | 10–60 秒 | 阶段封板 |
| schema/transaction/process boundary | 专项矩阵 + 双连接/进程测试 + diff-check | 10–90 秒 | 阶段封板 |
| 文档、证据、索引 | `git diff --check` | <1 秒 | 不需要 |
| 用户可验收阶段 | 全量 pytest + Ruff + strict mypy + compileall | 约 2–3 分钟 | 必跑 |

## 当前节点的可复现命令

快速反馈：

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_scoped_lease_lifecycle.py \
  tests/test_invocation_worker_lifecycle.py \
  tests/test_result_acceptance_worker_integration.py
.venv/bin/ruff check \
  src/quantum_entanglement/invocation_worker_lifecycle.py \
  src/quantum_entanglement/store.py \
  tests/test_scoped_lease_lifecycle.py \
  tests/test_invocation_worker_lifecycle.py
```

阶段封板：

```bash
PYTHONPATH=src .venv/bin/pytest --collect-only  # 当前 2,969 项，仅盘点库存
PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/ruff check src tests
PYTHONPATH=src .venv/bin/python -m mypy --strict src
PYTHONPATH=src .venv/bin/python -m compileall -q src
git diff --check
```

## 提交纪律

1. 先跑快速门禁，再提交一个独立、可回滚的 commit；
2. 立即 push 到 review branch，记录 commit、测试命令和结果；
3. 只有跨阶段节点才跑完整回归，并把结果写到 `analysis_report/research/`；
4. 若快速门禁失败，先修复再提交，不用全量回归掩盖局部失败；
5. 文档提交不触碰运行代码，也不因文档变更重新等待全量测试。

### 影响面选择规则

先用 `git diff --name-only <base>...HEAD` 列出本 commit 触及的路径，再按下面规则取最小充分
集合：

- `docs/`、`analysis_report/`、HTML、索引：只做 `git diff --check` 和对应生成器/链接检查；
- `src/quantum_entanglement/<module>.py`：运行同名或直接依赖的 `tests/test_<module>.py`；
- `store.py`、事务、schema、lease、process boundary：运行对应专项矩阵，再加 Ruff、strict
  mypy、compileall；
- `apps/im-api/`：在该模块目录运行 `go test ./...`，必要时加 `go test -race ./...` 和 `go vet`；
- `clients/im-web/`：运行 `npm run build`，再跑 `scripts/verify_web_first.sh`；
- 跨上述两层的合并或用户验收节点：各层专项全部通过后，只在封板时跑一次全量 pytest。

当前 2,964 项全量回归的证据见
[`50_mainline_web_im_integration_regression_20260830.md`](../../analysis_report/research/50_mainline_web_im_integration_regression_20260830.md)。

### 自动选择入口

不想手工判断时，在仓库根目录运行：

```bash
# 查看当前工作区/暂存区会选择哪些门禁
PYTHONPATH=src .venv/bin/python scripts/regression_gate.py --dry-run

# 执行最小充分门禁
PYTHONPATH=src .venv/bin/python scripts/regression_gate.py

# 对某个已提交节点相对基线做影响面回归
PYTHONPATH=src .venv/bin/python scripts/regression_gate.py --base origin/mainline_continue_quantum_entanglement~1

# 用户验收/阶段封板时显式跑全量
PYTHONPATH=src .venv/bin/python scripts/regression_gate.py --full
```

脚本遇到无法映射的运行时代码会自动升级到 Python 全量门禁，宁可多跑也不会静默漏测；只改
文档时只执行差异检查。`apps/im-api/README.md`、`clients/im-web/README.md` 等模块文档也按文档
处理，不会因为目录前缀误触发 Go/Web 门禁（修复证据见
[`57_regression_gate_scope_fix_20260830.md`](../../analysis_report/research/57_regression_gate_scope_fix_20260830.md)）。
脚本不执行 `npm ci`，缺少 Web 依赖时会明确失败并提示先按锁文件安装。

全量通过只证明当前记录环境的源码和断言成立，不代表生产 GA；外部 IM、飞书、企微、模型
出网和 connector 仍由独立 Gate 控制。
