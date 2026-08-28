# 原生 IM E2 Provider Bundle 离线闭环证据

> 证据日期：2026-08-28（Asia/Shanghai）  
> 代码节点：`ee0666fe3e956234cbd653abd0ea57bdba322cb7`  
> 分支：`mainline_continue_quantum_entanglement`  
> 证据级别：离线 provider-bundle compatibility evidence；不是生产发布批准  
> 网络状态：真实 IM endpoint、credential、DNS、TLS、HTTP/WebSocket 与 outbound 全部未启用

## 1. 阶段结论

本阶段已经把此前分离的 provider approval、transport、mapper、adapter 和 durable inbox 串成一个
可执行、可复核、零网络的 provider bundle 闭环：

```text
approved config/profile/manifest
  -> scripted zero-network exchange
  -> provider-specific transport mapping
  -> signed raw response + per-read exchange evidence
  -> HMAC raw-body verification
  -> pure provider mapper
  -> provider-neutral canonical page/events
  -> exchange-enhanced admission provenance
  -> atomic SQLite page admission
  -> canonical provenance/digest readback
```

这关闭了“transport TCK 和 mapper TCK 各自通过，但组合后没有证据”的缺口，也关闭了“每次网络
读取证据被错误塞进稳定 event，导致同一 provider event 重送时 canonical event 漂移”的语义缺口。

当前可以把一个真实 provider bundle 接到同一套离线 TCK 和 approved composition 中继续验证；
当前仍不能直接连接真实 IM，因为 production exchange、真实 provider contract/fixture、批准 scope、
read-only secret reference 和 sandbox runbook 尚未提供或实现。

## 2. 最重要的证据分离

### 2.1 稳定事件来源证据

`InboundIMEventV1.transport_evidence_digest` 保持为稳定的 event-source evidence。相同 provider
event 在不同 read request 中重送时，这个值不因 TCP/TLS session、received time、request ID 或
cursor 变化而变化。因此 event canonical bytes 和 event digest 可以保持稳定，durable inbox 的
`scope + eventId -> eventDigest` 约束不会被一次读取的偶然状态污染。

### 2.2 每次读取交换证据

新增 `NativeIMInboundReadExchangeEvidenceV1`，独立绑定：

- `readRequestId`；
- `readRequestDigest`；
- `afterCursor` / `afterSequence`；
- `snapshotToken`；
- `receivedAt`；
- provider request-intent digest；
- exchange-security evidence digest；
- stable event-source evidence digest；
- 对上述全部字段重算得到的 evidence digest。

它属于 read/admission provenance，不属于 event identity。同一 event 经两次不同 exchange 返回时，
event-source digest 可以相同，而 exchange evidence digest 必须不同。

### 2.3 持久 provenance

新增 `NativeIMSandboxExchangeAdmissionProvenanceV1`，在既有 approval/profile/manifest/transport/
mapper/request/page/mapping 绑定上嵌入完整 read-exchange evidence。adapter 只在 transport 显式实现
enhanced exchange port 时生成该模型；旧 transport 仍生成 legacy
`NativeIMSandboxAdmissionProvenanceV1`，保持读取兼容。

SQLite migration 仍停留在 v6。理由不是省略审计，而是 migration 6 的 provenance 表已经同时保存：

- 共同高价值字段的独立列；
- bounded canonical provenance JSON；
- provenance domain digest。

增强 evidence 完整进入 canonical JSON 和 digest，公共列继续做双重 readback 校验。旧 canonical
JSON 由 legacy decoder 读取，新 JSON 由 enhanced decoder 读取；不需要为一个已有 bounded JSON
扩展占用 `0007`。因此编号继续冻结为：

```text
0005_native_im_inbox
0006_native_im_sandbox_provenance
0007_atomic_invocation_results
0008_native_im_actions
```

## 3. Provider Mapper TCK

可复用 test-only Mapper TCK 固定验证：

- exact mapper type 与 fresh instance；
- 同实例、不同实例确定性；
- 输入不可变；
- file/socket/DNS/subprocess/browser/thread/process/SQLite/environment 等 effect fence；
- canonical page bytes；
- source body、request、capability、profile、page 和 mapper contract 的 evidence 重算；
- 五类固定、脱敏 rejection code；
- accepted/rejected vector ID 唯一且 suite digest 稳定。

