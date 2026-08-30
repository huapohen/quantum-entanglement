# 回归门禁影响面选择器修复证据（2026-08-30）

> 提交：`28b09a1`
> 分支：`mainline_continue_quantum_entanglement`

## 问题

路径选择器此前只判断目录前缀：`apps/im-api/README.md` 或 `clients/im-web/README.md` 也会分别
触发 Go/Web 产品门禁。它们不影响运行代码，却会给“每个小改动都要跑大套件”造成错误反馈。

## 修复

- Go 门禁只由 `apps/im-api/**/*.go`、`go.work` 和 `go.work.sum` 触发；
- Web 门禁由 `clients/im-web/` 下非 Markdown 的源码、构建配置和资源触发；
- 模块 README/Markdown、分析报告和普通文档仍只执行 `diff-check` / `cached-diff-check`；
- Python focused Ruff 继续同时覆盖被改的源文件和选中的测试文件（前一节点 `1a0c54a`）。

## 可复现验证

```text
PYTHONPATH=src .venv/bin/pytest -q tests/test_regression_gate.py
7 passed

ruff check scripts/regression_gate.py tests/test_regression_gate.py
exit 0

apps/im-api/README.md
  -> diff-check, cached-diff-check
clients/im-web/README.md
  -> diff-check, cached-diff-check
apps/im-api/internal/app/app.go
  -> diff-check, cached-diff-check, go-test, go-vet
clients/im-web/src/App.tsx
  -> diff-check, cached-diff-check, web-build, web-first-synthetic
```

这次修复没有执行 Python 全量 2,969 项；Go 代码门禁和 Web 构建仍由真实代码变更触发。脚本遇到
无法映射的 Python 运行时代码仍 fail-closed 升级全量，避免“提速”变成漏测。
