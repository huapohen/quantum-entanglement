# 当前实现证据与生产边界（2026-08-20）

> **历史快照：** 本文的 commit、tree、531 项测试和能力判断只绑定
> `e4cbf040579bf1f33c2b7692d2fbd6944d837952`，不得作为当前主线证据。后续提交已经
> 改变实现与门禁；当前状态必须以 `docs/production/CURRENT_READINESS.md` 及更新日期更晚、
> 明确绑定 source commit/tree 的证据报告为准。

> 本文是 2026-08-19 调研报告之后的实现增量基线。它取代综合报告中已经过时的
> 测试数量、模块清单和“待实现”判断，但不改写历史调研当时观察到的事实。这里的“取代”
> 只描述该快照形成时的关系，不表示它持续取代后继证据。

## 1. 结论先行

Quantum Entanglement 已从 54-test 的领域原型发展为一个有 531 项 clean-clone
测试、持久化 primitives、迁移/备份/发布证据和供应链内容门禁的单节点内核。
从原调研报告提交 `8bf1522` 到本次实现基线，共增加 142 个原子提交、变更 86 个
文件，净统计为 `+39,028/-459` 行。

这不等于“已经生产可商用”。当前准确边界是：

- 已有单节点 SQLite 下可独立验证的事件、投递、attempt、artifact、projection、
  tenant authorization、备份恢复、迁移桥和制品完整性 primitives；
- 多项 primitive 尚未在 Orchestrator 的一次端到端事务中组合，例如 durable
  attempt 与 runtime、durable artifact 与 task completion、authorization 与每次
  tool/connector effect；
- 没有对公网服务 API、正式 A2A/MCP 互操作、真实 IM connector、完整 action receipt、
  生产部署/观测、锁定的构建供应链、SBOM 或签名 provenance；
- 因此仍只能用于可信、单节点、无真实不可逆副作用的预生产验证，不能承载未受控
  客户流量，也不能把测试通过描述成 GA。

## 2. 可复核基线

| 项目 | 固定值 |
|---|---|
| 实现 commit | `e4cbf040579bf1f33c2b7692d2fbd6944d837952` |
| Git tree | `0a879ae5351bdc3747a00cf4277ee6460df62d15` |
| 原调研报告 commit | `8bf1522` |
| 实现增量 | 142 commits；86 files；`+39,028/-459` |
| Python | macOS system CPython 3.9 clean clone |
| 测试 | 531/531 passed |
| 本地 release gates | 5/5 passed；`summary.releasable=true` |
| 工作树 | gate 前后均 clean；commit/tree identity stable |

验证在独立 clean clone 中执行。canonical evidence 写在 checkout 外部，再由严格
verifier 绑定到上表完整 commit；没有把共享工作树中的并行未提交文件计入结果。

### 2.1 实际执行的固定 gates

| Gate | 固定 argv | 结果 |
|---|---|---|
| Unit tests | `python3 -m unittest discover -s tests -q` | passed |
| Deterministic demo | `python3 examples/group_chat_demo.py --compact` | passed |
| Compile | `python3 -m compileall -q src tests scripts` | passed |
| Static lint | `ruff check src tests scripts` | passed |
| Diff integrity | `git diff --check` | passed |

另外以非 quiet 模式运行同一 test discovery，得到 `Ran 531 tests ... OK`。测试数量
只绑定本次 commit；以后不能复制该数字而不重新执行 clean-clone gate。

### 2.2 制品实测

同一 commit 在两个独立 clean checkout/worktree、相同构建 toolchain 与固定
`SOURCE_DATE_EPOCH` 下各构建一次：

- 原始 wheel 已字节一致；
- 原始 setuptools sdist 因 checkout mtime 等 archive metadata 不一致；
- 经 `scripts/normalize_sdist.py` canonicalize 后，两份 sdist 字节一致；
- `scripts/verify_reproducible_distributions.py` 对精确 wheel+sdist 集合逐字节比较通过；
- canonicalized 制品仍通过 `scripts/distribution_manifest.py` 的 source-bound 严格核验；
- package CI 已把独立 detached worktree 双构建和比较放在正式上传之前。

这只证明同一 CI toolchain/job 的可复现性。构建依赖尚未完整锁定，跨 runner、Python、
setuptools/zlib 版本复现仍需 retained matrix evidence；SBOM、签名和 provenance 也仍未
完成。

## 3. 当前能力矩阵

