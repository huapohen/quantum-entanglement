# 原生 IM Provider-Neutral Contract V1

> 状态：V1 文档已冻结；`IM-P0 CONTRACT_READY` 尚未达成
>
> 日期：2026-08-27（Asia/Shanghai）
>
> 适用范围：Quantum Entanglement 与独立原生 IM 后端之间的适配边界
>
> 生产状态：Gate A–E 全部关闭；本文不授权连接真实 endpoint 或发送消息

## 1. 决策

原生 IM 是外部渠道和用户交互系统；Quantum Entanglement 是 Agent 编排、任务状态、结果、
授权和外部 Action 的事实拥有者。两者通过 provider-neutral port 连接，不共享数据库，不让 IM
后端成为 Agent task/result 真相源，也不让 Agent、Prompt 或 `MentionRouter` 直接调用 IM SDK。

```text
Native IM backend
  -> authenticated provider adapter
  -> IMGatewayPort
  -> verified inbound envelope
  -> digest-bound durable inbox admission
  -> invocation / result
  -> action-time authorization
  -> durable IM Action Command
  -> IM-specific fenced dispatcher
  -> IMGatewayPort
  -> receiver Action Receipt / acceptance query
```

V1 冻结平台值模型、严格 codec、port 语义、fake adapter 和 contract tests。真实 provider profile、
endpoint、credential、生产身份、真实读取和真实发送继续关闭。

## 2. 版本规则与不变量

### 2.1 兼容规则

- 每个 wire model 都带 exact integer `schemaVersion=1`；
- 缺字段、未知字段、未知 enum、重复 JSON key、非 plain container 全部失败关闭；
- 新增或删除字段、改变 required/nullable、扩大 enum、改变 digest domain 或 canonical 规则均为 breaking
  change，必须新建 V2；
- V1 decoder 不尝试猜测、降级或透传未来版本；adapter 必须显式宣告所支持的 exact version；
- Python 代码使用 snake_case，wire 使用本文列出的 exact camelCase key；二者不是两个版本。

### 2.2 安全与一致性不变量

1. 所有 ID 都是 provider 返回或平台生成的 opaque text，adapter 不解析其中业务意义；
2. scope tuple 固定为 `(tenantId, workspaceId, provider, channelId)`；所有独立引用和结果都绑定 scope；
3. caller/Agent 提供的 scope、sender、role、authorization、capability 和 receipt 都不是可信事实；
4. 入站 `eventId` 负责 transport 去重，`messageId` 负责业务对象身份，二者不能混用；
5. 同一 scope + event ID 的不同 event digest 是冲突攻击或上游损坏，不是重复投递；
6. outbound `actionId` 是业务副作用身份，transport attempt、command 或 message ID 不能替代它；
7. receiver receipt 与 Agent Result Receipt 是两种不同证明；
8. 发送超时、ACK 丢失或 crash-after-send 进入 `effect_unknown`，不得猜成功或盲重试；
9. 只有带权威“未接受”证明的 transient NACK 才能进入有界重试；
10. connector 不接收 prompt 生成的 endpoint、credential、tenant、conversation 或 capability；
11. V1 payload 不携带 secret、cookie、签名原文、Authorization header、租约明文或临时下载 URL；
12. 所有文本、集合、附件、整数、模型和整页 canonical bytes 有界；
13. 默认只注册 fake connector，fake outbound 默认关闭；
14. P0 不打开 socket、DNS、HTTP、WebSocket，不读取环境 credential，也不写入真实 IM。

## 3. 职责与当前仓库边界

| 领域 | 原生 IM 后端 | Quantum Entanglement |
|---|---|---|
| Tenant/成员 | 返回稳定身份、成员版本和服务身份 | 绑定可信 RequestContext，并在 Action 时重新授权 |
| Conversation | room/thread/message 的 durable identity | 保存 scope-bound reference，不复制 IM 业务库 |
| 入站 | 签名、nonce、event cursor、provider ACK/NACK | 验证 envelope、inbox digest 去重、任务触发和恢复 |
| Agent 执行 | 不负责 | admission、attempt、worker、result、Artifact |
| 出站 | 接收幂等键、执行 Action、返回/查询 acceptance | Action Command、授权、fenced dispatch、receipt、unknown reconcile |
| 限流 | provider 配额、Retry-After 和终态错误 | bounded retry、DLQ、Needs You 和操作审计 |
| 附件 | immutable object identity 与受控读写 | 保存不可变引用、摘要和分类，不保存临时 credential |
| 审计 | provider operation/message evidence | request→invocation→result→action→receipt 全链关联 |

当前仓库中的下列组件只能复用基础能力，不能直接充当 V1 connector：

- `chat.MentionRouter` 必须位于 verified envelope 和 durable inbox admission 之后；它现有的
  `provider + external_message_id` 幂等边界不足以承担跨 tenant/channel transport 去重；
- 现有 inbox 对重复 message ID 的 fast path 不比较 canonical event digest；IM admission bridge 必须
  先持久化并比较 `(scope, eventId, eventDigest)`，再调用后续路由；
- 通用 `OutboxPublisher` 会对 callback exception 和 crash 后的 in-flight 记录执行 at-least-once
  retry；它不能原样驱动 IM Action。IM-P2 必须增加 `effect_unknown` fence 和 acceptance reconcile；
- 现有 publisher/store 可以复用事务、claim 和审计原语，但不能绕过本文的 Action 状态机；
- `consumerId`、outbox destination 和 receiver ledger key 都必须机械绑定完整 scope。

Verified inbox 进入现有 chat/runtime 之前必须新增 admission projection bridge，不能把当前
`InboundChatMessage`/`MentionRouter.route()` API 原样接线。该 bridge 必须：

