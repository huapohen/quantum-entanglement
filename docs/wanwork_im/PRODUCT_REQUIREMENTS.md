# 原生 IM 产品需求 V1

> 状态：研究审计后的产品合同；不表示所有条目已经交付
>
> 分支：`dev_wanwork_quantum_entanglement`
> 产品定位：企业 IM + Agent Store + 人机共生协作空间

## 1. 产品定义

WanWork IM 不是给现有聊天软件外挂一个机器人，而是 Quantum Entanglement 的原生交互与协作
投影：真人和 Agent 都有稳定身份、群成员关系、状态和责任；平台把消息、任务、审批、产出和
因果链连接起来。

一句话定义：

> 像飞书一样完成日常企业沟通，像 Agent 操作系统一样让人和多个 Agent 在同一组织内可控、
> 可恢复、可审计地共同工作。

### 1.1 交付边界

产品愿景和首个可验收版本不是同一个口径：

- **M0 原生闭环**只证明一个不可伪造的垂直切片：组织/群聊、普通成员 Agent、Agent Store
  安装、`@Agent` 唯一子群、durable Task、Needs You、Artifact 验收、action receipt 和响应式
  Web；全程使用 fake provider 或明确隔离的 sandbox；
- **V1 企业完备性**在 M0 之上补齐企业目录、消息类型、搜索、通知、治理、桌面端、移动端、
  容量、灾备和受控融云接入；
- M0 通过不代表可直接连接生产组织或真实外发；V1 功能齐全也不替代生产 Gate。

任何里程碑都必须以可运行代码、失败矩阵、验收证据和版本化文档为准，不能用页面、演示视频
或 Agent 自报“完成”代替。

## 2. V1 用户与主体

| 主体 | 含义 | 身份来源 | 是否可发消息 |
|---|---|---|---|
| 真人成员 | 组织中的员工、外部协作者 | Clerk + 平台成员目录 | 按群权限 |
| Agent 成员 | Agent Store 中安装到组织的版本化 Agent | 平台 Agent identity；融云侧普通用户 | 仅经 Action Plane |
| 系统主体 | 群治理、审计和迁移产生的系统事件 | 平台服务身份 | 只发版本化系统消息 |
| Provider connector | Clerk、融云、模型或外部工具适配器 | service identity | 不能拥有业务决策权 |

Agent 在融云侧必须注册为普通用户，不能注册为融云机器人账号。供应商 `ext_info` 只用于区分
主体类型和映射平台 ID；平台数据库中的主体、成员、权限和版本记录才是权威事实。

身份必须拆成三个相互关联但不可混用的维度：

1. human principal：登录者、数据主体、批准者和最终业务责任人；
2. workload principal：执行服务、进程、容器、设备或 worker；
3. agent delegation：某 Agent 在某 Task 中代表谁、为何、以何种缩小后的 capability 执行。

Clerk session 不能充当 Agent 的长期授权，provider service credential 不能充当用户委托，Agent
也不能读取长期 human OAuth refresh token。

## 3. 企业 IM 基本能力

### 3.1 组织与目录

- 创建和切换组织；
- 部门树、成员资料、角色、状态和搜索；
- 邀请、加入、停用、离职和外部协作者；
- 组织管理员、群主、群管理员和普通成员；
- 成员资料中的头像、显示名、时区、语言和通知偏好；
- Agent 与真人在统一目录中展示，但有不可伪造的主体类型标识。

### 3.2 会话

- 单聊、普通群、公告群和 Agent 工作子群；
- 创建、改名、头像、描述、公告、邀请、移除、退出和转让群主；
- 群成员角色、禁言、入群审批、邀请链接和可发现性；
- 置顶、归档、免打扰、收藏和会话排序；
- 父群与 Agent 工作子群可互相导航，但成员与权限单独计算。

### 3.3 消息

