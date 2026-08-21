# `ca02903` 上的认证事务安全重放与发布证据

验证日期：2026-08-20（Asia/Shanghai）

canonical parent：`ca02903b38f51a60712eb8165e2f5b0fc18e9005`

固定 evidence source：`9c242741dce1470325bc3744a77d44192217913b`

固定 source tree：`8920d2692055d7f9abf1d5a1004448b1c5da6ac0`

## 1. 结论

`codex/recovery-integrate-current` 在 invocation 候选审查期间由 process-identity foundation
从 `3d474e5` 安全前进到 `ca02903`。因此旧候选 `8d82dd7` 不再满足 fast-forward 谓词，
不能覆盖或回退 canonical。本阶段在 `ca02903` 上逐笔重放原 24 笔认证事务链，每一笔后均
运行 attempts + invocation recovery 专项；24/24 cherry-pick 与 24/24 专项门禁通过。

重放后的 14 个非 `CHANGELOG.md` 代码、测试、运维文档和研究索引 blob 与经审计的
`8d82dd7` 完全一致。process-identity foundation 只增加独立模块、测试和生产文档；组合线
同时保留全局 process epoch/at-fork guard 与 invocation store 自身 creator-PID fail-closed。

组合候选另关闭了一条测试证据噪音：Python 3.12/3.13 会在故意从多线程父进程调用 raw
`os.fork()` 的对抗测试中发出 `DeprecationWarning`。新测试只在该调用局部捕获，并在
3.12+ 精确断言一条 `multi-threaded ... fork() ... deadlocks` 警告；3.9 精确断言零条。
没有模块级或全局 ignore。三版本全仓 `-W error` 因而干净。

这证明的是 process identity foundation 与单节点 SQLite invocation storage primitive 的
可组合阶段，不是整体产品 GA、真实 IM 发送授权、distributed exactly-once 或生产 promotion。

## 2. 为什么不能继续使用旧集成候选

旧候选的 merge-base 是 `3d474e5`，而 canonical 已合法前进到 `ca02903`。任何把 canonical
直接移动到 `8d82dd7` 的操作都会丢弃 8 笔 process-identity 提交。安全处理是：

1. 保留 `8d82dd7` 作为已审计内容来源；
2. 从当前 canonical `ca02903` 新建 disposable integration worktree；
3. 按原顺序逐笔 cherry-pick 24 笔提交；
4. 每笔后运行两个专项文件；
5. 比较关键 blob，而不是假设无冲突就等价；
6. 在组合 HEAD 上重跑三版本、strict warnings、静态门禁与 source-bound evidence。

过程没有使用 merge、rebase、reset，也没有移动 `main` 或 canonical。

## 3. 逐提交重放台账

| 原提交 | 新提交 | 阶段 | 专项结果 |
|---|---|---|---|
| `a0004f5` | `d33636a` | receipt stream identity bound | 81 passed |
| `8da2723` | `0bf987a` | derived stream limit 文档 | 81 passed |
| `5cdd4bf` | `ab447d3` | invocation fencing epoch 反例 | 82 passed |
| `011fb7c` | `0fa34a8` | fencing epoch 合同 | 82 passed |
| `bfec5f2` | `5dfc4b2` | UTC 年边界规范化 | 83 passed |
| `8d657bb` | `3fa24a2` | UTC range 文档 | 83 passed |
| `e808e9b` | `45d68d7` | first-claim remnant 拒绝 | 84 passed |
| `cd9ce57` | `184d245` | zero-counter 合同 | 84 passed |
| `d5076e2` | `c2574c4` | attempt/job error 绑定 | 87 passed |
| `26d8f3c` | `8e6e90d` | error 一致性文档 | 87 passed |
| `ef9dee1` | `7023989` | recovery error 长度 | 88 passed |
| `3a1ee98` | `787714a` | error limit 文档 | 88 passed |
| `30f40c2` | `9b64334` | job status/attempt budget | 90 passed |
| `aaaae87` | `a23761b` | budget invariant 文档 | 90 passed |
| `ba1ee0a` | `190657f` | parallel dispatch drain | 90 passed |
| `c586281` | `1abf559` | structured drain 文档 | 90 passed |
| `cecbc39` | `247a59e` | 本地 tests package 隔离 | 90 passed |
| `9028e13` | `b0766ae` | 认证 transaction outcome 与反例 | 143 passed |
| `9b15a39` | `e7f1212` | backup fixture 显式关闭 | 143 passed |
| `592f5c9` | `87168cb` | 事务恢复运维合同 | 143 passed |
| `221fbc8` | `100f1ca` | 认证事务研究证据 | 143 passed |
| `206acc1` | `107faa1` | 研究索引 | 143 passed |
| `b3ec2f1` | `d9910cb` | canonical-parent evidence | 143 passed |
| `8d82dd7` | `16d1c51` | evidence 索引 | 143 passed |

