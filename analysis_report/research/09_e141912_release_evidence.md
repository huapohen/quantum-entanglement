# `e141912` 本地发布与制品证据快照

验证日期：2026-08-20（Asia/Shanghai）

源码提交：`e1419125153260506657b54020f6e28d6e59fdab`

源码 tree：`752ffefc5e67d2931c72671f26d36856ec777981`

> **历史快照：** 本文全部 gate、digest、制品与路径只绑定 `e141912`。后继源码不能复用
> 这些结果；任何当前发布判断都必须重新生成并验证 source-bound evidence、制品、manifest
> 和 SBOM。

## 1. 结论

该提交在本机 clean checkout 上通过仓库现有的单节点测试、静态检查、格式、类型、编译、
demo、canonical local evidence、双构建、distribution manifest、SBOM、官方 CycloneDX schema、
wheel 安装和 package-data 检查。它是一个可复核的阶段基线，不是 GA、SaaS tenant isolation、
真实 connector 授权、漏洞/许可证清关、签名 provenance 或独立 trusted-builder 证明。

## 2. 源码与本地门禁

| 门禁 | 实测结果 | 证据限制 |
|---|---|---|
| Dependency lock verifier | 4 targets、74 package records，verified | 证明 committed lock contract 自洽，不证明 index/包本身可信 |
| Python 3.9 unit/integration | `Ran 625 tests ... OK` | 本机一次运行；fake/read-only fixture，不含真实 IM 写入 |
| Ruff lint | 锁定 Ruff 0.16.3，`All checks passed` | 静态规则不是安全审计 |
| Ruff format | 76 files already formatted | `4f10649` 修复了此前 5 文件 formatter gap |
| strict mypy | 30 source files clean，Python 3.9 contract | `follow-imports=skip`，不证明第三方包类型正确 |
| compileall | `src tests scripts` 通过 | 只证明 Python bytecode 编译路径 |
| compact collaboration demo | 3 tasks completed，25 events，3 artifacts | 内嵌 fake agents；无进程崩溃、网络或真实副作用 |
| `git diff --check` / source status | 通过；验证前后 clean | 本地观察，不是不可变 CI checkout attestation |

使用的核心命令：

```bash
python3 scripts/verify_dependency_locks.py --repository-root .
PYTHONPATH=src python3 -m unittest discover -s tests -q
ruff check src tests scripts
ruff format --check src tests scripts
MYPYPATH=src mypy --strict --python-version 3.9 \
  --no-site-packages --follow-imports=skip src/quantum_entanglement
PYTHONPATH=src python3 -m compileall -q src tests scripts
PYTHONPATH=src python3 examples/group_chat_demo.py
git diff --check
```

## 3. Canonical local release evidence

`scripts/generate_release_evidence.py` 在 checkout 外生成 canonical JSON，随后由
`scripts/verify_release_evidence.py` 以完整 expected commit 严格回读验证：

| 字段 | 值 |
|---|---|
| 结果 | `release evidence verified` |
| JSON bytes | 1,727 |
| SHA-256 | `dfbc8b9c42148e93d1e687a34f6bca35de473118fe6c23829e9362c53cd3aac8` |
| 本地临时路径 | `/private/tmp/qe-release-evidence.XXXXXX.json` |

该文件故意不放进 checkout：把 evidence 写入源码树会改变或污染其要证明的 source identity。
临时路径不是长期留存或不可变 artifact；digest 才能用于后续核对。该 JSON 也不是签名、
provenance、SBOM、漏洞扫描、clean-host 证明或发布批准。

## 4. 当前提交双构建

构建环境：CPython 3.12.12、`build==1.4.4`、`setuptools==82.0.1`，使用 committed
build/release lock，`--no-isolation`，`SOURCE_DATE_EPOCH=1787187233`。

