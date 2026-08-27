# Secret Claim Admission：研究证据到 W1 P0-4 实现

> 状态：P0-4 已在 `211ada7` 实现并通过 `im-api`、race、vet 门禁。
>
> 证据根：`/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output/more`
>
> 结论边界：本阶段完成的是 **Secret locator 的准入与不可携带绑定**，不是 action-time Secret
> lease、Vault/KMS、真实 connector credential 注入或生产凭据闭环。

## 1. 先给结论

P0-4 关闭了此前配置层直接携带 caller-provided Secret reference 原文的边界。新链路固定为：

```text
可信 Host API 接收 raw locator
  -> AdmitSecretClaim（host + broker 准入）
  -> SecretClaimReference（claim digest + revision）
  -> Compose 重新校验全部绑定
  -> SecretBindingView（身份/审计视图，不是 bearer）
  -> EffectiveConfiguration v3 / Factory

未来单独实现：
  typed ActionIntent + action-time policy
  -> JIT SecretLease / token exchange
  -> trusted executor
  -> provider receipt + revoke/reconcile
```

Raw locator 只在 `AdmitSecretClaim` 调用栈中提供给已注册 broker。它不会进入配置 layer、
EffectiveConfiguration、canonical bytes、digest、diff、getter、Factory、Host activation 或错误文本。
Factory 得到的 `SecretBindingView` 不提供任何 material getter，也不能凭该值换取 Secret。

这不是把 `ReferenceID` 改一个更像安全对象的名字。领域边界被拆成三类互不等价的对象：

1. `SecretClaimRequest`：一次准入请求，唯一允许短暂携带 raw locator；
2. `SecretClaimReference`：供配置输入引用的不可解释 claim identity；
3. `SecretBindingView`：供 effective snapshot、diff 和 Factory 使用的非 bearer 绑定证据。

## 2. 研究证据如何变成产品硬合同

所有位置均相对本报告顶部的 `2output/more` 证据根。

| 研究证据 | 原始结论边界 | 产品硬需求 | 本阶段控制 |
|---|---|---|---|
| `tech-agent-security-governance/research_report.md:315-333` | [A] Agent 产生 canonical intent，broker 按 tool/resource/action 签发短期 handle；模型不看 refresh token/主密钥。 | Secret material 不能成为 prompt/config 字符串；准入 identity 与未来使用 authority 分开。 | raw locator 不进入 composition；`SecretBindingView` 明确不是 bearer；action-time lease 延后且不得由本对象冒充。 |
| `tech-agent-security-governance/research_report.md:187-231` | [A] mandate 至少绑定 principal、task/purpose、audience、TTL、delegation 与 revocation；下游 scope 只能收窄。 | 一个通用 vault ID 不能跨 tenant、任务或调用对象重放。 | claim 绑定 tenant、row、plugin/version、artifact、manifest、admission revision、schema、logical name、broker、purpose、audience。 |
| `tech-agent-security-governance/research_report.md:235-276` | [A] CBOM 不只列依赖，还要列 Secret/Auth/Capability/Governance；升级、撤销和扩权进入重新准入。 | broker、schema、manifest、artifact 与组织批准必须是同一信任快照。 | broker definition digest、policy revision、manifest digest、admission revision、schema digest 全部进入 claim/effective 绑定；漂移拒绝。 |
| `deepseek-harness/research_report.md:433-459` | [F] DSH 本地凭据文件的 0600 只隔离其他 OS 用户，不隔离同 UID Agent/tool；[A] 需要 Keychain/Secrets Broker、audience、短期 token 与审计。 | 不能沿用“路径不告诉模型”或“opaque 字符串”作为安全边界。 | raw locator 与 Factory/模型边界分开；未来 executor/Secret lease 明确列为未完成阻断。 |
| `deepseek-harness/research_report.md:489-541` | [U/A] 插件生态缺 capability manifest、组织 allowlist、独立沙箱和撤销；同 UID + network 可外传 Secret。 | Secret contract 必须和已准入插件版本/行为绑定，第三方 broker/plugin 不能进入当前主进程。 | 只支持随 host 编译、平台准入的 trusted broker/factory；artifact/manifest/admission exact bind。 |
| `openbot/research_report.md:632-650,761-772` | [F] token 绑定错误 MCP server、地址变化复用 token、同名新 server 复用 orphan credential 都出现过；[A] 要 immutable typed association。 | locator 的“名字相同”不能代表同一授权；server/broker/purpose/audience 漂移必须使旧引用失效。 | logical name、broker definition、policy、purpose、audience 和完整 plugin trust snapshot 参与绑定。 |
| `sandbase-harness/research_report.md:470-504` | [F] 有 AES-GCM/Vault UI 不等于 runtime enforcement；[A] 正确方向是 Session/Agent/tool/target/purpose/policy 绑定的 Secretless Capability Broker。 | “加密存了 Secret”与“当前 action 被允许使用 Secret”严格分开。 | P0-4 只做 admission；没有 resolution API，防止被误用成 action capability。 |
| `holaos/research_report.md:889-924` | [F] 名为 `secret_ref` 的字段仍可能直接承载 token；[A] YAML/DB 只存 opaque ref，JIT resolve 后给 scoped capability。 | 安全不能由字段名推断，必须有数据流和负向 canary 证据。 | 原 locator 不持久化；canonical/golden/Factory/error 用 canary 证明不泄漏。 |
| `open-connector/research_report.md:375,687,727-738,906` | [F/A] stable connection scope、token exchange 与 JIT credential 是企业控制点；remote runtime 自带 credential 会绕过 gateway。 | 最终 Secret 使用必须经过统一 broker/executor，不能让插件自行解析环境变量或持 refresh token。 | 当前没有给插件 resolver；未来 action-time API 必须从 Action Plane/Egress Broker 进入。 |