Synthetic semantic mapper 的结果为：

| 项 | 结果 |
|---|---|
| accepted vectors | 3 |
| rejected vectors | 6 |
| suite digest | `e569232b71e0989d4577604e0452b4ccb058c6b80ca981eac8334da6b34f5d51` |
| fresh process/hash seed | 通过 |

该 mapper 只证明 compatibility kit 和 semantic mapping 机制，不代表真实 provider schema 已冻结。

## 4. Provider Transport TCK

Transport TCK 使用 injected `NativeIMProviderExchangePortV1`。provider transport 只能构造 request
intent、解析 response 和形成证据；DNS/TLS/socket/Authorization 只能由未来 production exchange
拥有。当前 exchange 是 exact immutable script，不暴露 endpoint、dispatch、acceptance query 或
任何网络实现。

覆盖矩阵包括：

- health exact 200/body/received-time/config/profile/manifest/contract binding；
- initial read；
- cursor/sequence/snapshot continuation；
- read request ID/digest 与 request-intent digest；
- exact signed header inventory；
- cross-request response rejection；
- disconnect/timeout 脱敏；
- 204、206、3xx、4xx、429、5xx 全部拒绝；
- opaque credential lease，transport 不调用 `SecretMaterial.view()`；
- close idempotency、closed-state rejection；
- default-off、no endpoint/no outbound surface；
- stable event source 与 transient exchange evidence 分离。

当前结果：

| 项 | 结果 |
|---|---|
| accepted paths | 5 |
| rejected paths | 12 |
| suite digest | `173a05e443a1506a41a23cf17ca834c08b667a0d28e46fd1d186b64cd106c1d4` |
| fresh process/hash seed | 通过 |

## 5. Provider Bundle 闭环

Bundle TCK 不直接拼接预制 canonical page，而是从 signed provider wire body 开始：

1. 构造与 approved config 完全绑定的 read intent；
2. scripted exchange 返回 provider semantic JSON、detached signature headers、received time、
   exchange-security evidence 和 stable event-source evidence；
3. enhanced transport 形成 raw response + read-exchange evidence；
4. adapter 校验 live approval，并关闭 read credential lease；
5. raw-body verifier 使用独立 verification-key lease 完成 HMAC、timestamp、nonce 和 body digest
   验证；
6. pure mapper 只接收 verified raw body、request、capability、verification 和 profile；
7. parser 重验 scope、request、capability、conversation、event 与 evidence binding；
8. adapter 重算 mapper evidence，形成 exchange-enhanced provenance；
9. SQLite inbox 预登记 exact read request；
10. approval admission guard 与 store transaction 共同覆盖 nonce/page/event/verification/link/read/
    checkpoint/provenance admission；
11. store 从 canonical JSON 和 provenance digest 回读增强 evidence。

Bundle suite digest：

```text
7fbdec73b0bbe74e18e721c39ae623548f5dfe6dfa97bbf14c4a716bdc50d4e7
```

`scripts/verify_native_im_provider_bundle_tck_v1.py` 已在不同 `PYTHONHASHSEED` 的独立新进程中得到
相同结果。脚本只接受零参数，拒绝 `--write`，并使用临时 SQLite 文件；read path 在 zero-effect
fence 内运行。

## 6. 提交账本

| Commit | 交付 |
|---|---|
| `ca2fd8d` | adapter 重算 mapper evidence，不接受 mapper 任意自报 |
| `90b1189` | 修复 provider sandbox golden 的 provider/scope 混用 |
| `2815251` | 固定五类 mapper rejection contract |
| `56e97ff` | 可复用 Mapper TCK |
| `98be066` | semantic synthetic mapper candidate |
| `4fb38bc` | mapper fresh-process/hash-seed verifier |
| `a8b66bd` | zero-network provider exchange seam |
| `83c16c1` | health evidence 绑定 config/profile/manifest/transport/intent/exchange |
| `1293bb4` | scripted transport TCK |
| `2cf7ba7` | transport fresh-process/hash-seed verifier |
| `f9f2a6b` | 独立 read-exchange evidence model |
| `32971e7` | enhanced raw-exchange transport port |
| `be49b57` | exchange-enhanced admission provenance |
| `31d2c7d` | adapter admission 采用增强 evidence，legacy 兼容 |
| `cb3d714` | migration-v6 provenance 表持久化与回读增强 JSON |
| `3f21f31` | provider bundle 到 atomic durable admission 的闭环测试 |
| `9141ad7` | provider bundle fresh-process/hash-seed verifier |
| `ee0666f` | 全仓 native-IM sandbox formatter 收敛 |

