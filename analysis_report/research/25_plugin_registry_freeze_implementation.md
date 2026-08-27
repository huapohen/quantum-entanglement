# Plugin Registry Freeze：从可变 Builder 到不可变定义快照

> 状态：W1 P1-1 已在 `e2f82be` 实现。
>
> 证据根：`/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output/more`
>
> 边界：本阶段冻结 plugin/schema/broker definition graph；Secret claim/revocation 仍是单独的动态状态，
> package fleet 热升级、持久化 registry、跨进程分发与第三方代码隔离尚未实现。

## 1. 决策

Registry 不再依赖“启动后大家自觉不调用 Register”的约定。生命周期被固定为：

```text
builder phase
  RegisterConfigSchema
  RegisterSecretReferenceBroker
  Register / RegisterFactory
        |
        v
Freeze
  re-normalize + re-digest + validate complete graph
  clone builder maps into final private snapshot
  permanently close every registration path
        |
        v
runtime phase
  Resolve / ResolveSelection / Compose
  AdmitSecretClaim / RevokeSecretClaim
  NewHost
```

未冻结 Registry 不能 Resolve、Compose、admit/revoke Secret claim；`NewHost` 也拒绝构造。Freeze 后
schema、broker、package 或 factory 的 late registration 统一返回 `ErrRegistryFrozen`。Freeze 幂等，
但失败的 Freeze 不封死 builder，允许补齐缺失 definition 后重新验证。

## 2. 研究证据到硬合同

| 来源 | 研究原意 | 派生合同 |
|---|---|---|
| `agentspace/research_report.md:1852-1861` | App registry 的 passport 包含 publisher、release digest、SBOM、permission manifest、network/Secret requirements、兼容、测试、漏洞和撤销；runtime start 前验证 digest/policy。 | runtime 读取的 definition graph 必须是完成准入后的同一份快照，不能在 validate→start 间继续注册或替换。 |
| `tech-agent-security-governance/research_report.md:267-276` | private registry/allowlist/pin/risk diff，immutable version，升级扩权重批，运行 enforcement 和撤回。 | Register 是 builder 操作；Freeze 是 admission boundary；冻结后 mutation 不是“更新”，而是必须构造新 Registry/新批准快照。 |
| `deepseek-harness/research_report.md:489-541` | DSH 安装/Bundle 入口真实存在，但缺强制 publisher/capability/组织 allowlist/兼容认证/撤销；lock 不代表来源可信。 | 保留 profile/bundle 组合，不允许 package manager 或 prompt 在 runtime 动态改变当前 Registry。 |
| `deepseek-harness/research_report.md:830-848` | 生产基线要求内部 registry、固定 hash、签名/SBOM/扫描、replay/canary/rollback。 | Freeze 只关闭进程内定义漂移；签名/SBOM/扫描和跨版本 rollout 仍由后续 Trust Registry 提供，不能由 Freeze 冒充。 |
| `tech-agent-security-governance/research_report.md:315-333` | Secret 使用需 broker、scope、短期 token 和撤销。 | definition freeze 与 Secret claim/revocation 分锁：broker 定义不可变，claim/revocation 保持可变，避免“为了不可变”取消运行期撤权。 |

对应 RQ-008、RQ-009、RQ-017、RQ-021、RQ-042。

## 3. Freeze 重新证明什么

Freeze 不只设置一个 boolean。它在写锁内重新验证：

### 3.1 Config schema

- schema 可重新 normalization；
- normalized canonical digest 与 map key 完全相等；
- 每个被注册 plugin 的 manifest Secret names 与 schema Secret fields 完全一致。

### 3.2 Secret broker

- definition 可重新 normalization；
- map key 等于 broker ID；
- implementation/policy/purpose canonical digest 等于注册时 digest；
- broker interface 不是 nil 或 typed-nil；
- schema 允许的每个 broker 均存在并支持该 field purpose。

### 3.3 Plugin/package/factory

- manifest 可重新 normalization；
- map key 等于 manifest plugin ID；
- host 重算 manifest digest，必须等于 Registry entry digest；
- `PackageRecord` 再次通过 plugin/version/artifact/approved-manifest/admission/provenance/SBOM/approval/
  revoked 检查；
- 非空 factory 不能是 typed-nil。

Freeze 不调用 Factory `Manifest`、Configure 或 broker validation callback，因此不会在验证快照时产生
插件副作用。Freeze 后调用 `RegisterFactory` 会在调用外部 `Manifest()` 前先拒绝，避免冻结状态仍触发
late factory callback。