对应既有研究合同：RQ-008、RQ-009、RQ-017、RQ-021、RQ-032、RQ-035、RQ-042。

## 3. 威胁模型与信任边界

### 3.1 保护资产

- provider API key、OAuth refresh token、数据库/对象存储/IM credential；
- Secret locator 的内部路径、Vault item ID、account/server association；
- tenant、插件、purpose、audience 和批准 revision 的绑定关系；
- EffectiveConfiguration、diff、Factory 参数和 Host activation；
- broker backend 错误、panic 与诊断文本。

### 3.2 攻击者能力

- 配置提供方提交 raw Secret、伪造 claim digest/revision 或复用合法引用；
- 从其他 tenant、row、plugin 或 logical field 重放引用；
- 在 manifest、artifact、schema、admission、broker policy、purpose 或 audience 变化后复用旧引用；
- 恶意/故障 broker 在 error 或 panic 中夹带 backend Secret；
- caller 修改输入 map、getter 返回值或 baseline，试图改变已摘要快照；
- 使用 typed-nil broker 绕过接口非空检查。

### 3.3 当前受信任主体

- Registry/Host 与 host-owned HMAC key；
- 平台注册的 declarative config schema；
- 随 binary 编译且经过平台 admission 的 broker 与 Factory；
- OS 随机源。

当前不信任插件 manifest 自报的 provenance/approval，不信任配置 layer 自报的 manifest/admission，
也不信任任意第三方 Go plugin、MCP server、Skill 或 App 代码。第三方执行隔离仍属于后续阶段。

## 4. 冻结的领域对象与 API

### 4.1 Host-owned declarative schema

`ConfigSchemaDefinition` 取代任意 validator callback。W1 普通值故意只允许 bounded enum；每个字段
声明 required/default/enum。Secret field 声明：

```text
logical name + required + purpose + audience + allowed brokers
```

Host 先 normalization，再计算 domain-separated schema digest。非法/重复字段、Secret-like 普通
字段名、非法 UTF-8、控制字符、Secret canary、未启用却携带的 default、重复 enum/broker 或超界输入
全部在注册前拒绝。Schema 与 manifest 的完整 Secret logical-name 集合必须相等。

这样做牺牲了 W1 的自由字符串配置能力，换取公开 canonical value 与 Secret 的明确数据分类。未来若
增加 integer、URL 或 typed string，必须为每种类型单独定义 canonical、安全分类和边界，不能重新
引入任意 callback。

### 4.2 Broker definition

`SecretBrokerDefinition` 绑定：

```text
schema version + broker ID + semantic version
+ implementation digest + policy revision + supported purposes
```

Host 对 normalized definition 计算 digest。Claim 同时保存 broker definition digest 和 policy revision；
同名 broker 替换实现或改变 policy 后，旧 binding 不能继续通过 Compose/NewHost。

### 4.3 Claim admission

`AdmitSecretClaim` 的完整输入绑定：

