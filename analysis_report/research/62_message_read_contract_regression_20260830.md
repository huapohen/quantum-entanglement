# 消息读取 seam 节点回归证据（2026-08-30）

## 节点

- 分支：`mainline_continue_quantum_entanglement`
- 提交：`c4d3ec7 feat(im): add authenticated conversation message read contract`
- 变更范围：`apps/im-api/internal/app/` 与 `apps/im-api/internal/imstore/` 的 Go 合约和路由

## 为什么不是 2,975 项

`pytest --collect-only` 的数字只是当前 Python 测试库存盘点。它不等于每个 commit 的执行清单。
本节点没有修改 Python runtime，因此默认回归门禁只选择：

```text
git diff --check
git diff --cached --check
go test ./...
go vet ./...
```

自动选择器实测 `changed_paths=3`，没有选择 Python pytest。只有阶段封板、跨语言合并或用户验收
才显式运行 `scripts/regression_gate.py --full`。

## 已执行结果

| 门禁 | 结果 |
|---|---|
| `gofmt` | 通过 |
| `go test ./internal/app ./internal/imstore` | 通过 |
| `go test ./...` | 通过 |
| `go vet ./internal/app ./internal/imstore` | 通过 |
| `go vet ./...` | 通过 |
| `git diff --check` | 通过 |

## 十小时交付节奏

每个独立小 commit 先按路径执行最小充分门禁并 push；同一阶段的多个 commit 合并为一个封板点，
封板时才支付一次全量 Python 成本。若运行时代码无法映射到专项测试，选择器 fail-closed 自动升级
全量，避免提速造成漏测。文档、报告和索引变更只做差异检查。

可复制命令：

```bash
PYTHONPATH=src .venv/bin/python scripts/regression_gate.py --dry-run
PYTHONPATH=src .venv/bin/python scripts/regression_gate.py
PYTHONPATH=src .venv/bin/python scripts/regression_gate.py --full  # 阶段封板/验收才用
```

快速门禁减少等待时间，不改变生产安全边界；真实 IM、飞书、企微、模型出网及 outbound 仍不启动。