组合后的预期-warning 精确断言为独立提交 `9c24274`，不改动 production source。

## 4. 内容等价与 ancestry 证明

以下 14 个路径在 `8d82dd7` 与重放后的 `16d1c51` 上 blob 完全相同：

- `analysis_report/README.md`；
- 两份 invocation 研究/发布证据；
- 三份 invocation 运维文档；
- `attempts.py`、`invocation_recovery.py`、`runtime.py`；
- `tests/__init__.py`；
- attempts、backup、invocation recovery、runtime 四份测试。

`CHANGELOG.md` 预期不同，因为组合版本同时保留 process-identity 与 invocation 两组条目。

已验证：

- merge-base(`9c24274`, canonical) = `ca02903`；
- canonical 相对组合 HEAD 为 `0 behind / 25 ahead`；
- `554115c`、`f18ad9f`、`e05357b` 均不是组合 HEAD 的祖先；
- 工作树在 gate 前后 clean；
- `git diff --check` 与 Git connectivity 通过。

## 5. 三版本与静态门禁

| 门禁 | 结果 |
|---|---|
| Python 3.9.6 + pytest 8.4.2 + `-W error` | 900 passed，1 skipped |
| Python 3.12.12 + pytest 8.4.2 + `-W error` | 901 passed |
| Python 3.13.9 + pytest 8.4.2 + `-W error` | 901 passed |
| Python 3.13 unittest discovery | Ran 901 tests；OK |
| Attempts + invocation recovery（最终） | 143 passed |
| Process identity | 17 passed；预期 fork warning 被精确断言 |
| Ruff check | All checks passed |
| Ruff format | 93 files already formatted |
| Strict mypy | 35 source files clean |
| compileall | `src tests scripts examples` PASS |
| dependency locks | 4 targets、74 package records、verified |
| compact demo | 3 tasks、25 events、3 artifacts、`completed=true` |
| source integrity | `git diff --check` PASS；worktree clean |

3.9/3.12 使用 `uv run --isolated --no-project` 固定 pytest 8.4.2 与 pytest-asyncio
1.2.0；3.13 使用已安装的相同 pytest 版本。项目生产依赖仍为零；该测试环境解析不替代
仓库的 dependency-lock verifier。

## 6. Canonical release evidence

固定 evidence source 为 `9c242741dce1470325bc3744a77d44192217913b`，tree 为
`8920d2692055d7f9abf1d5a1004448b1c5da6ac0`。

| 字段 | 结果 |
|---|---|
| Evidence format | `quantum-entanglement.release-evidence` v1 |
| 固定 gates | 5/5 passed |
| `summary.releasable` | `true` |
| Source clean before/after | `true` / `true` |
| Source identity stable | `true` |
| JSON bytes | 1,738 |
| JSON SHA-256 | `22e1a85fbbf17def4f9ec18042b3210b60c49b0b4c6baafebfa4a33c86dcec39` |
| expected-commit verifier | PASS |

JSON 位于 checkout 外部，不提交进 Git。它由 `python3.13` 生成，必须由
`python3.13 scripts/verify_release_evidence.py` 消费；解释器 basename 属于固定 gate argv。
本机 sidecar 不是 immutable CI retention，也不是签名 attestation。

## 7. 集成谓词

独立审查通过且 canonical 仍精确等于 `ca02903` 时，只允许：

```bash
git update-ref refs/heads/codex/recovery-integrate-current \
  9c242741dce1470325bc3744a77d44192217913b \
  ca02903b38f51a60712eb8165e2f5b0fc18e9005
```

old-OID 不匹配必须停止并重新重放，不能强推、reset 或覆盖并行结果。完成快进后还要在目标
HEAD 回读 commit/tree、重跑 strict gate，并把后续 Notion 文档链重新基于新 canonical
重放；不能直接使用仍以 `8d82dd7` 为父的旧 Notion 分支。

## 8. 剩余阻断

- Gate A operation authorization 尚待最终独立 P0 复审与组合重放；
- ordinary attempt read APIs 的 provider-error firewall 尚未实现；
- backup manifest v2 的 exact topology/DDL/count/bounded-number 防线尚未重建；
- tenant/authentication 尚未覆盖每个 public repository 与 external effect；
- durable attempt、runtime、trusted receiver acceptance、action receipt、artifact/task terminal
  仍未形成一个端到端可恢复 Unit of Work；
- 单机 SQLite/WAL 不是多机 HA 或 distributed fencing；
- 仍缺 kill-point chaos、容量/soak、RPO/RTO、完整 SBOM/签名 provenance、部署与运营验收；
- 飞书/企微保持只读零发送，任何真实消息写入都不在授权范围。

因此准确 promotion decision 是：该组合 primitive 达到当前仓库 local baseline，允许在独立
审查后进入下一条 canonical 阶段；整体产品仍为 **NO-GO for production/GA**。