1. 从 `IMVerifiedInboundEnvelopeV1` 和 digest-bound inbox receipt 读取可信 scope、conversation、
   sender、message segments、event ID/digest 和 traceparent；
2. 将 mention segments 映射到 scope-bound roster actor，未知或跨 scope mention 失败关闭；
3. 创建 `CoordinationEnvelope` 时使用 `idempotencyKey="native-im-event:" + eventDigest`、
   `correlationId=event.correlationId`、`causationId=eventId`，并原样传播 verified envelope 的
   traceparent；event 原始 `causationId` 保留在只读 payload/audit evidence 中，不覆盖新 envelope
   指向直接前驱 event 的 causation；
4. 在 payload 中保留 tenant/workspace/provider/channel/conversation/message/event digest 引用，不把
   transport credential、验证原文或临时附件 URL 带入；
5. 要么扩展 chat ingress model 和 router 以携带上述字段，要么新增独立 router；两条路径都必须先
   经过 durable inbox，且不得让现有无 scope/trace 的构造器静默丢字段。

## 4. Exact codec 与公共标量

### 4.1 Plain value

- `from_dict()` 只接受 `type(value) is dict`；array 只接受 `type(value) is list`；
- boolean 只接受 `type(value) is bool`；integer 只接受 `type(value) is int`，因此 bool-as-int 被拒绝；
- V1 不接受 float、NaN、Infinity、Decimal、自定义 Mapping、tuple、bytes 或任意递归 payload；
- `null` 只在字段明确标注 `| null` 时允许；空字符串不能代替 null；
- `to_dict()` 只产生 JSON-safe plain dict/list/string/integer/boolean/null，并深拷贝可变输入。

每个模型还提供 `from_json_bytes()`：只接受 `type(value) is bytes`，先执行该模型顶层 byte limit，
再用 strict UTF-8 和保留 object pairs 的 JSON parser 检测重复 key，最后调用 `from_dict()`。
`from_json_bytes()` 可以接受字段顺序或空白不同但语义合法的 JSON；`canonical_bytes()` 始终只输出
第 4.3 节的唯一形式，且 `from_json_bytes(canonical_bytes()).canonical_bytes()` 必须逐字节不变。

### 4.2 Text、ID、整数和时间

- 所有 string 必须已经是 Unicode NFC；decoder **拒绝**而不是静默规范化非 NFC 输入；
- 所有 string 拒绝 surrogate code point；ID、enum、revision、cursor、error code 和 opaque ref 必须
  非空，并拒绝全部 C0 (`U+0000–U+001F`) 与 DEL (`U+007F`)；
- display text 允许空字符串但拒绝全部 C0 与 DEL；
- message text segment 允许 HT (`U+0009`) 和 LF (`U+000A`)，拒绝其他 C0、DEL 和 CR；换行只用 LF；
- canonical lexical order 定义为 NFC string 的 UTF-8 bytes 升序；要求有序的集合必须无重复；
- 所有 integer 限于 signed 64-bit；`byteSize`、sequence、retention 和 byte limit
  为非负；`attemptNumber`、`limit`、`retryAfterSeconds` 为正数；
- timestamp 必须精确为 UTC 微秒 `YYYY-MM-DDTHH:MM:SS.ffffffZ`，不接受 offset 或低精度；
- W3C `traceparent` 只接受小写 `00-<32 hex trace-id>-<16 hex parent-id>-<00|01>`；两个 ID
  均不能全零。`traceparent | null` 不允许空字符串。

### 4.3 Digest

每个模型的 digest body 是该模型完整 `to_dict()`，包含 `schemaVersion` 和全部 nested model。
Domain 中 `<model>` 是本文标题给出的 exact ASCII 模型名，例如 `IMActionIntentV1`：

```text
SHA-256(
  UTF-8("quantum-entanglement.native-im/<model>/1\n")
  || canonical-json-body
).hexdigest()
```

Canonical JSON 固定 UTF-8、NFC、Unicode code point key sort、`,`/`:` 分隔且无额外空白、
`ensure_ascii=false`、`allow_nan=false`、JSON integer 十进制最短形式。只转义 `"`、`\\` 和 JSON
强制控制字符；HT/LF 分别写作 `\\t`/`\\n`，不转义 `/`，非 ASCII 直接编码为 UTF-8。Raw JSON
decoder 必须通过 pair-preserving parse 拒绝重复 key。Digest 字段一律是无 `sha256:` 前缀的 64 位
小写 hex。Parser 必须先完成 exact typed decode，再重新编码和计算 digest；不得对未经验证的
arbitrary JSON 直接做可信 digest。

`idempotencyKey` 使用第 7.3 节的独立固定 domain，不使用任一模型 digest 代替。

## 5. 公共值模型

### 5.1 `IMConversationRefV1`

```text
schemaVersion             integer = 1
tenantId                  ID
workspaceId               ID
provider                  ID
channelId                 ID
conversationId            ID
threadId | null           ID | null
```

`threadId=null` 表示 conversation 根，不允许用空字符串表达缺失。

### 5.2 `IMParticipantRefV1`

```text
schemaVersion             integer = 1
tenantId                  ID
workspaceId               ID
provider                  ID
channelId                 ID
participantId             ID
participantKind           human | agent | service
displayName | null        display text | null
roleIds[]                 ordered unique ID
membershipRevision        revision ID
```

`displayName` 只用于展示；授权只使用可信身份映射、role IDs 和 membership revision。Sender、mention
或模型输出中的 role 不构成授权证明。

### 5.3 `IMAttachmentRefV1`