两个 detached worktree 都固定在 `e141912`，输出写入各自 checkout 之外的目录。两份 sdist
分别 canonicalize 后，严格 comparator 返回 `byteIdentical: true`：

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `quantum_entanglement-0.1.0-py3-none-any.whl` | 166,590 | `df8207254736298badb0a0d833c24ab1dd399158e1824993d69f2923baa83c68` |
| `quantum_entanglement-0.1.0.tar.gz` | 281,601 | `8b1e2f00377ac7a8ad83ed95d90490356dfcec9c619e265433cef926af1616d7` |

本地证据位置：

- reference dist：`/private/tmp/qe-current-dist-a.AEqrj8`
- candidate dist：`/private/tmp/qe-current-dist-b.vuwTWz`
- source worktrees：`/private/tmp/qe-current-wt-a.1J9KlA`、
  `/private/tmp/qe-current-wt-b.jUGrdd`

两 worktree 在构建后保持 clean，但共享同一主机、Git object database、解释器、依赖环境和信任
根。因此这只证明同一锁定工具链/主机条件下的重复构建，不是 independent builder 或跨 runner
reproducibility。

## 5. Distribution manifest 与 SBOM

严格 source-bound distribution manifest 已按完整 commit 回读：

| Document | Bytes | SHA-256 |
|---|---:|---|
| Distribution manifest | 1,122 | `3a92e6e8f5cb413da8551a4984d021bec10cdb1ca8b6e2ca7cd8124785d05361` |
| Runtime CycloneDX 1.6 | 2,238 | `4064cdc01c23093535db8761de5289898f2351fb55673dd5182fbc950493a9c8` |
| Build CycloneDX 1.6 | 79,753 | `d29c0129720db4733a658e54a9605c11f19a0094a481b60d1ae73626ec666a8b` |

Runtime SBOM 有 0 个 base runtime dependency；这符合当前 base install，但明确不覆盖 optional
extras、解释器、OS、容器和部署依赖。Build SBOM 有 51 个唯一 component，绑定四份 lock 的
target/version/hash evidence。两份文档均通过：

1. repository strict generator/verifier 的 exact-byte/profile/graph/source 校验；
2. `cyclonedx-python-lib==11.12.0` 的 CycloneDX 1.6 `JsonStrictValidator`。

临时位置：manifest 为 `/private/tmp/qe-current-manifest.8YOGYM`，SBOM 目录为
`/private/tmp/qe-current-sbom.PHpaTv`。这些临时文件未签名，也不是长期 artifact retention。

## 6. 安装与 package data

- 在新 Python 3.12 venv 中用 `--no-index --no-deps` 安装 exact wheel；`pip check` 返回
  `No broken requirements found`；package import 成功。
- wheel 内存在 `attempts.py`、migration 0001 的 up/down SQL，package-data 检查通过。
- 首次执行曾错误选择一个没有 `build` CLI 的临时 venv，在任何 package 生成前失败；第二次
  使用锁定 build closure 从头执行并得到上述完整成功链。失败尝试不计入通过证据，也没有用
  部分输出替代正式制品。

## 7. 仍然阻断生产商用的边界

即使本快照全部通过，至少以下问题仍阻断真实客户和不可逆副作用：

- Orchestrator 尚未把 durable attempt lease/fencing、Agent 调用、artifact/result、task
  terminal 和 action receipt 接成一个可恢复 effect state machine；`RUNNING` 崩溃接管仍未
  端到端落地。
- typed tenant capability 尚未覆盖 admission、所有 repository 和每次 tool/connector effect；
  没有 OIDC/service principal、KMS-backed root、membership freshness 或真正不可信进程沙箱。
- 没有版本化服务 API、流式 cursor、readiness/liveness composition root、OpenTelemetry/SLO、
  容量/chaos/soak 证据。
- 备份仍缺加密异地保管、调度/保留、deployment-equivalent retained restore drill 和实测
  RPO/RTO。
- 供应链仍缺 immutable runner/interpreter/bootstrap、离线可信镜像、漏洞/恶意包/许可证策略、
  签名 provenance、artifact signature 和独立 builder 复现。
- 所有 connector 仍限定 fake/read-only fixture；本项目没有向飞书或企微发送任何消息。

所以本快照可用于“阶段基线通过”的工程结论，不能用于“生产商用级已经完成”的结论。