- 纯文本、富文本、图片、文件、音视频引用、链接卡片和系统消息；
- `@成员`、`@Agent`、`@所有人`、回复、引用、转发、编辑、撤回；
- reaction、置顶、收藏、已读回执、未读计数和最近阅读位置；
- 本地临时 ID、provider message ID、平台 message ID 三者明确映射；
- 离线、重连、重复、乱序、分页、cursor resume 和发送结果对账；
- 消息搜索按组织、会话、发送者、时间、类型和附件筛选；
- 任何 Agent 生成文本必须显式标记 Agent 身份与执行状态。

### 3.4 文件与通知

- 文件上传前的大小、MIME、摘要和恶意内容检查；
- 文件 immutable object 与消息 attachment ref 分离；
- 会话级/关键词/@我/Agent 完成/Needs You 通知；
- 多端通知去重、静默时段和组织策略覆盖；
- 下载授权短时、最小 scope，不在消息 `ext_info` 放永久 URL。

## 4. Agent Store

### 4.1 Agent 商品/能力卡

每个版本至少展示：

- 稳定 `agentDefinitionId`、版本、发布者和所有者；
- 名称、头像、简介、适用场景、输入/输出和示例 Artifact；
- 模型、运行位置、支持协议、工具和数据类型；
- capability manifest、网络/文件/工具权限、数据去向和保留策略；
- 价格/预算、延迟、质量、成功率和可用性；指标必须同时标注时间窗、样本量、verifier、来源和
  `self-reported/verified`，无证据时显示未知；
- 签名、来源、SBOM、扫描、兼容矩阵和撤销状态；
- 当前审核等级：草稿、内部可信、组织批准、隔离、撤销。

### 4.2 认领、安装与入群

1. 用户认领或组织管理员批准某个 Agent 版本；
2. 平台创建组织级 Agent installation，冻结版本与 capability snapshot；
3. 创建平台 Agent actor，并在融云注册同 ID 映射的普通用户；
4. 通过 `ext_info` 写入限长的非秘密主体 metadata；
5. 管理员把 Agent 像真人一样加入群聊；
6. 群策略决定 `mention_only` 或受控的 `all_messages`；V1 默认 `mention_only`；
7. 升级、暂停、移除和撤销都产生审计事件，不能静默替换版本。

### 4.3 离职、撤权与版本退役

- 真人离职会撤销 Clerk mapping 对应的组织 membership、活动设备和未使用 delegation；
- Agent installation 暂停、撤销或版本退役后，不能接受新 invocation，正在执行的 Task 按组织
  策略取消、转交或进入人工处置；
- 下游 capability lease、connector token、provider membership 和定时任务必须可枚举并撤销；
- Memory、Artifact、消息和证据分别按 retention policy 归档、迁移、隔离或删除，生成 deletion
  proof；不能用“删除融云用户”冒充完整离职；
- 历史消息和 Artifact 保持原 producer/version，不因升级或重装被改写。

### 4.4 Skill 激活与 Tool 授权

Skill 不是一段永久塞进 prompt 的文本，也不能因为 Agent “知道它存在”就视为已经获得能力：

1. catalog 只向 Run 投影名称、描述、来源、版本和最小索引；
2. 模型或 planner 明确选择后，必须完整读取主 `SKILL.md`，形成 activation receipt；
3. receipt 绑定 `skillPackageVersion + objectVersion + contentDigest`，整次 Run 从不可变快照读取；
4. 脚本、模板、图片和参考资料按 manifest 物化；任何必需文件缺失、超限、摘要不符都 fail-closed；
5. Run 中途升级、撤回或覆盖 Skill 不改变已激活字节；新 Run 必须重新 admission/activation；
6. 第三方 Skill 只能由 Agent 提交安装提案，不能绕过组织的来源、扫描、审批、隔离和撤销策略。

Tool/MCP 能力必须拆成三个权威对象，不能用“已安装”同时表达定义、授权和一次执行：