```text
idempotency key
+ tenant + row
+ plugin ID/version + artifact digest
+ manifest digest + admission revision
+ config schema digest + logical name
+ broker ID + purpose + audience
+ presented raw locator
```

Raw locator 先经 host-keyed HMAC 变为 `LocatorBinding`。Canonical claim 只保存 HMAC binding，不保存
locator 原文；claim digest 使用独立 SHA-256 domain。Exact retry 返回同一
`SecretClaimReference` 且不重复调用 broker；相同 idempotency key 携带不同 canonical 请求时返回
`ErrSecretClaimConflict`，不让 retry 换 locator。

Broker error 或 panic 统一折叠为 `ErrSecretClaimDenied`，不向 caller 转发 backend 文本。

### 4.4 Composition input 与 Factory output 分开

```go
ConfigurationInput {
    Values
    SecretClaims
}

PluginConfig {
    Values
    SecretBindings
}
```

配置 layer 只接收 claim digest/revision。Compose 根据当前 Registry 重新验证完整 scope/trust/policy，
再物化为 `SecretBindingView`：

```text
broker ID + broker definition digest
+ claim digest/revision
+ host-keyed binding fingerprint
+ broker policy revision
+ scope digest
```

该 view 没有 Secret material、locator、resolve token、lease ID 或执行方法。复制 view 不会增加权限。

### 4.5 Revocation

`RevokeSecretClaim` 使当前 in-memory claim 失效：

- 撤销后重新 Compose 失败；
- Compose 完成后、NewHost 前撤销，activation 失败；
- exact retry 不能复活已撤销 claim。

由于当前不存在 action-time resolver，本阶段不能声称“运行中外部 credential lease 已即时撤销”。
该保证必须由未来短 TTL lease、broker/executor revoke 与每次 action-time policy check 提供。

## 5. Canonical 与版本迁移

P0-4 改变了配置层和 effective snapshot 的 preimage，因此没有原地伪装成兼容修改：

| 对象 | Schema/domain | 关键变化 |
|---|---|---|
| Plugin Manifest | v1 | secret requirement names 与 config schema digest 继续进入 manifest claim |
| Config Schema | v1 | host-owned declarative value/Secret fields |
| Secret Broker Definition | v1 | implementation/policy/purpose exact bind |
| Secret Claim | v1 | raw locator 的 host-keyed binding + 全 scope/trust bind |
| Configuration Layer | v2 | canonical 保存 materialized `SecretBindingView`，不保存 caller raw reference |
| EffectiveConfiguration | v3 | row 保存 binding view、manifest/admission/schema 与能力声明 |

固定 golden/digest：

| Vector | Digest |
|---|---|
| Manifest v1 | `sha256:24b280244cdad62d5f019537451e7df05ca0eab6954b7b5f064e7ecdf83fd89a` |
| Config schema v1 | `sha256:f7b4dab60180aa172c3d413a2e46a18f6e4919559fa1aca5d20f5c8056caec7c` |
| Broker definition v1 | `sha256:a1493b2752af1c6240ac045d02ee584f08b41fa52287c2b858282194e6caf6f5` |
| Secret claim v1 | `sha256:8c55cd2021fb0b551b646516be1d6078fec89148b2dd9361276b96a5ccbcaa47` |
| EffectiveConfiguration v3 | `sha256:d16801e019d692c39fc93362a85bb2a507422ea32974e4b1fc75c09c50960acb` |

Golden 只固定 canonical codec，不证明真实 Secret backend、KMS、跨进程持久化或外部执行安全。

## 6. 代码落点与可复核验收