```text
schemaVersion             integer = 1
tenantId                  ID
workspaceId               ID
provider                  ID
channelId                 ID
attachmentId              ID
version                   revision ID
mediaType                 canonical lowercase type/subtype
byteSize                  non-negative signed 64-bit integer
sha256                    64 lowercase hex
immutableRef              opaque ID
```

`immutableRef` 是 provider-owned opaque reference，不是 URL。临时下载 URL、cookie、header 或
credential 不得进入模型、event、Artifact、日志和 receipt。零字节对象合法。

`mediaType` 只接受无 parameter 的 lowercase ASCII：每侧匹配
`[a-z0-9][a-z0-9!#$&^_.+*-]{0,126}`，中间一个 `/`，总 UTF-8 不超过 255 bytes。

### 5.4 `IMMessageSegmentV1`

```text
schemaVersion             integer = 1
kind                      text | mention
text | null               message text | null
participantId | null      ID | null
```

- `text` segment：`text` 必填且非空，`participantId=null`；
- `mention` segment：`participantId` 必填，`text=null`；
- 相邻 `text` segment 非 canonical，必须合并后再编码；
- mention 的位置和重复次数由 segment 顺序表达，不用排序后 ID 集合丢失位置信息。

### 5.5 `IMMessageContentV1`

```text
schemaVersion             integer = 1
segments[]                ordered IMMessageSegmentV1
attachments[]             ordered IMAttachmentRefV1
```

两者不能同时为空。Segment 和附件顺序都有业务意义并进入 digest。Mention ID 只允许在 enclosing
conversation scope 内解析，attachment scope 必须与该 conversation 精确一致；成员存在性在
action-time authorization 边界检查，而不是由 codec 猜测。

### 5.6 `IMMessageRefV1`

```text
schemaVersion             integer = 1
conversation              IMConversationRefV1
messageId                 ID
revision                  revision ID
createdAt                 canonical timestamp
```

edit/delete 必须携带当前已知 revision。Provider 是否提供真正 CAS 由对应 operation capability 冻结；
`provider_best_effort` 不得在日志或 UI 中冒充 atomic CAS。

### 5.7 `IMReactionRefV1`

```text
schemaVersion             integer = 1
tenantId                  ID
workspaceId               ID
provider                  ID
channelId                 ID
reactionKey               opaque ID
```

`reactionKey` 是 provider profile 版本化映射后的 canonical key；Prompt 不得生成未在 profile 中
allowlist 的 provider-specific token。

### 5.8 `IMMembershipChangeV1`

```text
schemaVersion             integer = 1
subject                   IMParticipantRefV1
changeKind                joined | left | role_changed | suspended | restored
previousMembershipRevision | null
                           revision ID | null
```

`joined` 必须令 previous revision 为 null；其他 change kind 必须携带与当前
`subject.membershipRevision` 不同的 previous revision。该 payload 表达当前可信成员快照，不是授权
缓存；任何 outbound Action 仍重新获取/验证 action-time membership revision。

### 5.9 `InboundIMEventV1`

```text
schemaVersion             integer = 1
eventId                   ID
eventType                 message.created | message.edited | message.deleted |
                          reaction.added | reaction.removed | membership.changed
cursor                    opaque ID
sequenceNumber            non-negative signed 64-bit integer
conversation              IMConversationRefV1
message | null            IMMessageRefV1 | null
sender | null             IMParticipantRefV1 | null
content | null            IMMessageContentV1 | null
reaction | null           IMReactionRefV1 | null
membershipChange | null   IMMembershipChangeV1 | null
occurredAt                canonical timestamp
firstReceivedAt           canonical timestamp
ingressRequestId          ID
correlationId             ID
causationId | null        ID | null
transportEvidenceDigest  64 lowercase hex
```

字段组合必须精确满足：

| eventType | message | sender | content | reaction | membershipChange |
|---|---|---|---|---|---|
| `message.created` | required | required | required | null | null |
| `message.edited` | required | required | required | null | null |
| `message.deleted` | required | optional | null | null | null |
| `reaction.added` | required | required | null | required | null |
| `reaction.removed` | required | required | null | required | null |
| `membership.changed` | null | optional | null | null | required |

所有 nested scope 必须等于 event conversation scope；message conversation 必须与 event
conversation 完全相等。`firstReceivedAt` 和 `ingressRequestId` 是 receiver **首次**观察值；同一
event 的重送不得改写它们或 event digest。

该模型是 canonical parsed transport data，本身不证明签名已认证。它只有进入下一节的 verified
envelope 并完成本地 digest-bound inbox admission 后，才能触发路由或 Agent。

### 5.10 `IMVerifiedInboundEnvelopeV1`

```text
schemaVersion                 integer = 1
event                         InboundIMEventV1
eventDigest                   digest(InboundIMEventV1)
verificationId                ID
verifierId                    configured verifier ID
authenticationEvidenceDigest 64 lowercase hex
tenantMappingRevision         revision ID
verifiedAt                    canonical timestamp
traceparent | null            canonical W3C traceparent | null
```

只有 admission composition root 在验证 provider signature、timestamp、nonce、replay window、
endpoint binding、tenant/channel mapping 和 trusted connector identity 后才能构造该 envelope。
`authenticationEvidenceDigest` 只绑定清洗后的验证证据，不保存签名原文或 secret。

本地 inbox 必须持久化 `(scope, eventId, eventDigest, verificationId)`。同一 event ID + 相同 digest
是幂等重送；同一 event ID + 不同 digest 必须失败关闭并产生安全审计，不能返回旧事件后继续执行。

### 5.11 `IMCapabilityRequestV1`

```text
schemaVersion             integer = 1
tenantId                  ID
workspaceId               ID
provider                  ID
channelId                 ID
requestId                 ID
```

### 5.12 `IMAcceptanceLookupCapabilityV1`

