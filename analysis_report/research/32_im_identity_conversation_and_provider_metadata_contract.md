# IM 身份、会话拓扑与 Provider Metadata 合同实现证据

> 日期：2026-08-28（Asia/Shanghai）
>
> 分支：`dev_wanwork_quantum_entanglement`
>
> 一级研究根：`/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output/more`
>
> 代码范围：`apps/im-api/internal/im`、`apps/im-api/internal/immetadata`

## 1. 阶段结论

本批不是“在融云 `ext_info` 里塞几个 JSON 字段”，而是先冻结平台身份、会话拓扑与 provider
投影之间的信任边界。实现已经形成以下可执行合同：

1. 可见 Actor 的稳定引用与版本快照分离；snapshot revision 变化不会改变稳定 Actor；
2. `human | agent | system | service` 与 `usr_ | agt_ | sys_ | svc_` 前缀精确绑定，但前缀只证明
   语法，不证明数据库存在、安装有效、membership 或 authorization；
3. Clerk/融云 external subject 绑定 provider realm/app/environment，避免相同 subject 跨环境串号；
4. Conversation 的稳定引用与版本快照分离；`direct | group | agent_thread` 的 topology 字段互斥；
5. `agent_thread` 必须同时携带 parent conversation、root message 和 Agent invocation，parent 不得等于
   child；parent lineage 不产生 ACL 继承；
6. 融云用户/群 `ext_info` 是 1024-byte 上限的 canonical JSON string，只是显示和对账投影；
7. metadata 不允许 tenant、workspace、membership、ACL、role、delegation、credential、token、消息正文、
   Task、Artifact、Receipt 或外部地址；
8. decoder 对 unknown/duplicate/missing/null/wrong type/trailing value/non-canonical order/escape drift/
   非 NFC/控制字符/Unicode 混淆/超长输入 fail closed，返回零值和固定脱敏错误；
9. 用户和会话四种合法 shape 各有唯一 canonical bytes，合计 848 种非 canonical key permutation 被拒绝；
10. 普通、race、seeded fuzz、secret canary 和 Unicode 语料均已有 Go 测试。

这里的“实现”只指 W1 纯值与 codec 合同。它不等于 Clerk JWT 验证、融云真实 adapter、持久 mapping、
conversation membership/ACL、PostgreSQL、provider sandbox 兼容性或 production authorization 闭环。

## 2. 调研吸收方法

本批严格使用固定链：

```text
研究证据
  -> 产品硬需求
  -> 领域对象/API
  -> 安全控制
  -> 实施阶段
  -> 可复核验收证据
```

调研报告中的 `[F]`、`[C]`、`[A]`、`[U]` 口径原样保留。产品名、UI、字段或静态客户端能力不能
证明真实 provider、durability、isolation、authorization 或 SLA。源码实现也不能把分析建议改写成
已经验证的外部事实。

### 2.1 本批直接使用的一级证据