| 层 | 权威内容 | Run/Action 冻结内容 |
|---|---|---|
| Capability Definition Version | 输入/输出 schema、effect、provider、数据分类、route contract | `capabilityVersionId` |
| Agent Capability Assignment Revision | 哪个 Agent 在何 tenant/workspace 可用、策略、配额和状态 | `assignmentRevision` |
| Execution Binding | 本次 route、credential ref、policy、deadline、idempotency/reconcile | `routeDigest + credentialRef + policyRevision` |

执行前必须重新校验 membership、Agent/assignment 状态、route digest、credential lease 和 action policy；
checkpoint、消息、Artifact、event 和模型上下文只保存 opaque credential reference，绝不保存 secret。

## 5. `@Agent` 工作子群

### 5.1 触发规则

只有同时满足以下条件才创建 invocation：

1. 入站消息来自已验证的融云事件或平台 fake adapter；
2. 消息已写入 durable inbox 并完成稳定去重；
3. mention 指向父群中的 active Agent member；
4. 发送者有权调用该 Agent，群策略允许，预算和数据范围通过；
5. 同一 `(tenant, parentConversation, providerMessage, agent)` 尚未创建 invocation。

`@Agent` 只绕过 LLM 选 Agent，不绕过记录、授权、上下文、预算、审批和结果事务。

### 5.2 子群语义

融云没有满足产品需要的原生子话题，因此平台创建真实子群：

- `conversationType=agent_thread`；
- 保存 `parentConversationId`、`rootMessageId`、`agentInvocationId`；
- 默认成员是触发用户、被 @ 的 Agent 和明确加入的协作者；
- 父群成员不能因属于父群而自动读取子群；访问按子群 ACL 计算；
- 父群只展示不含敏感正文的工作卡片：状态、Agent、发起人、可见范围和子群入口；
- Agent 的 progress、Needs You、结果和回复只发送到子群；
- 最终 Artifact 可在用户确认后以引用卡片发布回父群；默认不自动回发。

### 5.3 子群与执行生命周期分离

```text
Thread: requested -> provisioning -> ready -> provisioning_failed -> archived
Execution: admitted -> queued -> running -> waiting_human | waiting_external
           -> succeeded | failed | cancelled | effect_unknown
```

创建子群、邀请成员和启动 Agent 分属不同的 durable command，不允许一个 HTTP handler 直接完成
所有外部副作用。Thread ready 不表示 Task 已开始；Execution succeeded 也不表示结果已验收。任何
provider ACK 丢失进入 `effect_unknown` 并查询接受状态，不能盲重试。

## 6. 权威 Task、Artifact 与验收

### 6.1 Task 是业务脊柱

聊天消息可以触发或投影 Task，但不能充当权威 Task。每个 Agent 工作至少冻结：

| 字段簇 | 最小内容 |
|---|---|
| Identity | tenant、human principal、workload、Agent 与 subagent chain |
| Mandate | objective、purpose、scope、owner、deadline |
| Capability | tools/data/actions、audience、constraints、expiry |
| Budget | token、money、compute、wall time、attempt、human attention |
| Context | 输入版本、来源、taint、许可、retention |
| Plan/Execution | 计划版本、依赖、runtime/model/plugin 版本、checkpoint |
| Artifact/Acceptance | schema、hash、lineage、verifier、rubric、threshold |
| Evidence/Recovery | policy、approval、action receipt、idempotency、retry、compensation |
| Closure | accepted/rejected/cancelled、cost、residual risk、revocation/delete proof |

### 6.2 Task 状态机

```text
draft -> planned -> authorized -> running
      -> waiting_human | waiting_external
      -> delivering -> verifying
      -> compensating
      -> closed(accepted | rejected | cancelled)
```

`completed`/`succeeded` 只描述一次 execution，不推进 Task 到 accepted。取消 Task 必须撤销后续能力；
已发生且不可逆的动作进入补偿或人工事故流程。

### 6.3 Artifact 与 Acceptance