| 领域 | 已提交、可直接验证 | 尚未形成的生产保证 |
|---|---|---|
| 协作协议与 DAG | strict Envelope、Actor/Authority/Handoff、TaskGraph、Context Manifest、因果/幂等字段 | 内部协议版本注册中心；上一受支持版本兼容；公网 transport |
| 事件与 inbox/outbox | append-only SQLite event、transactional inbox/outbox、乐观并发、bounded reads | 多节点数据库；跨服务 transaction；完整外部 effect receipt |
| Publisher | lease/fencing、bounded admission、retry/DLQ、unknown outcome quarantine、ambiguity reconciliation | 进程级 hostile connector containment；接收方业务 acceptance；操作员鉴权 |
| Invocation attempt | durable claim/heartbeat/recovery/terminal CAS、fencing、迁移与两连接竞争测试 | Orchestrator 尚未把每次 Agent run 全部接到该 store；runtime heartbeat/cancel/effect receipt |
| Artifact | content-addressed blob、version CAS、scope、事务提交、链完整性和 bounded materialization | Orchestrator task/result/event 与 durable artifact 尚不是一次原子提交；大对象外部存储 |
| Projection | durable cursor、lease/fencing、strict schema/upcaster、batch validation、ambiguous transaction recovery | 多节点生产数据库与完整 operator rebuild service；所有业务投影尚未接入 |
| Tenant authorization | typed tenant/workspace/member/role、signed capability chain、audience/time/revocation、SQLite revision guard | 这是一块 security slice，不是 public admission；无 OIDC/KMS；未覆盖所有 repository/effect |
| Approval | Needs You queue、scoped grant、event recovery、因果/revision 校验 | 持久化 approval service、approver 当前权限复验、UI、action digest 到真实 effect 的闭环 |
| Backup/restore | consistent SQLite backup、strict canonical manifest、stable descriptor checks、exact-byte restore、admin CLI | 调度/加密/远端保管、真实容量 RPO/RTO、restore 后全系统 reconciliation 演练 |
| Migration | checksum ledger、exact schema、domain sidecar/registry/state/planner、bridge plan atomic application | native sparse v4、所有旧/新 binary 矩阵、完整 release executor 与 retained rehearsal |
| Runtime adapters | dependency-free runtime port、DSH 生命周期隔离 seam、A2A JSON-RPC mapping、LangGraph bridge | 官方 A2A SDK/TCK、MCP、真实隔离 launcher、remote stream/cancel/reconciliation |
| Release evidence | clean-source canonical gate evidence、strict verifier、CI retention | 它不是签名 attestation，也不替代 security/performance/DR 人工 release record |
| Distribution | exact wheel/sdist inventory、source bytes、wheel RECORD、metadata/entry point、safe archive bounds、双构建比较 | locked build deps、SBOM、vulnerability/license policy、signed provenance、artifact signature |
| 产品体验 | 三 Agent deterministic demo、`@Agent` 绕过 planner 但不绕过日志/上下文 | 群聊/任务图/Artifact/Needs You UI；首发 ICP/JTBD 验证；真实业务验收/付费数据 |

## 4. 对旧报告关键判断的修订

### 4.1 不再成立

下列原报告表述已经被提交实现取代：

- “ArtifactLedger 仍需迁入持久存储”——现在已有 `SQLiteArtifactStore`，具备 blob 与
  metadata 原子提交、版本 CAS、scope 和完整性校验；但 Orchestrator 组合事务仍未完成。
- “需要引入 outbox”——transactional outbox 和 bounded Publisher 已实现，且对 lease、
  fencing、retry、DLQ 与 ambiguity 有专门测试和 runbook。
- “task attempt/lease、projector checkpoint/schema upcast 整体未实现”——对应 storage/
  projection primitive 已实现；需要修订为“Orchestrator/服务级端到端集成仍未完成”。
- “没有多租户授权边界”——已有严格 tenant authorization slice；但 admission、所有
  repository 和 action-time effect 的系统级覆盖仍是 P0，不能写成“多租户生产安全”。
- “无备份恢复”——已有本地 POSIX 单节点的验证/恢复 primitive 和 CLI；远端保管、
  加密、调度、真实 RPO/RTO 与灾备演练仍未完成。
- “构建只有普通 wheel smoke test”——已有 source-bound manifest、canonical evidence、
  sdist normalization 和独立双构建比较；锁定依赖、SBOM、签名仍为空缺。

### 4.2 仍然成立

- 这仍不是可部署的多人协作 SaaS；没有 public API、认证接入、UI 或生产 topology。
- 正式 A2A SDK/TCK、MCP client/tool/resource adapter 仍未实现。
- 平台尚未形成“授权决定 → durable attempt → tool/connector action → receiver acceptance
  → action receipt → artifact/task terminal”一次可恢复闭环。
- SQLite 当前只是单节点边界，不能把文件放在 NFS/SMB 上当分布式协调器。
- 没有真实 Feishu/WeCom write connector；本项目仍明确禁止任何真实发送。
- 用户价值侧仍缺首发 ICP、明确 JTBD、验收指标、用户访谈与付费/采购证据。