| 一级证据 | 本批吸收点 |
|---|---|
| `clawith/research_report.md:148-152,196-226` | Agent 是稳定对象；人与 Agent 统一 Participant；入群/发送前仍独立校验 tenant、membership、Agent status；mention intent 与 send 分离；Agent 用自己的身份回复 |
| `clawith/research_report.md:493-503,543-557,599-611` | handoff 冻结 source/root/parent/target/cutoff/idempotency；全局 identity 与 tenant membership 分离；IM provider 只是 adapter/projection |
| `floatim-floatboat/research_report.md:152-163,229-240` | Agent-native IM、群级触发规则可借鉴；静态客户端字段不能证明后端鉴权和 SLA |
| `codexloom/research_report.md:216-234` | 每个 Conversation 的 audience/purpose/role/speech/commitment/disclosure 边界独立；外部 membership 不自动开放内部 Thread、工具、凭据或 decision |
| `raft-slock/research_report.md:209-230,235-245,272-283,346-350` | stable Agent identity 与 runtime session 分离；private channel 按自身成员边界；跨边界 room 独立 policy；外部账户键必须包含 provider realm/server 范围 |
| `_portfolio/master_research_report.md:220-226,648-671` | chat/session 不是 Task 权威；Task、Thread、Attempt、Action、Acceptance 不能互相冒充 |
| `agentteams/research_report.md:113-123,192-207,250-282` | Room 与 Project/Task 分对象；成员拓扑必须显式，不由自然语言或父级隐式推断 |
| `tech-agent-security-governance/research_report.md:187-231,315,442-444` | human principal、Agent definition、workload、tool/resource 身份分离；mandate 与 secret broker 独立；metadata 不是授权边界 |
| `protocol-a2a/research_report.md:209,425-457,480` | Card/metadata 不是身份证或授权；extension smuggling、JSON bomb 需 allowlist/size/depth；出现 tenant 字段不等于 tenant isolation |
| `sandbase-harness/research_report.md:470` | Secret metadata/密文存在不代表执行授权或可被模型使用 |
| `holaos/research_report.md:599-607` | provider metadata 只能做投影，真实语义必须回查 canonical platform state |
| `agentspace/research_report.md:739-759` | adapter 必须声明并验证 provider capability，不能把 enum/file/config 存在当作外部能力已成立 |

### 2.2 证据到实现的硬映射

| 证据结论 | 产品硬需求 | 领域对象/API | 安全控制 | 阶段 | 验收证据 |
|---|---|---|---|---|---|
| 稳定 Actor 与 runtime/workload/delegation 分离 | 同一 Agent 重启/换 runtime 后可见身份不变 | `ActorRef`、`ActorSnapshot`；后续 `AgentRelease/Installation/RuntimeIncarnation/DelegationGrant` | prefix/type 只做 syntax；授权路径必须 resolve registry、membership、mandate | W1 值完成；W2/W4 authority 未完成 | 两个 revision 的 snapshot 不等，但 `Ref()` 相等；零 scope/type mismatch 拒绝 |
| 全局外部身份与平台/tenant membership 分离 | 相同 provider subject 在 prod/staging 或多个 app 不串号 | `ProviderRealmID`、`ExternalIdentityRef(provider, realm, subject)` | provider ref 不授予 tenant membership；realm 非 secret；后续 binding 需 CAS/status/revision | W1 ref 完成；W2/W3 binding 未完成 | 相同 provider+subject、不同 realm 得到不同 ref；missing realm 拒绝 |
| Human/Agent 共享可见 Participant，但 system/service 不作为普通聊天用户 | 融云普通 user 投影只允许 human/agent | `UserProjection` | Actor prefix 与 subject type 对齐；human 禁 Agent 字段；agent 必须 definition+SemVer | W1 完成；W3 provisioning 未完成 | human/agent positive；system/service、prefix drift、字段缺失/多余拒绝 |
| Conversation 是协作空间，不是 Task/ACL | topology 与业务/授权状态分域 | `ConversationRef`、`ConversationSnapshot` | stable ref 与 revision 分离；普通会话禁止 thread 字段 | W1 值完成；W2 aggregate/membership 未完成 | direct/group/thread 正负矩阵；普通会话伪造 topology 拒绝 |
| Agent thread 是 parent/root/invocation lineage | `@Agent` 子群具备确定性关联键 | `ConversationAgentThread`、parent/root/invocation values | 三字段 all-or-none；self-parent 拒绝；lineage 不继承 ACL | W1 值完成；W4 create-or-get 未完成 | partial topology、自指、错误 prefix、Unicode/控制字符拒绝 |
| extension smuggling/JSON bomb 必须 fail closed | `ext_info` 只能是最小有界投影 | `immetadata` 专用 encode/decode | 1024 bytes、flat allowlist、exact types、canonical roundtrip、fixed error | W1 codec 完成；provider limit readback W3 未完成 | unknown/duplicate/null/wrong type/trailing/oversize/canonical permutation 全拒绝 |
| metadata 不是授权边界 | provider payload 自报 actor/thread 不能改变权限 | `UserProjection`、`ConversationProjection` 注释与 forbidden field schema | 永久禁止 authority/secret/content/evidence 字段；入站后必须回查 binding+membership | W1 schema 完成；resolver/admission W2/W3 未完成 | 44 类 forbidden field 对 user/group 全拒绝，canary 不进入 error |
| canonical codec 不证明 provider 兼容 | 未经融云 sandbox readback 不开放真实 outbound | 后续 `ProviderCharacteristics/CapabilityMatrix` | provider 实际 limit、原样保存、稳定回传、签名、幂等、readback 全部分项验证 | W3 | 当前明确 `unverified`；sandbox fixture 通过前真实 outbound 保持关闭 |