- Artifact 必须有 type、schema、immutable version、content hash、producer、environment、lineage、
  evidence bundle 和 acceptance status；
- 文件、报告、代码、卡片和结构化数据都通过 Artifact reference 进入聊天，消息正文不是交付真相；
- verifier 与 producer 默认分离；自动测试、人工 review、四眼审批或独立第三方按风险组合；
- `accepted`、`request_changes`、`rejected`、`disputed` 都记录 reviewer、rubric version、理由和证据；
- 发布回父群、合并、部署、对客发送、付款等高风险动作只能消费已满足相应 acceptance contract
  的 Artifact，并仍需 action-time authorization。

## 7. Needs You：Human Attention OS

Needs You 不是一条 `@我` 消息或同步确认弹窗，而是 durable decision request：

- 按风险、最大损失、截止时间、可逆性、置信度和阻塞范围排序；
- 展示业务影响、冻结后的参数、参数 hash、与上次批准值的 diff、请求 evidence 和推荐安全默认；
- 支持 approve、reject、edit、delegate、request evidence、approve once/for task；
- 批准绑定 Task、action、参数 hash、policy version、批准者和 expiry，批准后参数变化必须重新申请；
- 支持四眼原则、代理审批、轮班、升级、超时和 fail-closed；
- 所有决定产生 receipt；拒绝理由回流 Plan/Policy，不能只关闭弹窗；
- 统计每个 verified outcome 的 human intervention minutes 和 approval fatigue。

## 8. 受治理的 Memory

Memory 是有生命周期的决策资产，不是把群聊全部写入向量库：

1. ingest 记录 source、owner、license/consent、timestamp、hash、trust/taint；
2. store 绑定 tenant/user/task scope、encryption domain 与 TTL，默认最小共享；
3. retrieve 必须先授权再检索，不能先跨租户召回后遮罩；
4. use 记录哪条 Memory 影响哪个 plan/decision/artifact；
5. conflict 结合时间、来源信誉和人工裁决，禁止静默 last-write-wins；
6. promote 为组织 Skill/Policy 前需要 eval、review、version 和 rollback；
7. forget 覆盖主存储、索引、缓存、备份到期和下游传播，并提供 deletion proof。

M0 只实现最小 provenance/scope/TTL/use-lineage；组织级共享记忆、冲突裁决和可携带导出在 V1
完成。任何原始消息、个人偏好或敏感资料都不会因 Agent 读过一次而自动晋升为组织记忆。

## 9. 外部动作与协议边界

- 所有 connector/IM 动作先形成 canonical intent、参数 hash、policy decision、credential lease 和
  durable command，执行后保存 provider acceptance evidence 与 receipt；
- timeout/断连/ACK 丢失进入 `effect_unknown`；没有 authoritative negative finality 时不得盲重试；
- MCP 只标准化 Host/Client 与 Server 的能力调用，A2A 只标准化 Agent 间 Task/Artifact 互操作，
  Client ACP 只用于 Client/Editor 与 coding Agent；支持协议不等于信任、授权、验收或赔付；
- 跨协议必须保留 tenant、actor、subject、task、purpose、audience、causation 和 policy decision，
  防止 confused deputy；
- 插件、Skill、MCP server、模型和镜像在进入私有 catalog 前需 provenance、digest、签名状态、
  CBOM/SBOM、capability manifest、扫描、兼容矩阵、policy verdict 和撤销状态。
- Skill 未产生绑定不可变包快照的 activation receipt 时不得执行；Tool definition 存在、Agent 已安装或
  模型生成 tool call 都不等于本次 execution binding 已获授权。

## 10. 关键页面

1. 登录/组织选择；
2. 工作台：会话、未读、搜索、Needs You 和最近 Artifact；
3. 单聊/群聊：消息、成员、文件、公告和 Agent 工作卡；
4. Agent 工作子群：执行时间线、任务图、上下文来源、Artifact 和审批；
5. Agent Store：发现、详情、权限清单、认领/安装/升级；
6. 通讯录：真人、Agent、部门和外部协作者；
7. 管理后台：组织、策略、审计、保留、供应链和 provider health；
8. 个人设置：设备、通知、语言、隐私和数据导出。