```text
schemaVersion             integer = 1
lookupMode                idempotency_key | provider_operation_id
negativeAcceptanceMode    authoritative_terminal | unavailable
retentionSeconds          positive signed 64-bit integer
consistencySeconds        non-negative signed 64-bit integer
```

`consistencySeconds < retentionSeconds`。`authoritative_terminal` 表示在 consistency window 后、retention
到期前，该 exact lookup mode 的终态 negative evidence 可以证明未接受；`unavailable` 表示 not-found
永远不能升级为 negative finality。不同 lookup mode 的保证不能互相借用。这两个窗口是 receiver
profile 的保证；平台不得仅凭本地 `requestedAt` 或本地时钟流逝合成 negative evidence。

### 5.13 `IMOperationCapabilityV1`

```text
schemaVersion             integer = 1
operation                 send_message | edit_message | delete_message |
                          add_reaction | remove_reaction
revisionMode              not_applicable | required_cas | provider_best_effort
idempotencyMode           receiver_deduplicated | not_supported
acceptanceLookups[]       ordered unique IMAcceptanceLookupCapabilityV1
```

- 只为 enabled operation 建条目，按 operation 的 UTF-8 bytes 排序且不重复；
- send/reaction 的 `revisionMode=not_applicable`；edit/delete 不能使用 `not_applicable`；
- `required_cas` 表示 receiver 原子比较 target revision；`provider_best_effort` 必须由 policy 单独
  允许，且不能提供 CAS 成功声明；
- `receiver_deduplicated` 表示 receiver ledger 在 retention 内绑定本合同的 idempotency key；
- lookup entry 的 `authoritative_terminal` 才允许该 exact mode 产生 `reconciled_rejected`；暂时
  not-found、最终一致性窗口未结束和 retention 过期都不构成 negative evidence。

组合约束：

- `acceptanceLookups` 含 `idempotency_key` 时，`idempotencyMode` 必须为
  `receiver_deduplicated`；
- `idempotencyMode=not_supported` 时，key 仍进入平台审计和 request，但 receiver 去重不能作为保证；
- lookup entries 按 `lookupMode` UTF-8 bytes 排序且 mode 不重复；
- `provider_operation_id` lookup 不要求 receiver idempotency，但只在 operation ID 已知时可用。

### 5.14 `IMCapabilitySnapshotV1`

```text
schemaVersion                     integer = 1
tenantId                          ID
workspaceId                       ID
provider                          ID
channelId                         ID
revision                          revision ID
observedAt                        canonical timestamp
operations[]                     ordered unique IMOperationCapabilityV1
idempotencyRetentionSeconds|null positive integer | null
supportsThreads                   boolean
supportsMentions                  boolean
supportsAttachments               boolean
supportsMembershipEvents          boolean
maxTextBytes                      non-negative integer
maxAttachments                    non-negative integer
maxAttachmentBytes                non-negative signed 64-bit integer
```

如果任一 operation 为 `receiver_deduplicated`，idempotency retention 必须为正数且覆盖平台最大
自动重试窗口；否则它必须为 null。Acceptance retention/consistency 只从所选 operation + lookup
entry 读取。该 entry 没有权威 negative mode 时，query 的 not-found 永远保持 unknown。

Capability 由已配置 adapter/provider probe 产生并版本化，不能由模型名称、Prompt 或 UI checkbox
推断。Command 同时绑定 capability revision 和 digest，不能只信任可复用的 revision string。

## 6. 入站分页与恢复

### 6.1 `IMInboundReadRequestV1`

```text
schemaVersion             integer = 1
tenantId                  ID
workspaceId               ID
provider                  ID
channelId                 ID
afterCursor | null        opaque ID | null
afterSequence | null      non-negative integer | null
snapshotToken | null      opaque ID | null
limit                     integer, 1..1000
readRequestId             ID
```

`afterCursor` 与 `afterSequence` 必须同时为 null 或同时非 null。开始一个新 snapshot 时
`snapshotToken=null`，可以携带上次稳定 resume pair；继续同一分页 snapshot 时必须回传上一页的
snapshot token 和 next pair。

### 6.2 `IMInboundPageV1`

```text
schemaVersion             integer = 1
tenantId                  ID
workspaceId               ID
provider                  ID
channelId                 ID
readRequestId             ID
readRequestDigest         digest(IMInboundReadRequestV1)
snapshotToken             opaque ID
envelopes[]               ordered IMVerifiedInboundEnvelopeV1
nextCursor | null         opaque ID | null
nextSequence | null       non-negative integer | null
hasMore                   boolean
capabilityRevision        revision ID
capabilityDigest          digest(IMCapabilitySnapshotV1)
```

要求：

- response `readRequestId`、`readRequestDigest` 和四个 scope 字段必须等于完整 request；每个 envelope
  也必须等于 page scope；
- cursor 是 opaque resume token，调用方不得做 lexical/numeric 比较；顺序只比较 `sequenceNumber`；
- page 内 sequence 严格递增、event ID 无重复，并且首项大于 request `afterSequence`（若存在）；
- envelopes 非空时，next pair 必须精确等于最后一个 event 的 `(cursor, sequenceNumber)`；
- `hasMore=true` 时 envelopes 必须非空，且该 exact next pair 不得等于 request after pair；
- `hasMore=false` 且 envelopes 为空时，next pair 必须等于 request after pair；若初始 request 也没有
  after pair，则 next pair 同时为 null；
- page 的 canonical bytes 先受总上限约束，因此可能在达到 request limit 前截断；
- 断线恢复可以重送 transport event，但 durable inbox 只接受同 digest 的幂等重送；
- adapter 不得用进程内 offset 冒充 provider/native-IM durable cursor 或 sequence；
- fake 的 out-of-order 模式是显式故障注入，用于证明 consumer 拒绝违规 page，不是合规输出。