## 3. 平台身份合同

### 3.1 ID 命名空间

```text
tenant               ten_
workspace            wsp_
provider realm       rlm_
human actor          usr_
agent actor          agt_
system actor         sys_
service actor        svc_
agent definition     agd_
conversation         cnv_
message              msg_
invocation           inv_
```

所有平台 ID：

- 最长 128 bytes；
- 只允许受限 ASCII `A-Z a-z 0-9 _ -`；
- suffix 首尾必须是字母或数字；
- 拒绝空 suffix、错误 prefix、空白、C0/DEL、尾部分隔符、全角/Cyrillic 等 Unicode 混淆与超长值；
- ID 可比较但不自证数据库存在、唯一归属、status、membership、installation 或 authority。

### 3.2 `ActorRef` 与 `ActorSnapshot`

```text
ActorRef
  tenantId
  actorId

ActorSnapshot
  ActorRef
  subjectType
  revision
```

拆分原因：如果把 revision 放进“稳定身份”本身，同一 Actor 的 revision 1 与 revision 2 会在 map key、
dedupe、audit join 或 equality 中变成两个不同身份。现在 `ActorRef` 表示稳定业务引用，`ActorSnapshot`
表示该引用的一个不可变版本。

`ActorSnapshot` 的 prefix/type 校验仍只是语法层。合法 `agt_finance` 不能证明 caller 可以作为该 Agent
发言；合法 `sys_`/`svc_` 不能证明平台 ownership 或 workload identity。授权路径必须另行解析：

```text
authenticated external principal
  -> realm-scoped persisted binding
  -> stable ActorRef
  -> active tenant membership
  -> current ActorSnapshot/status
  -> Agent installation/release（Agent only）
  -> conversation membership + mandate
  -> action-time policy
```

### 3.3 外部身份 realm 隔离

```text
ExternalIdentityRef
  provider = clerk | rongcloud
  providerRealmId = rlm_...
  subjectId
```

Clerk subject 必须为 `user_...`。融云 user subject 当前要求直接映射平台 Actor ID。realm 用于区分
staging/production、不同 Clerk instance 或不同融云 app；它不包含 key、endpoint 或 tenant 权限。

后续 W2/W3 还必须实现 `ExternalIdentityBinding` 的唯一性、status、revision、link/unlink/retarget CAS、
provider callback 验证和 offboard/revoke。当前 ref 不能证明 provider ownership 或 callback authenticity。

### 3.4 Agent SemVer 的诚实边界

`AgentVersion` 只校验严格 SemVer syntax，是 display/compatibility label。它不是：

- immutable release ID；
- artifact/config digest；
- publisher signature；
- tenant installation approval；
- 当前 runtime authority。

W2 必须以 `AgentReleaseID + artifact/config digest` 冻结真正不可变的执行版本；历史 Run/Message/Artifact
pin 该 release。重复使用同一个 SemVer 上传不同 bytes 必须被 registry 拒绝。

## 4. Conversation 合同

### 4.1 `ConversationRef` 与 `ConversationSnapshot`

```text
ConversationRef
  tenantId
  conversationId

ConversationSnapshot
  ConversationRef
  workspaceId?      # nil 与 zero value 精确区分
  type              # direct | group | agent_thread
  parentConversationId?
  rootMessageId?
  agentInvocationId?
  revision
```