| 合同 | 代码 | 自动证据 |
|---|---|---|
| 三类 Secret 对象与 declarative schema | `apps/im-api/internal/plugins/types.go` | 编译期类型分离；schema/broker golden |
| schema/broker normalization、digest、claim/revoke | `registry.go` | `TestSecretAdmissionCanonicalGoldenVectors`、schema registry tests |
| raw locator 脱离 composition | `registry.go`, `composition.go` | `TestSecretAdmissionKeepsRawLocatorOutsideCompositionAndFactory` |
| ordinary/default Secret canary 拒绝 | `registry.go`, `composition.go` | `TestSecretCanariesAreRejectedFromOrdinaryValuesAndSchemaDefaults` |
| unknown broker/forged locator | `registry.go` | `TestSecretAdmissionRejectsUnknownBrokerAndForgedLocator` |
| exact retry/conflict | `registry.go` | `TestSecretAdmissionRetryIsExactAndIdempotencyConflictFailsClosed` |
| tenant/row/plugin/logical-name anti-replay | `composition.go` | `TestSecretClaimReferenceCannotReplayAcrossBoundScope` |
| manifest/admission/schema/purpose/audience/policy drift | `composition.go` | `TestSecretClaimRejectsTrustAndPolicyDrift` |
| error/panic firewall | `registry.go` | `TestSecretBrokerErrorAndPanicNeverLeakBackendMaterial` |
| forged/revoked claim | `composition.go`, `lifecycle.go` | `TestForgedAndRevokedSecretClaimsAreRejectedByComposeAndActivation` |
| typed-nil broker | `registry.go` | `TestRegisterSecretBrokerRejectsTypedNil` |
| Effective v3 immutable snapshot/diff | `composition.go` | existing composition golden/determinism/diff suite |

阶段门禁：

```text
go test ./apps/im-api/...
go test -race ./apps/im-api/internal/plugins
go vet ./apps/im-api/...
git diff --check
```

上述门禁在提交 `211ada7` 前通过。它们证明记录的 Go 合同和负向断言通过，不证明生产 Vault、
真实模型、真实 IM、网络隔离、外部 connector 或商用 SLO。

## 7. 已关闭与明确未关闭

### 7.1 P0-4 已关闭

- caller raw locator 不再进入 public configuration canonical/digest/diff/getter/Factory；
- 普通 config value/default 不能携带本阶段 Secret canary；
- claim 不能跨 tenant/row/plugin/logical name 重放；
- manifest/artifact/admission/schema/broker/purpose/audience 漂移 fail-closed；
- exact retry 不重复 broker validation，冲突 retry 不换 locator；
- broker error/panic 不回显 backend material；
- forged/revoked/typed-nil 路径有负向证据；
- configuration/effective schema 明确升级，不用旧版本号掩盖 preimage 变化。

### 7.2 仍未关闭，禁止误报

1. **Action-time JIT Secret lease**：没有 typed action intent、短 TTL/PoP token exchange、trusted
   executor、provider receipt、runtime revoke/reconcile；这是 RQ-021 的后半段。
2. **持久化与密钥生命周期**：claim store 和 HMAC key 当前是进程内 W1 状态；没有 KMS/Keychain、
   key version/rotation、backup/restore、跨重启稳定 identity。
3. **Registry 并发 freeze**：entries/schema/broker map 尚未 immutable freeze；broker validation 的
   并发/阻塞隔离也未完成，不能承载不可信或可能无限阻塞的 broker。
4. **进程/UID/容器隔离**：broker、Factory 和 Host 仍在同一 Go 进程；只有 trusted built-ins 可用。
5. **全系统 canary/DLP**：当前负向测试覆盖 plugin configuration 边界，不等于日志、event、Artifact、
   model prompt/response、metrics、trace 和备份的端到端扫描已经完成。
6. **通用 public config 类型**：W1 只支持 bounded enum；URL、整数、结构体、动态 provider config
   需要后续逐类型冻结 canonical 和 validation。
7. **运行中撤销**：Compose/NewHost 会拒绝撤销状态，但没有 action-time bearer，因此尚不存在可验证的
   live credential kill SLA。

## 8. 下一步依赖顺序

P0-4 后不直接接真实 Secret 或真实 IM outbound。W1 下一阶段顺序为：

1. Registry freeze/immutable snapshot 已在 `e2f82be` 完成，证据见专题 25；
2. effect scope `open -> closing -> closed`，拒绝 cleanup 开始后的迟到注册；
3. Host callback 不持全局 lifecycle mutex，并冻结 reentrancy/concurrency 合同；
4. 对忽略 context 的插件建立独立进程隔离边界，in-process timeout 不冒充可强杀；
5. 补 source-only drift、NFC/跨语言 canonical 与 conformance vectors；
6. 再实现明确标记为 volatile 的 MemoryFake EventStore；
7. W2/W4 Action Plane 建立后，单独实现 action-time Secret lease/executor/receipt。

这条顺序保留 DeepSeek Harness 的 capability seam 和可逆生命周期优点，同时拒绝其同 UID Secret、
任意插件代码和“模块存在即控制生效”的风险。
