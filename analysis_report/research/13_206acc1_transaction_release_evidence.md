# `206acc1` 认证化事务恢复：本地发布证据快照

验证日期：2026-08-20（Asia/Shanghai）

源码 commit：`206acc1a93c16fe07fde4428d4d7e3b63c69ecc7`

源码 tree：`783d44b57d96f3837dacbe2690da8bbdd003b32d`

canonical parent：`3d474e5c566e545119dbb94e1f4e46932396a0c8`

## 1. 结论

该候选是 `codex/recovery-integrate-current` 的直接后继链，可由 canonical
fast-forward 集成。它与开发分支 `9610269` 的最终 tree 完全相同，但提交祖先从最新
canonical `3d474e5` 开始，不包含被拒绝的 `554115c..e05357b` 临时链。

仓库固定 local-evidence generator 在 clean detached worktree 上运行 5 个 baseline gate，
生成前后 commit/tree 不变、工作树 clean，随后 strict verifier 以完整 expected commit 回读通过。
这是一份 source-bound 本地阶段证据，不是 runtime Gate A–E promotion、正式安全评审或 GA 批准。

## 2. Canonical evidence

| 字段 | 结果 |
|---|---|
| Evidence format | `quantum-entanglement.release-evidence` |
| 固定 gate | 5/5 passed |
| `summary.releasable` | `true` |
| Source clean before/after | `true` / `true` |
| Identity stable | `true` |
| Expected commit verifier | PASS |
| JSON bytes | 1,738 |
| JSON SHA-256 | `c80388e07eb95b018091ee00b014890dfa50d95f50ffe08e0f260079a76f6caf` |
| 外置文件 | checkout 同级 `../qe_release_evidence/206acc1.release-evidence.json`（不提交） |

JSON 刻意保存在 checkout 外。把 evidence 写入源码树会改变 `dirty` 或 source identity，
破坏它要证明的谓词。该本机路径不是不可变 artifact retention；长期消费必须同时核验完整
digest、expected commit 和可信存储来源。

复核命令：

```bash
python3 scripts/generate_release_evidence.py > /outside/checkout/evidence.json
python3 scripts/verify_release_evidence.py /outside/checkout/evidence.json --repository-root /clean/checkout --expected-commit 206acc1a93c16fe07fde4428d4d7e3b63c69ecc7
```

verifier 输出：

```text
release evidence verified
```

## 3. 扩展三版本门禁

固定 generator 不替代多版本兼容验证。本候选另行执行：

| 门禁 | 实测 |
|---|---|
| Python 3.9.6 | 883 passed、1 skipped |
| Python 3.12.12 | 884 passed |
| Python 3.13.9 | 884 passed |
| Python 3.13 strict warning | `ResourceWarning` 与 `PytestUnraisableExceptionWarning` 均升级为 error，PASS |
| Ruff check | PASS |
| Ruff format | 91 files already formatted |
| strict mypy | 34 source files clean |
| compileall | `src tests scripts examples` PASS |
| dependency locks | 4 targets、74 package records、verified |
| compact demo | 3 tasks、25 events、3 artifacts、`completed=true` |
| `git diff --check` | PASS |

在最新 canonical 上重新 cherry-pick 的 disposable integration worktree 还单独复跑了
Python 3.13 strict 全仓 884 项、Ruff 与 strict mypy，最终得到与开发分支相同的
`783d44b57d96f3837dacbe2690da8bbdd003b32d` tree。

## 4. Ancestry 与集成谓词

已验证：

- merge-base(`206acc1`, canonical) = `3d474e5`；
- `206acc1` 是 canonical 的直接后继提交链；
- `554115c`、`f18ad9f`、`e05357b` 均不是 `206acc1` 的祖先；
- 开发分支与 canonical-parent 候选的 tree 均为 `783d44b...`；
- candidate worktree 在门禁前后保持 clean。

独立审查通过前，不移动 `codex/recovery-integrate-current`；审查通过后也只能使用
old-OID guarded `update-ref` 做 fast-forward，禁止隐式覆盖其他并行集成。

## 5. 证据边界

本证据没有证明：

- Gate A operation authorization、backup manifest v2 等并行分支已经安全；
- 真实飞书/企微 connector 可发送；本项目仍禁止任何真实消息写入；
- external effect 已形成 trusted receipt 与端到端原子恢复；
- 多机数据库、HA、Kubernetes、容量、soak、RPO/RTO 或 kill-point chaos；
- wheel/sdist 已在本 commit 重新完成双构建、manifest、SBOM、risk-policy、签名或 provenance；
- 本地用户名路径、临时 JSON 或手工摘要等于 immutable CI attestation。

因此准确表述是：`206acc1` 达到当前仓库定义的 local baseline，并有额外三版本安全门禁；
完整产品仍须逐项关闭 `docs/production/RELEASE_GATES.md` 中的运行时与发布阻断。