与 Actor 相同，稳定 Conversation 引用不含 revision。snapshot revision 变化不改变 Ref。

### 4.2 topology 互斥

| 类型 | parent | root message | invocation | 当前 W1 规则 |
|---|---|---|---|---|
| `direct` | 禁止 | 禁止 | 禁止 | tenant/workspace scope + revision 合法 |
| `group` | 禁止 | 禁止 | 禁止 | tenant/workspace scope + revision 合法 |
| `agent_thread` | 必填 | 必填 | 必填 | all-or-none；parent != self |

parent/root/invocation 只建立 lineage，不证明：

- root message 确实属于 parent；
- parent 与 child 在同一 workspace；
- invocation 指向 active Agent installation；
- 父群成员可读写子群；
- Conversation 已创建 Task；
- provider group 已成功创建或可稳定回读。

这些跨聚合事实需要 W2 repository/transaction 和 W4 use case 校验。子群必须创建独立 membership/ACL，
explicit initial members 之外的父群成员默认无权访问；Artifact 回父群也必须是独立授权的 publish-ref。

### 4.3 与业务状态分域

```text
Conversation ready  != BusinessTask running
Invocation succeeded != Artifact accepted
Message delivered    != Action succeeded
Provider HTTP 200     != business accepted
Parent archived       != Task canceled
```

本批不在 Conversation value 中加入 Task/Attempt/Action/Acceptance 字段，防止 transport 状态推进业务
权威状态。

## 5. `ext_info` V1 canonical schema

### 5.1 Human user

```json
{"platformActorId":"usr_alice","schemaVersion":1,"subjectType":"human"}
```

### 5.2 Agent user

```json
{"agentDefinitionId":"agd_finance","agentVersion":"1.0.0","platformActorId":"agt_finance","schemaVersion":1,"subjectType":"agent"}
```

### 5.3 Ordinary group

```json
{"conversationType":"group","platformConversationId":"cnv_product","schemaVersion":1}
```

### 5.4 Agent thread group

```json
{"agentInvocationId":"inv_finance","conversationType":"agent_thread","parentConversationId":"cnv_parent","platformConversationId":"cnv_thread","rootMessageId":"msg_root","schemaVersion":1}
```

用户投影只允许 `human | agent`。`system | service` 是平台内部主体，V1 不注册为融云普通聊天用户。
群投影只允许 `group | agent_thread`；direct conversation 不使用 group `ext_info`。

### 5.5 canonical bytes 规则

实现采用 RFC 8785/JCS 思路的受限平面子集：

1. 输入最多 1024 UTF-8 bytes；真实 adapter 进一步使用 `min(1024, 已核实 provider limit)`；
2. 只接受单一 JSON object；V1 没有 array/nested object/自由 metadata；
3. key 是固定 allowlist，字段顺序按 key 字典序；
4. `schemaVersion` token 必须是 integer `1`，拒绝 string、`1.0`、`1e0`、其他版本；
5. 禁止 unknown key、alias、大小写漂移、snake_case、duplicate 和 escaped-equivalent duplicate；
6. 禁止 missing、显式空 optional、`null`、bool/number/object/array 类型漂移；
7. 整体 UTF-8 有效且 NFC；V1 字段最终还需通过 ASCII enum/ID/SemVer validator；
8. 禁止 BOM、前后空白、trailing garbage、第二个 JSON value 与截断输入；
9. 解码后重新编码，必须 byte-for-byte 等于输入；等价但重排、加空格或使用 `\uXXXX` 的输入拒绝；
10. 失败只返回零值和固定 sentinel error，不拼接 raw payload。

编码器的唯一目标是内部 canonical bytes。它尚未证明融云会接受、原样保存或稳定回传这些 bytes；
该证明属于 W3 sandbox provider contract test。

## 6. 永久禁止进入 provider metadata 的字段