文档和 migration resequencing 的独立提交包括 `249c0b5`、`8a28938`、`dfea1ff`、`be86f89`。

## 7. 本机验证证据

代码节点 `ee0666f` 的本机门禁结果：

| Gate | 结果 |
|---|---|
| full pytest | `2,386 / 2,386` passed |
| Ruff lint | passed |
| Ruff format | `207 files already formatted` |
| strict mypy | `65 source files` passed |
| Mapper TCK verifier | passed |
| Transport TCK verifier | passed |
| Bundle TCK verifier | passed |
| Git diff check | passed |

pytest 只保留既有 Python 3.13 multi-threaded `fork()` deprecation warnings；没有 test failure。

## 8. 当前真实接入边界

### 8.1 已经足够作为真实 provider 实现的底座

- exact provider profile/config/approval/manifest；
- durable approval high-water 与 live permit；
- default-off approved composition；
- provider transport、exchange、pure mapper 三段边界；
- mapper、transport、bundle 三套独立 verifier；
- stable event identity 与 transient read exchange 证据分离；
- raw-body authentication、nonce replay、atomic inbox/checkpoint/provenance；
- zero-network、secret/message/trace canary、kill switch 与 lifecycle；
- migration 5/6、backup/topology 基础。

### 8.2 连接真实 sandbox 前仍必须完成

只剩 provider-specific 的五类输入/实现，不需要先做 E3/E4：

1. IM 后端提供并冻结测试 endpoint class、health/read 方法、event schema、认证、cursor/snapshot、
   limit/rate/error/maintenance contract；
2. 冻结测试 tenant/workspace/channel/conversation allowlist、合成数据等级、截止时间、审批人与 kill
   switch；
3. 创建真实 provider profile、manifest、golden fixtures 和 mapper candidate，并通过 Mapper TCK；
4. 实现 production exchange 的 DNS/TLS/IP/redirect/timeout/body-limit/credential 边界，再让 provider
   transport 通过 Transport/Bundle TCK；
5. 单独修订 `SERVICE_BOUNDARY.md` 和 sandbox runbook，经人工审阅后才启用 health，再启用 read/
   dedupe/resume。

如果后端合同和测试 scope 已完整提供，上述工作是一个明确的 provider adapter 任务，不再是平台
内核重构。Level B inbound-only 不需要等待 `0007_atomic_invocation_results` 或
`0008_native_im_actions`。

### 8.3 仍禁止

- 不连接飞书、企微、真实用户、生产群聊、bot 或 webhook；
- 不向任何人或群发送消息；
- 不让 Level B observation 驱动 Agent、tool、browser、subprocess 或 outbound；
- 不从 prompt、模型或消息正文选择 endpoint、credential、tenant、conversation 或 capability；
- 不把 synthetic candidate 宣称为真实 provider interoperability；
- 不把 full pytest 或 TCK 通过宣称为生产 Gate A–E 通过。

## 9. 下一阶段

当前最短路径不是继续扩平台合同，而是等待/接收 IM 后端具体合同，然后实现第一个真实 provider
bundle。建议顺序：

```text
provider contract fixture
  -> profile + mapper TCK
  -> production exchange review
  -> transport + bundle TCK
  -> health-only window
  -> one allowlisted read
  -> duplicate/dedupe
  -> disconnect/resume
  -> Level B checkpoint
```

E3 Atomic Result Authority、PURE Agent draft 和 E4 fake-only Action Plane 继续保留为接入后的 TODO。
任何 outbound 都必须等 E4 完成并由用户针对具体测试环境另行明确授权。