## 5. 生产阻断项（按依赖顺序）

### P0-1：端到端 effect transaction

需要把当前分离的 primitives 串成一个可恢复状态机：

```text
authenticated admission
  -> action-time authorization
  -> inbox/idempotency
  -> durable invocation attempt + fencing
  -> sandboxed runtime/tool action
  -> receiver acceptance or explicit UNKNOWN
  -> durable action receipt
  -> artifact + task terminal CAS
  -> outbox projection
```

崩溃注入必须覆盖每个箭头两侧。远端不支持幂等或查询时，不得盲重试未知副作用。

### P0-2：系统级 tenant/authentication

现有 authorization slice 要接到每个 public operation、repository 和 external effect；
同时补 OIDC/service principal、KMS-backed key registry、membership freshness、操作员
break-glass 和交叉租户 generative tests。缺任一 authoritative dependency 都必须
readiness fail closed。

### P0-3：不可信 runtime/tool/URL 边界

需要进程/容器级文件、网络和进程权限；tool arguments strict schema；URL DNS/IP 二次
解析、redirect limit、metadata/private range deny；secret handle 而非明文；事件、错误、
trace、artifact 的 canary scan。模型输出、群聊、网页和 tool result 都只能是数据，不能
授予权限。

### P1-1：服务与互操作

实现 versioned API、readiness/liveness、cursor stream、OpenAPI compatibility；再做正式
A2A 1.x SDK/TCK 与 MCP adapter。协议 mapping 单测不是网络互操作证据。

### P1-2：部署、可观测与灾备

需要 reference container/deployment、locked configuration、OpenTelemetry、固定低基数
指标、告警、容量/耐久/故障测试、加密备份与 retained restore drill。SLO/RPO/RTO 只能
来自测量，不能来自设计文档。

### P1-3：完整供应链

锁定 build/dev/runtime 依赖及 hashes，验证许可证/漏洞策略，生成并验证 SBOM，输出签名
provenance/attestation 与 artifact signature，并证明 clean-host/cross-runner reproducible
build。当前 content manifest 和同 toolchain 双构建只是其中两层。

## 6. 推荐下一阶段

1. 先完成 approval/runtime 当前原子性与恢复收口，并把 durable attempt 接入一次真实
   Agent invocation；这个阶段必须有 crash matrix 和运行手册。
2. 并行完成 build lock + SBOM + provenance，使以后每个阶段都能保留可信发布证据。
3. 随后实现最小 authenticated service API 和 fake connector action receipt；继续保持
   Feishu/WeCom 无真实发送。
4. 产品轨同步选定一个首发 ICP/JTBD，用群聊、任务、Artifact、Needs You 四个同源视图
   跑通一个 3–5 Agent 的可验收业务闭环。
5. 每个阶段都以 clean clone、固定 commit/tree、exact gates、未解决 P0/P1 和明确
   promotion decision 收尾，不能用“测试很多”代替发布判断。

## 7. 证据入口与限制

实现与运维文档：

- [`READINESS_AUDIT.md`](../../docs/production/READINESS_AUDIT.md)
- [`ROADMAP.md`](../../docs/production/ROADMAP.md)
- [`RELEASE_GATES.md`](../../docs/production/RELEASE_GATES.md)
- [`THREAT_MODEL.md`](../../docs/production/THREAT_MODEL.md)
- [`DURABLE_INVOCATION_ATTEMPTS.md`](../../docs/production/DURABLE_INVOCATION_ATTEMPTS.md)
- [`DURABLE_ARTIFACT_STORE.md`](../../docs/production/DURABLE_ARTIFACT_STORE.md)
- [`OUTBOX_PUBLISHER.md`](../../docs/production/OUTBOX_PUBLISHER.md)
- [`TENANT_AUTHORIZATION.md`](../../docs/production/TENANT_AUTHORIZATION.md)
- [`SQLITE_BACKUP_RESTORE.md`](../../docs/production/SQLITE_BACKUP_RESTORE.md)
- [`DISTRIBUTION_INTEGRITY.md`](../../docs/production/DISTRIBUTION_INTEGRITY.md)
- [`LOCAL_RELEASE_EVIDENCE.md`](../../docs/production/LOCAL_RELEASE_EVIDENCE.md)

本文没有保留测试 stdout/stderr、主机路径、用户名、环境变量或 secret。canonical JSON
evidence 按设计存放在 checkout 外，不提交进 Git；本报告只记录非敏感 source identity、
固定 gate 和汇总结论。外部 GitHub Actions、Notion 回读、真实部署、性能、安全和 DR
证据未在本次 clean-clone 本地 gate 中执行，不能从本文推断其通过。