| 分类 | 禁止示例 | 原因 |
|---|---|---|
| scope/authority | tenant、workspace、ACL、role、member IDs、permission、capability、policy、approval、delegation、mandate、scope | provider metadata 可伪造/漂移，不能建立平台 authority |
| runtime | session/run/attempt/workload/process/device/lease/fence | visible Actor 与 runtime identity 必须分离 |
| credential material | token、API key、password、cookie、Authorization、refresh token、credential、secret ref | 凭据只能在 trusted executor 通过 broker JIT 解析 |
| content/PII | message body、Prompt、email、phone、Memory | 防止 provider profile 泄漏和 prompt/日志扩散 |
| external address | callback、webhook、endpoint、永久 file URL | 防止 SSRF、credential forwarding 与失效链接变成权威引用 |
| business/evidence | Task、Action、Receipt、Artifact、Acceptance、Evidence、Checkpoint | provider transport 状态不能冒充业务状态或审计证据 |
| extension bag | metadata、extensions、extra | 防止 extension smuggling 绕过 V1 allowlist |

测试逐个注入 44 类字段到 user/group golden payload，全部返回固定脱敏错误；secret canary 不进入 error。

## 7. 解码与入站授权边界

### 7.1 当前 W1 codec

```text
raw ext_info string
  -> size <= 1024
  -> valid UTF-8 + NFC
  -> strict typed decode + unknown-field rejection
  -> domain ID/enum/SemVer validation
  -> canonical re-encode
  -> exact byte equality
  -> zero-authority projection value
```

### 7.2 W3 入站必须增加

```text
authenticated RongCloud callback/service identity
  -> durable provider event dedupe
  -> provider realm + authenticated user/group ID
  -> persisted platform binding lookup
  -> tenant + Actor/Conversation status and revision
  -> conversation membership/object authorization
  -> Agent installation/release/status（Agent only）
  -> ext_info projection consistency comparison
  -> mention/policy/budget/mandate admission
```

合法 canonical JSON 仍可能引用一个不存在、已撤销或被攻击者替换的 Actor/parent/root/invocation。
因此 decode 成功绝不能直接授予读、写、入群、执行、delegation 或 tool capability。

## 8. 测试与机器证据

### 8.1 测试面

当前两个包共有 29 个 `Test`/`Fuzz` 入口、2,148 行 Go code/test，覆盖：

- Actor/Conversation stable ref 与 snapshot revision 分离；
- subject/prefix、realm、scope、topology 互斥；
- ID/SemVer 正负语料；
- 四种 golden bytes 和双向 roundtrip；
- root type、BOM、whitespace、trailing、multiple value、truncated JSON；
- unknown、duplicate、escaped duplicate、missing、null、wrong type、schema numeric drift；
- Human 6 个 key order、Agent 120 个、Group 6 个、Agent Thread 720 个 key order；
- 44 类 authority/secret/content/address/evidence forbidden fields；
- NFD、NFC non-ASCII、fullwidth、Cyrillic、bidi、zero-width、C1、NUL、CR/LF/TAB、DEL、lone surrogate、
  invalid UTF-8；
- 128 goroutine × 100 iterations 的 deterministic encode/decode race fixture；
- user/conversation seeded fuzz：任何 accepted input 必须可 exact canonical re-encode，任何 rejected input
  必须返回 zero value 和固定 error。

### 8.2 已运行命令

```bash
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./apps/im-api/internal/im/... ./apps/im-api/internal/immetadata/... -count=1

GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test -race ./apps/im-api/internal/im/... ./apps/im-api/internal/immetadata/... -count=1

git diff --check
```

普通测试与 race 均通过。阶段末仍须运行 `./apps/im-api/...`、`go vet`、`go mod verify`、
`go mod tidy -diff` 和全量 Python gate；专题通过不代表全仓阶段门禁完成。

## 9. 小提交台账

