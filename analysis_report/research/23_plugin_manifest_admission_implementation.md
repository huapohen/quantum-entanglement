# 插件 Manifest 与 Admission 绑定：研究证据到 W1 实现

> 状态：P0-3 已在 `ed9a709` 实现；本文固定当时的研究证据、派生合同和门禁。
> P0-4 后续已在 `211ada7` 完成，现状与剩余 action-time Secret 边界见
> [`24_secret_claim_admission_implementation.md`](24_secret_claim_admission_implementation.md)。

## 1. 为什么这是产品合同，不是摘要字段优化

插件 ID、version 和 artifact digest 不能完整说明一次启动会做什么。Host API、ports、依赖 provider pin、
capability、egress、secret requirements、config schema 和 lifecycle timeout 中任何一项变化，都可能改变
权限、装配顺序、网络行为或故障恢复。若批准只绑定 ID/version/artifact，旧批准可以被复用于不同的
运行行为；若 effective digest 不包含这些行为，部署快照也不是可复核的部署真相。

本次坚持如下链路：

```text
研究证据
  -> 产品硬需求：manifest 是 claim，admission 才是 authority
  -> 领域对象/API：Manifest + PackageRecord + EffectiveRow + activation snapshot
  -> 安全控制：host 重算、exact binding、revision、drift fail-closed
  -> 实施阶段：W1 P0-3
  -> 验收证据：golden + every-field digest + diff + Host drift/race tests
```

## 2. `2output` 原始证据

所有行号均来自：
`/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output/more`。

| 来源 | 原始位置 | 研究原意 | 对本实现的硬约束 |
|---|---:|---|---|
| AgentSpace | `agentspace/research_report.md:1852-1861` | App passport 同时包含 publisher、release digest、SBOM、permission manifest、network destinations、secret requirements、runtime compatibility、test evidence、漏洞与撤销状态；runtime start 前验证 digest/policy。 | 权限 manifest、artifact 与准入 verdict 必须组成同一启动前快照；manifest/admission 漂移 fail-closed。 |
| Agent 安全治理 | `tech-agent-security-governance/research_report.md:239-276,700` | CBOM 覆盖 artifact hash/signature/provenance、capability、secret、OAuth、runtime 与治理信息；安装经 registry/allowlist/policy/pin/risk diff，扩权需重批。 | manifest 是不可信声明；host-owned admission 必须绑定 exact manifest digest，升级 diff 进入重新准入。 |
| Kiro/Crew | `kirocrew/research_report.md:910-936` | publisher、实际字节、依赖、权限与更新之间需要可验证身份链；要求 manifest/SBOM/provenance/权限签名、digest pin、撤回版本拒绝。 | admission 不能只绑定可复用的名称或版本文本；artifact 与 manifest digest 分别绑定字节和行为。 |
| MCP | `protocol-mcp/research_report.md:394-404,424-434` | 本地 Server 安装展示 exact command/args/source，改变需重新同意；组织 Registry pin digest；调用绑定 tenant/task/Server digest。 | 安装参数和运行声明变化使旧批准失效；start 与后续调用都需回绑 exact digest。 |
| DeepSeek Harness | `deepseek-harness/research_report.md:493-506,803-840,890-896` | bundle manifest 决定是否进入装配，但默认缺 publisher 签名、capability manifest、权限 diff、兼容验收和独立沙箱；生产要求内部 registry、固定 hash、签名、SBOM、扫描。 | 保留 Harness 的装配思想，但 manifest 不能自证信任；PackageRecord/admission 与插件 claim 分离。 |
| OpenBot | `openbot/research_report.md:629-650,761-772,1236-1248` | generic vault ref、server 地址变化后复用 token、同名 server 复用 orphan credential 都是现实失败模式；credential 需要 typed immutable association。 | P0-3 当时只冻结 manifest 中的 secret requirement names；该缺口后来由 P0-4 的 typed/bound claim admission 关闭，action-time lease 仍未完成。 |

对应 RQ：RQ-008、RQ-009、RQ-017、RQ-021、RQ-032、RQ-042。

## 3. 冻结的 W1 P0-3 合同

### 3.1 Manifest 是 claim

插件提供 `Manifest`，Registry 先执行 host-owned normalization/validation，再由 host 计算 domain-separated
`ManifestDigest`。配置 layer 不接受 `ManifestDigest` 或 `AdmissionRevision` 输入，因而 profile、bundle、
tenant overlay 不能自报可信值。

Manifest canonical v1 覆盖：

- plugin ID、semantic version、Host API；
- provides、requires 与 pinned provider；
- capabilities、egress、secret reference names；
- config schema digest；
- start/ready/drain/stop timeout。

集合字段排序并把 nil/empty 归一为 `[]`。Timeout 只接受 1ms～10min 的整毫秒值，canonical 使用显式
`timeoutsMs` 整数；非法 UTF-8、前后空白、控制字符或超长 egress 在摘要前拒绝，避免 JSON encoder
替换非法字节导致不同输入产生相同 preimage。

### 3.2 Admission 是 authority

`PackageRecord` 是 host-owned 准入快照，至少绑定：

```text
plugin_id + version + artifact_digest
+ approved_manifest_digest + admission_revision
+ provenance + SBOM + approval + revoked
```