Needs You、Task、Artifact 和审计不是只在 Agent 子群出现的二级页面；工作台必须提供跨会话的
Attention inbox、Artifact review queue 和任务恢复入口。

## 11. 验收标准

### 11.1 M0 原生闭环

- fake provider、fake runtime 和测试身份可零网络启动；Clerk/RongCloud adapter 合同存在但真实
  outbound 默认禁用；
- 两个真人可创建组织与群聊，安装普通用户身份的 Agent 并加入群；
- `@Agent` 重放只创建一个子群、一个 Task 和一个 invocation；失败可从 durable 状态恢复；
- Agent execution succeeded 后 Task 进入 delivering/verifying，而不是直接 accepted；
- 一个 Needs You 可冻结参数、拒绝/修改/批准并留下 receipt；
- 一个 Artifact 可经独立 verifier 接受或退回，接受后由真人显式发布引用回父群；
- action timeout 可演示 `effect_unknown -> reconcile -> accepted|not_accepted|manual_review`；
- Web 在 1440px 和 390px 可完整走通以上闭环，合同、测试、截图、运行手册和 Notion 回读齐全。

### 11.2 V1 企业完备性

- 两个真人可创建组织、单聊和群聊并完成基本消息操作；
- 同一消息在重复 webhook、断线重连和 cursor resume 后只接纳一次；
- Agent 作为普通用户进入群成员目录，UI 和审计能稳定区分主体类型；
- 用户可从 Agent Store 安装 Agent 并把它加入群；
- `@Agent` 创建唯一工作子群，同一个 mention 重放不创建第二个群或 invocation；
- Agent 回复只进入子群，父群只出现受限工作卡；
- 业务错误全部使用 HTTP 200 envelope，测试覆盖 auth、validation、conflict 和 provider error；
- fake provider 完整覆盖成功、NACK、429、timeout、ACK loss 和 reconcile；
- 默认配置不包含真实融云 endpoint/credential，不产生真实 outbound；
- Web 在 1440px、1024px 和 390px 视口可用；键盘、屏幕阅读器和弱网状态可验收；
- Desktop/Mobile 共享合同与设计 token，不各自发明消息语义；
- Git 小步提交、测试、运行手册、截图和 Notion 阶段回读齐全。

### 11.3 生产 Gate

- tenant isolation、action-time authorization、offboarding、backup/restore、RTO/RPO、retention 和
  deletion proof 有故障注入或恢复演练证据；
- 插件/Agent/镜像供应链 admission 与撤销可演示，未知来源默认拒绝；
- Skill 激活在 Run 中途升级后字节不漂移，部分物化失败关闭；stale Tool assignment、route drift、
  跨 Agent assignment 和 checkpoint secret canary 全部被拒绝；
- 日志、trace、Notion 和 Git secret canary 通过；消息正文、token、永久附件 URL 不进入普通日志；
- 只有专用融云 sandbox、明确 allowlist、限额、kill switch、reconcile 和用户对该具体目标的新授权
  都成立时，才允许真实 outbound；通过 M0/V1 不自动满足本 Gate。

## 12. V1 非目标

- 生产环境自动对外发送批准；
- 端到端加密承诺（需单独密钥和多端协议设计）；
- 跨组织公开 Agent 经济网络；
- 公共 Agent marketplace、自动结算/escrow/dispute/reputation 与劳动力市场；
- 跨组织 federation、Agent 绩效 HRIS 和 Agent lending；
- 宣称不同 harness/session 可无损迁移，或宣称支持协议即获得对等方信任；
- 将融云历史记录当平台唯一真相源；
- 让模型决定 endpoint、credential、群 ID、成员权限或 retention；
- 用 `ext_info` 代替数据库、授权 token 或审计证据。