| 提交 | 内容 |
|---|---|
| `9f55b33` | 初版 immutable IM identity values |
| `8acd6bd` | identity 正负合同测试 |
| `a15d7e2` | 初版 Conversation values |
| `66b5e80` | Conversation topology 合同测试 |
| `75c8ee7` | 受控 Actor subject classification |
| `eeb05db` | user/conversation provider projection values |
| `792cf03` | strict canonical provider metadata codec |
| `99778ac` | 根据一级调研拆分 `ActorRef/ActorSnapshot` 并增加 realm scope |
| `33ce779` | 根据一级调研拆分 `ConversationRef/ConversationSnapshot` |
| `3a1278e` | canonical key order 收敛为受限 JCS 风格 |
| `e543737` | user `ext_info` golden、负向与排列语料 |
| `528a19f` | group `ext_info` golden、负向与排列语料 |
| `ae9095f` | strict decoder seeded fuzz properties |
| `1f76925` | 128 路 race/determinism fixture |
| `bcfe064` | authority/secret/content/evidence field canary |
| `60ebf6a` | Unicode normalization/control/invalid UTF-8 语料 |

这些提交刻意保留初版、研究审阅后的修正与测试闭合过程，不 squash，使人工审阅能看到设计如何被
一级证据纠偏。

## 10. 尚未完成与下一阶段 gate

### W2：Domain + PostgreSQL

- `AgentDefinition -> immutable AgentRelease(digest) -> tenant AgentInstallation -> ActorRef`；
- `HumanPrincipal/WorkloadPrincipal/RuntimeIncarnation/DelegationGrant/ActingContext`；
- `ExternalIdentityBinding`/`ProviderConversationBinding` 的 realm、status、revision、unique、retarget CAS；
- Conversation aggregate/state、独立 Membership/ACL/RepresentationPolicy；
- root message belongs-to-parent、parent/root same tenant/workspace、cycle/depth/context cutoff；
- Task/Attempt/Action/Acceptance 与 Conversation 分立的持久状态和 transaction；
- provider mapping、inbox/outbox、event/projection checkpoint migrations 与 tenant isolation。

### W3：Clerk + RongCloud

- Clerk JWT signature/issuer/audience/exp/nbf/JWKS rotation；
- verified Clerk subject 后的平台 binding 和 membership lookup；
- RongCloud provider profile/capability matrix；
- callback/stream authentication、durable dedupe、reorder/resume；
- user/group provisioning fake 与 inbound-only sandbox readback；
- 实际 `ext_info` size limit、原样保存、稳定回传、unknown field/Unicode/provider transformation fixtures；
- provider ID/ext_info drift fail closed；
- outbound 仍关闭。

### W4：`@Agent` 子群闭环

- 单 Agent mention 确定性 admission，多 Agent 才进入受控 planning；
- create-or-get stable key `(tenant,parent,rootMessage,targetInstallation)`；
- child explicit membership，不继承 parent ACL；
- root/context cutoff、cycle/depth、duplicate/reorder/ACK-loss reconcile；
- Agent 用自己的 Actor 回复，progress/result 只进 child；
- parent 只出现受限 card，Artifact publish-ref 独立授权；
- Conversation ready、Task/Attempt/Artifact/Acceptance/Action finality 全部分域。

## 11. 禁止提前宣称

本批之后仍然禁止声称：

- `ExternalIdentityRef` 已认证或授权任何用户；
- Actor prefix 或 `subjectType` 已证明 caller 可代表该 Actor；
- Agent SemVer 已证明 release 不可变、签名、批准或安装；
- Conversation topology 已证明 membership、ACL、Task 或 provider group 存在；
- strict `ext_info` codec 已证明融云兼容、稳定回传、tenant isolation 或 authorization；
- Clerk claim 可以直接决定 tenant role；
- provider HTTP 200 等于 message delivered、accepted 或 exactly-once；
- parent group membership 自动开放 child；
- 当前原生 IM 已达到生产可用或商用闭环。

当前准确表述是：W1 identity/conversation/provider-metadata 纯值合同与严格本地 codec 已冻结并有机器
证据；持久 authority、Clerk/RongCloud adapter、真实 provider contract 和 Agent 子群业务闭环仍按 W2～W4
实施。