注册要求 `ApprovedManifestDigest == hostComputedManifestDigest`、`AdmissionRevision > 0` 且未撤销。
artifact digest 绑定代码/包字节，manifest digest 绑定运行声明，两者不能互相替代。

### 3.3 P0-3 时的 EffectiveConfiguration v2 是部署真相

每个 `EffectiveRow` 增加 host 注入的 `ManifestDigest` 与 `AdmissionRevision`。两者进入 deep clone、row
canonical、row digest、overall effective canonical/digest、baseline validation、diff 和 activation check。
由于 preimage 发生不兼容变化，effective schema 和 digest domain 从 v1 明确升级为 v2，旧 v1 golden
保留为历史证据，不原地改写成“仍是 v1”。

Manifest 或 admission 变化会产生：

- `RowsChanged`；
- `ManifestChange` 或 `AdmissionChange`；
- 新 candidate effective digest；
- 新一次 host-owned admission，而不是静默沿用旧批准。

P0-4 因 Secret preimage 再次发生不兼容变化，当前 schema 已升级为 EffectiveConfiguration v3；
v2 只作为 `ed9a709` 的历史合同，不应被当前调用方继续生成。

### 3.4 Activation 不回读可变 Registry

`NewHost` 先校验 plan、binding、artifact、manifest digest、approved manifest digest、admission revision、
schema/capability/egress 与 revocation，再创建 selected activation snapshot。Snapshot 冻结 factory、config
和 timeouts；`Start` 不再从 Registry map 回读这些值，关闭 validate→start 的直接 TOCTOU 窗口。

Registry 本身的并发 freeze/snapshot 仍是 P1：当前只解决 Host 构造成功后的 activation 漂移，不宣称
并发 Register/Compose 已安全。

## 4. 代码与验收证据

| 合同 | 代码落点 | 自动验收 |
|---|---|---|
| host-owned manifest canonical/digest | `apps/im-api/internal/plugins/registry.go` | `TestManifestDigestIsCanonicalCompleteAndDomainSeparated` |
| input normalization、timeout/egress 拒绝 | `registry.go` | `TestManifestRejectsAmbiguousCanonicalInputsAndTimeouts` |
| 注册时 manifest/admission exact binding | `types.go`, `registry.go` | `TestRegisterRejectsUntrustedPackageClaims` |
| 注册后 caller slice 不可篡改 | `normalizeManifest` / Registry entry | `TestRegistryFreezesManifestSlicesAndDigestAtRegistration` |
| P0-3 Effective v2 trust fields/canonical/diff | `composition.go@ed9a709` | `TestEffectiveConfigurationHasGoldenCanonicalBytesAndDomainSeparatedDigest`, `TestEffectiveConfigurationBindsManifestAndAdmissionSnapshots` |
| Host 漂移拒绝 | `lifecycle.go` | `TestNewHostRejectsManifestAndAdmissionDrift` |
| frozen activation | `lifecycle.go` | `TestHostStartUsesFrozenActivationSnapshot` |
| P0-3 Manifest golden | `testdata/plugin_manifest_v1.golden.json@ed9a709` | exact bytes + 当时固定 digest |
| P0-3 Effective golden | `testdata/effective_configuration_v2.golden.json@ed9a709` | exact bytes + 当时固定 digest |

当前 P0-4 golden 与完整 digest 见专题 24，不能把工作树中的 v3 文件反向当作 P0-3 历史证据。

已通过：

```text
go test ./apps/im-api/...
go test -race ./apps/im-api/internal/plugins
go vet ./apps/im-api/...
git diff --check
```

## 5. 已关闭与尚未关闭

### 本提交关闭

- P0-3：effective digest 未绑定真实 manifest/lifecycle 行为；
- timeout-only、unused declaration 或 secret requirement 变化可以复用旧 effective digest；
- admission revision 未绑定被批准 manifest；
- `NewHost` 校验后 `Start` 再回读 Registry 的直接 TOCTOU。

### P0-3 当时仍为阻断、当前已关闭的项

P0-4 已关闭普通 value/default Secret canary、caller raw locator 进入 canonical、跨 scope replay、
broker/manifest/schema/admission 漂移和撤销检查缺口。实现与负向证据见专题 24。

### 当前仍为阻断项

1. **Action-time JIT Secret lease**：P0-4 binding view 不是 bearer，没有 token exchange、短 TTL/PoP、
   trusted executor 或 provider receipt。
2. **Registry 并发合同**：需要 freeze 或一次性 immutable snapshot，消除 Register/Compose/Resolve map race。
3. **不响应取消的插件**：in-process context timeout 不能强杀忽略 context 的插件；第三方代码仍必须进入
   process/UID/container/microVM 隔离。
4. **effect scope 迟到注册**：需要 `open -> closing -> closed` 状态。
5. **source/diff 与跨语言 canonical**：layer kind/raw source、source-only drift、NFC 与正式跨语言 codec 仍待冻结。

因此，P0-3 完成从未等于 W1 Plugin Host 全部完成；当前 P0-4 已完成，下一项是上述 P1 加固，
随后才进入 volatile MemoryFake EventStore。