## 4. 不可变快照与并发合同

Registry 使用独立的 definition `RWMutex`：

- Register/Freeze 使用写锁；
- Resolve/Compose/NewHost 使用读锁；
- AdmitSecretClaim 只在读锁中取得冻结 entry/schema/broker 值，随后释放 definition lock；
- Secret claims 使用原有独立 mutex，避免 definition 与 revocation 混成一把全局锁。

Freeze 重新分配并深拷贝 entries、manifest slices、schema slices 和 broker-purpose slices。即使 builder
阶段旧 map 引用被测试代码修改，runtime snapshot 仍不变化。Factory/broker interface 本身不做对象深拷贝；
它们仍必须是随 binary 编译、平台准入的 trusted built-ins，第三方对象不得进入当前进程。

ResolveSelection 不再临时创建一个可变 Registry 再调用 Resolve，而是在已持有 snapshot read lock 时从
选中 entries 构造纯 plan，避免嵌套锁和半冻结临时对象。

没有增加“整个 Registry 的全局 digest”。EffectiveConfiguration 只绑定被选中 plugin 的 artifact、
manifest、admission、schema、broker/claim 与 dependency plan；新增一个未选中的已准入 package 不应改变
现有部署 candidate digest。若未来需要 inventory/rollout 级 snapshot identity，应另建
`RegistryRelease`，不能污染 selected deployment identity。

## 5. 失败语义

| 情况 | 结果 |
|---|---|
| nil Registry Freeze/Register/Resolve | `ErrInvalidRegistry` |
| Resolve/ResolveSelection/Compose/claim/revoke before Freeze | `ErrRegistryNotFrozen` |
| missing schema、manifest/schema Secret mismatch | `ErrInvalidRegistry` |
| missing broker、unsupported purpose、broker/manifest digest tamper | `ErrInvalidRegistry` |
| schema/broker/package/factory late registration | `ErrRegistryFrozen` |
| repeat Freeze | success，不生成第二个 snapshot |

`NewHost` 继续对外折叠为 `ErrInvalidActivation`，避免把 activation 的内部失败细节当作可枚举接口。

## 6. 验收证据

| 自动测试 | 证明 |
|---|---|
| `TestRegistryDefinitionReadsAndSecretClaimsRequireFreeze` | 所有 runtime read/claim 入口的 freeze 前 fail-closed |
| `TestRegistryFreezeValidatesCompleteSchemaAndBrokerGraph` | missing/mismatch/unsupported/tampered definition 拒绝 |
| `TestRegistryFreezeIsIdempotentAndClosesEveryRegistrationPath` | 幂等 Freeze 与四类 late registration 关闭 |
| `TestRegistryFreezeDetachesFinalSnapshotFromBuilderMaps` | Freeze 后 snapshot 与 builder maps 分离 |
| `TestFrozenRegistryConcurrentReadsClaimsAndRejectedWrites` | Resolve、Compose、exact claim retry 与拒绝写并发；race gate 无 map race |
| `TestRegisterFactoryRejectsTypedNil` | typed-nil factory 不能进入 Registry |
| 全部 composition/lifecycle/Secret tests | 原 P0-1～P0-4 合同迁移到必须 Freeze 后仍成立 |

通过门禁：

```text
go test ./apps/im-api/...
go test -race ./apps/im-api/internal/plugins
go vet ./apps/im-api/...
git diff --check
```

## 7. 明确未完成

1. Registry snapshot 仍是进程内内存对象，没有数据库、签名 inventory、跨节点发布或恢复；
2. PackageRecord 没有 Freeze 后的 fleet revocation/rollout API；当前 package revocation 是构建新快照，
   Secret claim revocation 则是独立动态状态；
3. broker validation callback 仍在当前进程执行，尚未做超时、并发 singleflight、独立进程或 hostile
   broker containment；
4. Factory/broker 实例是 trusted interface 引用，不是可复制的不可变纯数据；
5. 第三方 package 的 signature/SBOM/provenance scan、sandbox behavior、canary rollout 和 rollback 仍在
   W4/W7；
6. action-time Secret lease、provider receipt 与真实 IM outbound 仍未开放。

## 8. 下一步

W1 effect scope `open -> closing -> closed` 已在 `0f00b47` 完成，证据见专题 26；Host callback
持锁/reentrancy 已在 `3b8e02e` 完成，证据见专题 27；callback panic containment 已在 `2d97f0a`
完成，证据见专题 28。下一个独立提交冻结忽略 context 时只能依靠进程隔离强制终止的边界。