## 7. Outbound Action 合同

### 7.1 平台状态机

```text
proposed
  -> authorized
  -> queued
  -> dispatching
  -> succeeded | rejected | retry_wait | effect_unknown

retry_wait
  -> queued | needs_you

effect_unknown
  -> reconciling | needs_you

reconciling
  -> reconciled_succeeded | reconciled_rejected | effect_unknown
```

不允许 `effect_unknown -> queued/dispatching`。`retry_wait -> queued` 只接受 receiver 已证明未产生
effect 的 `retryable_not_accepted` receipt，并受最大次数、deadline、retention 和 policy 限制。每次
retry 保留同一 `actionId`、`intentDigest`、`idempotencyKey` 和 effect 语义。

### 7.2 `IMActionIntentV1`

```text
schemaVersion             integer = 1
actionId                  ID
tenantId                  ID
workspaceId               ID
actorId                   platform actor ID
delegatorId | null        platform actor ID | null
conversation              IMConversationRefV1
operation                 send_message | edit_message | delete_message |
                          add_reaction | remove_reaction
targetMessage | null      IMMessageRefV1 | null
content | null            IMMessageContentV1 | null
reaction | null           IMReactionRefV1 | null
createdAt                 canonical timestamp
correlationId             ID
causationId               ID
traceparent | null        canonical W3C traceparent | null
```

Exact operation matrix：

| operation | targetMessage | content | reaction |
|---|---|---|---|
| `send_message` | null | required | null |
| `edit_message` | required | required | null |
| `delete_message` | required | null | null |
| `add_reaction` | required | null | required |
| `remove_reaction` | required | null | required |

Target message conversation 和 reaction scope 必须等于 intent conversation。Intent 只是待授权意图，
不得触发 connector；operation capability、target revision、成员、附件和内容上限在 action-time
authorization 再验证。

### 7.3 幂等键与 immutable effect 绑定

`idempotencyKey` 是以下 exact plain dict 的 canonical JSON digest：

```json
{
  "actionId": "<actionId>",
  "channelId": "<channelId>",
  "provider": "<provider>",
  "tenantId": "<tenantId>",
  "workspaceId": "<workspaceId>"
}
```

```text
SHA-256(
  UTF-8("quantum-entanglement.native-im/idempotency-key/1\n")
  || canonical-json-body
).hexdigest()
```

平台 action store 始终持久化以下 immutable binding；当 operation capability 为
`receiver_deduplicated` 时，receiver ledger 也必须执行同一绑定：

```text
(scope, actionId, idempotencyKey) -> intentDigest + accepted effect evidence
```

同一 action 重新授权或重建 command 时，key 和 intent digest 必须不变；command ID/digest 可以变化。
同一 action/key 配不同 intent digest，或同一 key 配不同 action/scope，必须以 collision 失败关闭。
当 idempotency mode 为 `not_supported` 时，key 仍发送并用于本地冲突审计，但不能声称 receiver 会
去重；任一 unknown 后仍禁止自动 re-dispatch。Fake adapter 固定使用 `receiver_deduplicated`，并证明
同一绑定不产生第二个 accepted effect。

### 7.4 `IMActionCommandV1`

```text
schemaVersion             integer = 1
commandId                 ID
intent                    IMActionIntentV1
intentDigest              digest(IMActionIntentV1)
idempotencyKey            64 lowercase hex from 7.3
authorizationDecisionId   ID
authorizationRevision     revision ID
approvalDecisionId|null   ID | null
approvalRevision|null     revision ID | null
policyRevision            revision ID
capabilityRevision        revision ID
capabilityDigest          digest(IMCapabilitySnapshotV1)
authorizedAt              canonical timestamp
expiresAt                 canonical timestamp
correlationId             ID
causationId               ID
traceparent | null        canonical W3C traceparent | null
```

Command 必须由平台 action-time authorization boundary 构造并持久化。Adapter 只接受 store-owned
command snapshot，不接受裸 Intent、LLM tool args 或 caller dict。

- `authorizedAt < expiresAt`；dispatcher 必须在任何可能产生副作用的调用**之前**检查当前时间严格
  小于 expiresAt；过期 command 禁止 dispatch/re-dispatch，但允许对已有 unknown 做 acceptance query；
- approval ID/revision 必须同时为 null 或同时非 null；
- command 的 intent digest、幂等键、scope 和 trace context 必须按第 9 节绑定；
- command `correlationId`/`traceparent` 等于 intent，`causationId` 必须等于 `intent.actionId`；
- capability snapshot 必须从可信 store 以 revision + digest 精确回读，不能由 adapter 自报替代。

### 7.5 `IMDispatchRequestV1`

```text
schemaVersion             integer = 1
dispatchAttemptId         ID
command                   IMActionCommandV1
commandDigest             digest(IMActionCommandV1)
attemptNumber             positive signed 64-bit integer
fenceId                   durable non-secret fence ID
fenceRevision             revision ID
claimedAt                 canonical timestamp
dispatchDeadlineAt        canonical timestamp
correlationId             ID
causationId               ID
traceparent | null        canonical W3C traceparent | null
```

Attempt identity 由 durable fenced dispatcher 分配，adapter 不得猜测、用进程内 counter 生成或从
command ID 推导。对同一 action，attempt number 从 1 严格递增且不复用；attempt ID 全局唯一。

- `claimedAt < dispatchDeadlineAt <= command.expiresAt`；
- request 的 correlation/traceparent 等于 command，`causationId=commandId`；
- dispatcher 在调用 port 前重新验证 fence 仍为 owner、revision 未变且 command 未过期；
- fence ID/revision 是非秘密的本地 authority reference，不发送给真实 provider；adapter 只把它用于
  将返回值绑定原始 request；
