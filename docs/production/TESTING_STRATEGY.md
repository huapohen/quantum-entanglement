# 测试与回归节奏

> 适用分支：`mainline_continue_quantum_entanglement`  
> 更新日期：2026-08-30

## 为什么有 2,958 项

2,958 是当前 pytest 收集到的测试用例总数，不是本轮新增数量。`509e593` 基线已有 2,949
项；本轮生命周期和 scoped lease 竞争节点新增 9 项。因此总数增加是有界的，且每一项都有
明确的回归目的。

## 分层门禁

每个 commit 不需要等待全量回归。提交按影响范围选择最小充分门禁：

| 变更类型 | 提交前门禁 | 典型耗时 | 何时跑全量 |
|---|---|---:|---|
| 单文件实现/修复 | 受影响测试文件或单测节点 + Ruff | 1–10 秒 | 阶段封板 |
| 跨模块运行 seam | 相关组合测试 + Ruff + strict mypy/compile | 10–60 秒 | 阶段封板 |
| schema/transaction/process boundary | 专项矩阵 + 双连接/进程测试 + diff-check | 10–90 秒 | 阶段封板 |
| 文档、证据、索引 | `git diff --check` | <1 秒 | 不需要 |
| 用户可验收阶段 | 全量 pytest + Ruff + strict mypy + compileall | 约 2 分钟 | 必跑 |

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
PYTHONPATH=src .venv/bin/pytest --collect-only
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

全量通过只证明当前记录环境的源码和断言成立，不代表生产 GA；外部 IM、飞书、企微、模型
出网和 connector 仍由独立 Gate 控制。

