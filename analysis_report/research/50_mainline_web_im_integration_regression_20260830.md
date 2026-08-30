# 主线 Web-first IM 集成回归证据（2026-08-30）

> 评审分支：`mainline_continue_quantum_entanglement`
> 集成提交：`4783f61`
> 远端：`origin/mainline_continue_quantum_entanglement`
> 运行边界：仅本地 synthetic/loopback；未连接飞书、企微、真实 IM provider 或生产凭据

## 结论

本次把 `dev_wanwork_quantum_entanglement` 的 Web-first IM 产品切片合入 QE 评审分支，同时保留
原分支的 `src/quantum_entanglement` Result Authority 内核。合并候选涉及 366 个文件、约 9.3 万
行变更，因此按“集成封板”执行一次全量门禁；后续单个小 commit 仍按影响面执行专项门禁，不要求
重复运行全部 pytest。

## 证据清单

| 门禁 | 命令/范围 | 结果 |
|---|---|---|
| Python 全量 | `PYTHONPATH=src .venv/bin/pytest -q` | 2,964 项通过，退出码 0，用时 152 秒 |
| Python 专项 | lease lifecycle、worker lifecycle、result acceptance；branch catalog、report sync | 全部通过；lease 专项 13 项约 2.2 秒 |
| Ruff | `.venv/bin/ruff check src tests` | 通过 |
| 类型 | `mypy --strict src` | 76 个源码文件无问题 |
| 字节码 | `python -m compileall -q src` | 通过 |
| Go API | `go test ./...` | 全部 package 通过 |
| Go 静态 | `go vet ./...` | 通过 |
| Web 构建 | `npm ci --no-audit --no-fund`；`npm run build` | 依赖按 lock 安装；TypeScript/Vite 构建通过 |
| Web-first 验收 | `scripts/verify_web_first.sh` | envelope、Agent Store、子群隔离、Workboard 闭环通过 |
| 路径/依赖/术语 | `check_workspace_paths.py`、`verify_dependency_locks.py`、术语专项 | 全部通过 |
| 差异卫生 | `git diff --cached --check` | 通过；清理了合并文档的尾随空白 |

全量 pytest 只有预期的 fork 相关 `DeprecationWarning`，没有失败或错误。集成前审计还确认：

- `src/quantum_entanglement` 相对合并前没有 staged 差异；
- 没有 `.env`、私钥、真实 API key、`node_modules` 或大于 5 MiB 的 staged 文件；
- 旧原型命名未出现在当前文本文件中；
- Go/Web 运行路径默认 synthetic、loopback、零真实 IM outbound。

## 回归纪律

这个证据不是“每个 commit 都跑 2,964 项”的依据，而是本次跨领域集成的封板记录。后续规则：

1. 文档/报告只跑差异卫生和生成器检查；
2. 单模块代码只跑直接测试文件和 Ruff；
3. 事务、schema、lease、process boundary 改动跑专项矩阵及必要的 mypy/compile；
4. 只有跨模块合并、阶段封板或用户验收前才跑一次全量。

Notion/Yuque 同步继续遵循本地优先策略；本证据先落在 Git/GitHub，不以远端知识库同步阻塞代码
交付。生产 Gate A–E 未因本次本地集成而打开。