- retry 会创建新 dispatch request，但 command effect、action ID、intent digest 和 idempotency key
  保持不变。

### 7.6 `IMActionReceiptV1`

```text
schemaVersion             integer = 1
receiptId                 ID
tenantId                  ID
workspaceId               ID
provider                  ID
channelId                 ID
actionId                  ID
commandId                 ID
dispatchAttemptId         ID
dispatchRequestDigest     digest(IMDispatchRequestV1)
intentDigest              64 lowercase hex
commandDigest             digest(IMActionCommandV1)
idempotencyKey            64 lowercase hex
attemptNumber             positive signed 64-bit integer
state                     succeeded | rejected | retryable_not_accepted |
                          effect_unknown | reconciled_succeeded |
                          reconciled_rejected
providerOperationId|null  ID | null
providerMessage | null    IMMessageRefV1 | null
receiverEvidenceDigest|null
                          64 lowercase hex | null
errorCode | null          core error enum | null
retryAfterSeconds | null  positive signed 64-bit integer | null
observedAt                canonical timestamp
correlationId             ID
causationId               ID
traceparent | null        canonical W3C traceparent | null
```

V1 core error enum：

```text
rate_limited_not_accepted
temporarily_unavailable_not_accepted
terminal_permission_denied
terminal_invalid_target
terminal_revision_conflict
terminal_unsupported
terminal_not_accepted
delivery_outcome_unknown
acceptance_not_final
acceptance_retention_expired
```

Provider 错误必须经 version-pinned profile 映射到该 allowlist；provider exception、traceback 和自由
文本错误不进入 receipt。

| state | receiver evidence | providerOperationId | providerMessage | errorCode | retryAfterSeconds |
|---|---|---|---|---|---|
| `succeeded` | required | 与 message 至少一个非 null | 与 operation 至少一个非 null | null | null |
| `rejected` | required | optional | null | terminal code required | null |
| `retryable_not_accepted` | required 的权威未接受证明 | null | null | transient not-accepted code required | optional；rate limit 时 required |
| `effect_unknown` | optional partial evidence | optional | null | unknown/not-final/retention code required | null |
| `reconciled_succeeded` | required | 与 message 至少一个非 null | 与 operation 至少一个非 null | null | null |
| `reconciled_rejected` | required 的权威终态未接受证明 | optional | null | `terminal_not_accepted` 或 terminal code | null |

`attemptNumber` 始终指向引发该结果的 dispatch attempt；对同一 unknown 的多次 query 不增加 dispatch
attempt。Dispatch receipt 的 `causationId=dispatchAttemptId`；acceptance-query receipt 的
`causationId=queryId`。两类 receipt 都沿用 command 的 correlation 和 traceparent。Receipt 是
receiver result data，不是 action-time authorization token，也不能单独作为 command lookup 的替代品。

### 7.7 `IMDispatchUnknownObservationV1`

```text
schemaVersion             integer = 1
observationId             ID
dispatchRequest           IMDispatchRequestV1
dispatchRequestDigest     digest(IMDispatchRequestV1)
reason                    dispatch_timeout | dispatch_cancelled |
                          connector_exception | dispatcher_recovery |
                          process_crash_recovery
observedAt                canonical timestamp
correlationId             ID
causationId               ID
traceparent | null        canonical W3C traceparent | null
```

这是平台本地的 durable uncertainty observation，不是 receiver receipt。它只在 port 没有返回
receipt、但 attempt 已进入“可能产生 effect”的边界时构造；`causationId=dispatchAttemptId`，其余 trace
绑定 dispatch request。Recovery 只能从 durable in-flight request/fence 构造，不能凭内存猜测。

### 7.8 `IMAcceptanceQueryV1`

```text
schemaVersion             integer = 1
queryId                   ID
unknownSourceType         action_receipt | dispatch_unknown_observation
unknownSourceId           ID
tenantId                  ID
workspaceId               ID
provider                  ID
channelId                 ID
actionId                  ID
commandId                 ID
dispatchAttemptId         ID
dispatchRequestDigest     digest(IMDispatchRequestV1)
intentDigest              64 lowercase hex
commandDigest             64 lowercase hex
idempotencyKey            64 lowercase hex
attemptNumber             positive signed 64-bit integer
lookupMode                idempotency_key | provider_operation_id
providerOperationId|null  ID | null
requestedAt               canonical timestamp
correlationId             ID
causationId               ID
traceparent | null        canonical W3C traceparent | null
```

Query 只能由 durable `effect_unknown` state，以及原始 `effect_unknown` action receipt 或
`IMDispatchUnknownObservationV1` 构造；普通 retry 不能伪造 query。Source type 决定 source ID 的 exact
store lookup，且 source 必须绑定同一 dispatch request。

Query 的 `correlationId`/`traceparent` 等于 command，`causationId=unknownSourceId`。`lookupMode` 必须
命中该 operation 的 exact lookup capability：选择 `idempotency_key` 时 provider operation ID 必须
为 null；选择 `provider_operation_id` 时它必须非 null。暂时 not-found、该 mode 的 consistency
window 未结束或 retention 已过期时只能返回 `effect_unknown`；只有同一 mode 的权威终态 negative
evidence 才能返回 `reconciled_rejected`。Retention 过期后平台进入 Needs You，仍不得自动重发。

## 8. `IMGatewayPort`

Python port 的冻结语义：

```python
class IMGatewayPort(Protocol):
    async def capability_snapshot(
        self, request: IMCapabilityRequestV1
    ) -> IMCapabilitySnapshotV1: ...

    async def read_inbound(
        self, request: IMInboundReadRequestV1
    ) -> IMInboundPageV1: ...

    async def dispatch(
        self, request: IMDispatchRequestV1
    ) -> IMActionReceiptV1: ...

    async def query_acceptance(
        self, query: IMAcceptanceQueryV1
    ) -> IMActionReceiptV1: ...
```

返回状态集合必须收窄：

- `dispatch()`：`succeeded | rejected | retryable_not_accepted | effect_unknown`；
- `query_acceptance()`：`reconciled_succeeded | reconciled_rejected | effect_unknown`；
- schema/binding/capability/expiry/fence 在调用前失败，不产生 provider receipt；
- 一旦进入可能产生 effect 的边界，取消、timeout、connector exception 或 process crash 都由调用方
  持久化为 `IMDispatchUnknownObservationV1`，不能用 Python exception 推断 receiver 未执行；
- adapter 返回 receipt 时，dispatch attempt ID/number/request digest 必须精确回显并通过 durable
  request lookup；stale/unknown fence 的返回值不能推进 action state。

Port 不暴露 SDK client、credential、HTTP session、websocket、raw webhook、数据库 connection 或
任意 callback。P0 fake 实现不导入或打开任何网络能力。

## 9. Cross-model binding matrix

以下相等关系在 decode 后、调用前和 receipt admission 时都必须验证：

| 来源 | 必须绑定 |
|---|---|
| request/page/envelope/event | 完整 scope、read request ID、capability revision + digest |
| verified event/chat admission | coordination correlation = event correlation；causation = event ID；traceparent 原样传播；原 event causation 留在 audit evidence |
| event/message/sender/attachment/reaction/membership | 完整 scope；message conversation exact equality |
| action intent/nested values | 完整 scope；target conversation exact equality |
| command/intent | `intentDigest`、scope、action；correlation/traceparent 相等；command causation = action ID |
| command/capability | stored snapshot 的 revision + digest + operation profile |
| command/idempotency | 第 7.3 节 exact derivation；重建 command 不得变化 |
| dispatch request/command | command digest、attempt/fence、deadline、correlation/traceparent；request causation = command ID |
| receipt/dispatch request | scope、action、command、attempt ID/number/request digest、intent/command digest、key、correlation/traceparent；causation 按 dispatch request/query |
| local unknown observation/dispatch request | 完整 request + digest、correlation/traceparent；causation = attempt ID |
| query/unknown source/dispatch request | source type/ID、scope、全部 ID/digest/key、attempt、lookup capability、correlation/traceparent；query causation = source ID |
| providerMessage/intent | provider message conversation 与 intent conversation exact equality |

任何 drift 都失败关闭，不能用“以一边为准”修正。用于 inbox 的 consumer identity 必须机械包含
tenant/workspace/provider/channel；用于 receiver ledger 的 immutable effect digest 是 intent digest，
不是会随重新授权变化的 command digest。

## 10. Provider profile 必须提供的映射

独立 IM 后端接入时另建 version-pinned provider profile，至少回答：

1. 每个 canonical ID、reaction key 和 membership revision 映射到哪个 provider 字段；
2. event 去重键、durable cursor、sequence 和 snapshot 的持久期、作用域、顺序保证；
3. 签名、timestamp、nonce、重放窗口、endpoint binding 及 key rotation；
4. 每个 operation 的 CAS、幂等、acceptance lookup 和 negative finality；
5. action/idempotency key 是否原样接收，保存多久，collision 如何返回；
6. provider operation/message receipt 的字段、验真主体、trusted connector identity 和证据留存位置；
7. acceptance query 的 lookup key、consistency window、终态和 retention；
8. 429/5xx/timeout/NACK 的 no-effect、retryable、unknown 和 terminal 分类证据；
9. thread、mention segment、附件、edit/delete/reaction 的能力差异；
10. service identity、tenant/channel scope 和 action-time membership inputs；
11. sandbox 与 production endpoint 如何机械区分；
12. transport trace 如何映射到平台 correlation/causation/traceparent。

未知项必须标为 `unsupported` 或 `unverified`，不能由 adapter 猜默认值。缺少 authoritative negative
finality 时，not-found 永不解释为“未发送”。

## 11. Fake Adapter 与 P0 零网络边界

P0 fake adapter 必须：

- provider 固定为 `qe.fake-im.v1`，scope ID 必须使用保留的 `test-` 前缀；
- 普通构造参数只接受固定 scope、capability snapshot、预装 immutable envelope sequence、注入时钟
  和 deterministic fault script；API 中不存在 endpoint、URL、token、secret 或通用 HTTP callback；
- 普通构造永远 outbound disabled；唯一可启用测试 effect 的路径是 adapter-specific
  `FakeIMAdapter.for_test(..., outbound_permit=FakeIMTestOutboundPermit)` factory；
- `FakeIMTestOutboundPermit` 只能由 test fixture 在进程内创建，以 object identity 校验，不可序列化、
  不进入 wire model/Port/env/config/repr；没有 permit 或 scope 非 `test-` 前缀时 `dispatch()` 失败关闭；
- 不读取环境 credential、不解析真实 endpoint、不打开 socket/DNS/HTTP/WebSocket、不调用模型；
- 正常 read output 遵守 sequence/page contract；duplicate、out-of-order 和 digest drift 可通过明确
  invalid-fault mode 注入，consumer 必须拒绝；
- receiver-side ledger 精确绑定 `(scope, actionId, idempotencyKey, intentDigest)`；
- duplicate dispatch 返回同一 accepted effect，不产生第二 effect；changed digest/key/scope 冲突拒绝；
- 429 只有在 ledger 证明未接受时返回 `retryable_not_accepted`；
- ACK loss 场景先记录 accepted effect，再返回 `effect_unknown`；query 随后返回同一 effect 的
  `reconciled_succeeded`；
- 支持 terminal reject、temporary NACK、timeout-after-effect-boundary-entry、crash-after-accept、unknown
  not-final、authoritative negative reconcile 和 retention expiry；
- repr/error/log 不包含消息正文、附件引用、签名 material 或 credential canary。

机械验收同时检查模块 import graph 和运行期 socket canary。P0 composition 只能注册 fake；任何真实
connector class、endpoint setting 或 credential setting 出现都使检查失败。

## 12. V1 大小与格式上限

第一版实现采用保守上限；任一扩大都需要 V2 或独立兼容性评审：

| 项目 | V1 上限 |
|---|---:|
| 单个 ID/revision/cursor/opaque ref UTF-8 | 4 KiB |
| display text UTF-8 | 16 KiB |
| 单个 text segment UTF-8 | 1 MiB |
| 单个 message content canonical bytes | 2 MiB |
| message segments | 4,096 |
| 单事件附件数量 | 64 |
| 单模型 role IDs | 1,024 |
| 入站 page envelopes | 1,000 |
| 单 event/envelope canonical bytes | 3 MiB |
| 单 Action Intent/Command/Dispatch Request/Unknown Observation canonical bytes | 3 MiB |
| 单 receipt/query canonical bytes | 256 KiB |
| 单 inbound page canonical bytes | 16 MiB |
| attachment byteSize/maxAttachmentBytes | signed 64-bit non-negative integer |
| JSON depth | exact typed schema 固定，不接受任意递归 payload |
| timestamp | canonical UTC 微秒 `YYYY-MM-DDTHH:MM:SS.ffffffZ` |

Capability 的更小上限始终覆盖平台上限。Encoder 在分配或深拷贝大型 nested value 前执行可行的
集合和字段预检，并在 canonical serialization 后再次执行顶层 byte limit。

## 13. P0 验收矩阵

- 全部引用类型闭合；每个 model exact schema/version/typing/unknown-field rejection；
- NFC 拒绝、surrogate、C0/DEL、LF/HT、长度、timestamp、signed-64-bit、bool-as-int；
- canonical bytes/digest 和第 7.3 节幂等键 golden vectors；
- 任一 scope/identity/revision/segment/attachment/action/capability/receipt 字段变化都改变对应 digest；
- cross-tenant/workspace/provider/channel binding drift 和 replay digest conflict 全拒绝；
- event type 和 operation 的 required/forbidden 组合矩阵全覆盖；
- mention segment 位置、重复 mention、相邻 text segment 非 canonical；
- page sequence、snapshot、duplicate ID、resume pair、hasMore、16 MiB 总上限；
- capability per-operation CAS/idempotency/query/negative-finality 组合；
- durable dispatch attempt/fence/deadline 与 receipt request-digest 回显；
- Receipt 六种 state 的 required/optional/forbidden field matrix；
- 429 只有权威未接受证明才可 bounded retry；timeout/cancel/crash-after-send 全 unknown；
- 无 receiver receipt 的 timeout/crash 使用本地 unknown observation，并可按 exact source 构造 query；
- `effect_unknown` 永不自动升级成功或重新 dispatch；not-found 不冒充 negative evidence；
- fake receiver effect idempotency、ACK-loss、query reconcile、collision 和 retention expiry；
- verified envelope 与 digest-bound inbox admission 先于 `MentionRouter`；
- generic `OutboxPublisher` 未被直接接到 IM port；
- credential/config canary 不进入 codec/repr/error/log；业务消息可进入 wire codec，但不进入日志；
- fake adapter import/runtime 无网络，默认 outbound disabled，无真实 endpoint、credential 或外部发送；
- Python 3.9 compile/import 与仓库全量测试通过。

## 14. 非目标、后续阶段与冻结条件

以下不属于 P0：

- 真实 IM SDK/HTTP/WebSocket/webhook；
- OIDC/SSO、mTLS 和生产 service identity；
- 全 repository tenant migration；
- Atomic Result Writer 和 heartbeat worker promotion；
- durable Action tables/migration/fenced dispatcher composition；
- 真实 provider security review、capacity、soak 和 SLO；
- 生产 send、真实用户、客户数据、飞书或企微 connector；
- PostgreSQL、HA/Kubernetes 和多地域。

它们分别在 IM-P1、P2、P3 和接入后 Gate A–E 推进，不能反向污染或绕过 V1 contract。

本文的文档冻结门禁如下，已在 2026-08-27 全部完成：

1. 独立协议、schema 和仓库边界审计无 unresolved blocking finding；
2. 文档术语、路径、diff 和 Markdown 检查通过；
3. 文档提交已推送 GitHub；
4. 私人 Notion 完整语义镜像、原始 Markdown 附件和远端 marker 回读完成。

冻结证据基线为 GitHub `main@a583a58fe43b20ae0d2372bbb8e032ea66f4c570`、三路独立审计无
blocking finding，以及 Notion 页面
`https://app.notion.com/p/3c9ead4b996e8114985cce2cc5af2b63` 的全文/表格/图/附件 marker
回读。此后的 V1 字段、enum、canonical 或状态语义变更必须按第 2.1 节升 V2；实现 bugfix 不能
借“兼容”名义改变冻结 wire contract。

`IM-P0 CONTRACT_READY` 还要求本文对应的值模型、strict codec、golden vectors、fake adapter 和全部
P0 contract tests 进入主线并完成相同的 GitHub + Notion 闭环。文档冻结不等于 P0 完成，更不
授权真实 IM 测试或任何消息写入。
