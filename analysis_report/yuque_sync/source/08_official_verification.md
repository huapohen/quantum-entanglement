# 06｜竞品官方信源、许可证与实现核验

> 核验日期：2026-08-19（Asia/Shanghai）
>
> 核验对象：`04_competitor_landscape.md` 中的 14 个产品
>
> 证据范围：产品官网、公开文档、生产 API 说明、官方 GitHub 组织/仓库、仓库内许可证与实现文件
>
> 限制：本轮是“信源与源码静态核验”，未注册商业产品、未执行付费流程，也未把“存在代码/测试文件”写成“已完成端到端实测”。
## 1. 结论先行
此前竞品表的方向判断基本成立，但事实层需要做九项重要校正：
1. **Pi Agent 的能力描述几乎写反了。** Pi 官方文档明确说核心刻意不内置 MCP、sub-agent、权限弹窗、plan mode、todo、后台 Bash；这些能力只能由扩展、package 或外部工具补上。
2. **Multica 是公开源码且可自托管，但不能不加限定地称为“开源”。** 它使用带附加限制的 `Multica License`：未经商业许可，不得向第三方提供托管服务或把它嵌入对外商业产品，并有品牌保留要求；这不是原版 Apache-2.0，也不应按 OSI 开源来理解。
3. **“Coze 3.0 完全闭源”也不准确。** 当前扣子办公托管产品与所谓 Agent Team 体验没有找到对应开源实现，但官方 `coze-dev/coze-studio` 的核心引擎确实以 Apache-2.0 发布。二者必须拆开评价。
4. **Slock 已更名/迁移为 Raft。** `slock.ai` 当前直接重定向到 `raft.build`；后续产品跟踪应使用 Raft 名称。
5. **Gotaa Pi.Agent 当前不可验证。** `pi.gottaa.com` 已无法 DNS 解析，`gottaa.com` 是 GoDaddy parking lander；不能继续把截图里的岗位、SOP、审批等写成当前事实。
6. **FloatIM 的“协议开放”目前只得到官网声明，未得到公开规范或源码仓库支撑。** 官网说 IACT、Selfware 为 MIT，但页面没有可核对的仓库/许可证文件；“协议开放”不能外推成“FloatIM 产品开源”。
7. **Mindra 的 RBAC、审计、SOC 2 Type II、GDPR、人工审批不再是无来源传闻。** 它们已出现在官方 Security 页面；但仍属于厂商声明，未得到源码或独立审计报告正文验证。
8. **NEAR Agent Market 的 escrow、bid、submit、accept、dispute 是可由生产 API 文档验证的真实接口；TEE 不是同一层证据。** `near.ai` 的机密计算叙事不能自动证明 Market 的每个任务都在 TEE 中运行。
9. **Todos 的公开文档比旧截图更具体。** Chief、Agent、版本化 plan/diff、AI review、独立分支、机器 claim、MCP、权限与调度都有官方文档；但服务条款明确产品软件归运营方/许可方，仍没有产品源码许可证。
对 WanWork 最有价值的、已有较强实现证据的参考对象是：
- **任务与运行状态：Multica**——任务队列、claim、运行记录、失败分类、lease/retry、实时投影的代码证据最完整。
- **人工许可与本地执行：OpenWorker**——风险分类、跨会话 Inbox、幂等审批、审计脱敏均能在 MIT 源码中定位。
- **群聊式多 runtime 汇聚：OpenAgents**——Workspace、Launcher、Network SDK、共享线程/文件/浏览器和 A2A adapter 均有源码。
- **长期身份与治理边界：CodexLoom**——稳定 Agent ID、Profile、Topic、Message、Artifact、Needs You 的对象边界可读，但许可证为 Elastic License 2.0，且不应把组织图当权限系统。
- **市场化验收与结算：NEAR Agent Market**——任务、投标、交付、验收、争议、托管资金形成完整 API 状态机，但公开 SDK 不是后端实现。
## 2. 证据口径
### 2.1 四类标签
本文所有关键判断使用以下标签，不把不同证据等级混写：

| 标签 | 含义 | 可以证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| **官方宣称** | 官网、官方文档、官方仓库 README 的产品表述 | 厂商当前如何定义产品、公开承诺哪些能力 | 功能一定稳定、性能达标、边界条件已处理 |
| **可验证实现** | 官方仓库中的源文件、迁移、测试、API schema、许可证原文 | 对象/接口/代码路径确实存在，可定位到具体文件 | 本轮已经跑通生产端到端，或云端与仓库完全同版本 |
| **推断** | 基于已核实事实做的竞争判断或架构含义 | 对 WanWork 的合理启示、产品边界判断 | 厂商意图或未公开实现的确定事实 |
| **未知** | 当前官方信源不可达、无仓库或证据不足 | 明确后续核验缺口 | 不等于“没有”，也不等于“做不到” |


### 2.2 核验强度

| 级别 | 证据 | 本文使用方式 |
| --- | --- | --- |
| L0 | 域名失效、只有二手截图 | 只记录来源与不可验证状态 |
| L1 | 官方营销页 | 仅写“官方宣称” |
| L2 | 官方操作文档、API/OpenAPI | 可确认产品对象、接口和公开流程 |
| L3 | 官方源码、许可证、迁移、测试 | 可写“可验证实现”，并给出文件路径 |
| L4 | 本地/生产端到端运行 | 本轮没有执行；不得虚构 |


### 2.3 开放性的拆分
“开源”在竞品宣传中经常混用。本文拆为五个独立问题：
1. 产品源码是否公开；
2. 许可证是否为 OSI 常见开源许可证；
3. 是否允许自部署；
4. 协议/SDK 是否公开；
5. 托管 SaaS 是否与公开源码同一功能边界。
只公开 SDK、协议或文档，不等于产品后端开源；可自托管也不必然意味着许可证允许提供竞争性 SaaS。
## 3. 14 项核验矩阵

| # | 产品（当前名称） | 官方入口状态 | 公开实现 | 许可证结论 | 原表关键结论的核验状态 | 置信度 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | YouMind | `youmind.com` 本机超时；中文同源站可达 | 未找到官方产品仓库 | 产品代码未知 | 创作工作室定位有官方元数据；多 Agent 协作未证实 | 中低 |
| 2 | 千问AI | 官网可达 | Skills、CLI、部署技能等官方仓库 | 公开组件 Apache-2.0；平台服务未知 | 模型/Agent 能力供给成立；多 Agent 协作平台不成立 | 高 |
| 3 | FloatIM | 官网可达 | 未找到产品或协议仓库 | 官网声称 IACT/Selfware MIT；文件未核到 | Agent 原生群聊为官方宣称；实现强度未知 | 中 |
| 4 | Multica | 官网、文档、仓库均可达 | 完整 monorepo、服务端、客户端、迁移与测试 | 自定义 Multica License，非纯 Apache-2.0 | Coding Agent 工作台、运行队列、自托管成立 | 高 |
| 5 | Todos | 官网、文档、更新日志可达 | 未找到产品源码 | 条款显示服务软件归运营方/许可方 | Chief、分支并行、plan/diff、review 成立于官方文档 | 中高 |
| 6 | NEAR Agent Market | Market、API skill、OpenAPI 可达 | 集成 SDK 公开；后端未公开 | SDK MIT；Market 后端未知 | 投标、托管、验收、争议成立；TEE 关联未知 | 高（API）/中低（实现） |
| 7 | Slock → Raft | `slock.ai` 重定向到 `raft.build` | 文档与外部 Agent 插件公开；产品核心未找到 | 产品未知；插件/文档各自需单查 | 群聊、常驻 Agent、本地 daemon 为官方宣称 | 中高 |
| 8 | Mindra | 官网和 Security 页可达 | 未找到官方产品仓库 | 产品未知/商业 SaaS | Agent Team、3,000+ 集成、RBAC/审计/审批均有官方声明 | 中高 |
| 9 | OpenWorker | 官网、仓库可达 | Python backend、Tauri/React UI、测试 | MIT | 本地优先、BYOM、风险审批、Inbox、审计均可定位代码 | 高 |
| 10 | Pi Agent | `pi.dev`、仓库可达 | 完整 monorepo | MIT | 旧表列出的内置 MCP/sub-agent/plan/todo 等必须删除 | 高 |
| 11 | Gotaa Pi.Agent | 子域 DNS 失败；主域为停放页 | 未找到 | 未知 | 当前产品状态和全部功能均不能确认 | 低 |
| 12 | CodexLoom | 官网、仓库可达 | Go backend、WebUI、CLI、文档与测试 | Elastic License 2.0（非 OSI 开源） | 长期 Agent/消息/Topic/Artifact 有源码；组织图不授予权限 | 高 |
| 13 | Coze（扣子） | 当前办公平台可达 | Coze Studio 核心仓库公开 | Coze Studio Apache-2.0；托管产品边界未知 | 办公平台成立；“Coze 3.0 Agent Team”未找到官方对应页 | 高/中 |
| 14 | OpenAgents | 官网、仓库可达 | Workspace、Launcher、Network SDK、Studio、A2A 代码 | Apache-2.0 | 多 Agent Workspace 与协议适配成立；企业治理仍需上层 | 高 |


## 4. 分产品核验
### 4.1 YouMind：创作工作室可确认，多 Agent 协作不可确认
**当前身份与入口**
- `https://youmind.com/` 在本轮网络环境中多次连接超时，因此不能声称已直接浏览主站完整功能。
- `https://youmindchina.com/zh-CN` 可达；页面 title 为“YouMind - AI 创作工作室”，description 为“利用生成式 AI 将多样化材料转化为创作”，结构化数据中的组织 URL 与 OpenGraph URL 都指向 `youmind.com`。这支持它与主站同源/官方镜像的判断，但“两个域名的法律运营主体完全一致”仍未单独核验。
**证据拆分**
- **\[官方宣称\]** 产品是 AI 创作工作室/创作智能体，围绕学习、材料吸收与创作展开。
- **\[可验证实现\]** 未找到由页面链接出的官方 GitHub 产品仓库、许可证文件或可自部署说明。
- **\[推断\]** 它适合作为 artifact-first、资料到成品连续体验的参照；现有公开材料更像个人/创作者工作区，而不是多人、多 Agent 责任网络。
- **\[未知\]** Board 的后端数据模型、素材引用/版本机制、是否支持独立 Agent 身份、Agent 间 handoff、企业权限与产品代码许可证。
**对原表的修订**
原表“从资料收集到多格式成品的 artifact 工作流”可保留为产品体验概括，但应标注为官网/语雀材料推断，不能写成已验证状态机。“缺少 Agent 间责任与依赖”也应写成“官方公开材料未见”，而不是绝对不存在。
**对 WanWork 的可用启示**
- 借鉴的是“工作最终落到可浏览的成品”，不是它的协作内核。
- 必须把素材引用、生成过程、版本、验收和责任者做成平台对象；不能只复制创作画布。
### 4.2 千问AI：供给层有真实开源组件，不是多 Agent 协作层
**当前身份与入口**
- 官网把产品描述为全栈模型矩阵与 AI 原生应用平台，提供模型、API Key、调用量/性能、Agent 能力扩展、用量与成本管理。
- 官方 GitHub 组织 `QianWen-AI` 当前公开 `qianwen-ai`、`qianwen-cli`、`qianwenai-deploy` 等仓库。
**证据拆分**
- **\[官方宣称\]** `qianwen-ai` 提供 10 个 Agent Skills，覆盖文本、视觉、图像、视频、语音、模型选择、认证、用量、支付和技能发现，并可装入多个支持 Agent Skills 的宿主。
- **\[可验证实现\]** `QianWen-AI/qianwen-ai` 与 `QianWen-AI/qianwen-cli` 均声明 Apache-2.0；CLI README 给出 JSON 输出、稳定退出码、模型/用量/账单/技能命令和系统钥匙串凭据存储。仓库存在具体实现，不只是官网入口。
- **\[推断\]** 千问AI是模型、技能和运营接口的供给层，可以成为 WanWork 的 provider/skill adapter，但它没有证明自己拥有 Agent 群成员、任务图、handoff、artifact 事务或协作审计状态。
- **\[未知\]** SaaS 平台服务端是否开放、平台 SLA/隔离实现、企业权限细节，以及多个宿主 Agent 之间是否共享任何协作真相源。
**对原表的修订**
- “企业模型与应用平台”成立。
- 不能因页面出现“Agent Skills”就归类为多 Agent 协作产品。
- 公开组件的 Apache-2.0 只覆盖对应仓库，不能外推到千问AI整个托管平台。
**对 WanWork 的可用启示**
- 把模型目录、用量、费用、技能安装作为适配层；任务状态、权限和 artifact 不能交给供应商 CLI。
- 借鉴 CLI 的机器可读 JSON 和稳定退出码，为 runtime adapter 设计可恢复协议。
### 4.3 FloatIM：产品命题强，公开实现证据弱
**当前身份与入口**
官方页面将 FloatIM 定义为 Floatboat 的 Agent-native messaging layer：人和 Agent 在同一群聊，Agent 在用户机器运行，多 Agent 可自组织协作。
**证据拆分**
- **\[官方宣称\]** First-Class Agents：Agent 理解群规则、可见范围、交互对象和异常处理。
- **\[官方宣称\]** Runs on Your Computer：通过 Floatboat 在 macOS/Windows 本地托管 Agent。
- **\[官方宣称\]** Self-Organizing Teams：多个 Agent 临时组队、承担角色、在关键时点向人确认并交付。
- **\[官方宣称\]** IACT 与 Selfware 是 MIT-licensed 开放协议；IACT 表达可点击/可填写的聊天文本，Selfware 表达携带数据、生成步骤和延续 Agent 的 artifact。
- **\[可验证实现\]** 本轮未从产品页定位到 IACT/Selfware 的公开规范仓库、版本号、LICENSE 文件、测试向量，也未找到 FloatIM 产品源码仓库。
- **\[推断\]** “Agent 是群成员”是与 WanWork 最接近的交互命题，但页面示例不足以证明任务状态、权限执行、消息幂等、handoff 或 artifact 冲突处理。
- **\[未知\]** 本地 runtime 的进程隔离、群规则的强制执行位置、离线恢复、协议兼容方、生产用户规模、部署形态与产品许可证。
**对原表的修订**
“支持开放协议”应改成“官网声称两项协议 MIT”；“本地化方向”可改成“官网明确声称 Agent 运行在用户电脑”。不得写“FloatIM 开源”。
**对 WanWork 的可用启示**
- 保留独立 Agent 身份、群内原生发言和临时团队体验。
- 内部仍要用结构化事件、任务、Artifact 和授权对象支撑，不能只依赖聊天历史或协议文案。
### 4.4 Multica：状态和代码证据最强，但许可证不是纯开源
**当前身份与活跃度**
- 官方仓库：`multica-ai/multica`，主分支 `main`。
- 本轮 GitHub 快照：最新 release 为 `v0.4.30`（2026-08-19）；仓库同日仍有推送。
- README 当前声称支持 20 个 Agent CLI，而不是旧材料中的 22；包括 Claude Code、Codex、Cursor、Copilot、Kimi、OpenCode、Pi 等。
**证据拆分**
- **\[官方宣称\]** Agent 像队友一样领取 issue、报告进度、提出 blocker、交回 review；支持本地 daemon、Docker Compose/Helm 自托管、Slack/Lark/DingTalk/WeCom 渠道。
- **\[可验证实现\]** `apps/docs/content/docs/tasks.mdx` 定义 `deferred → queued → dispatched → waiting_local_directory → running → completed/failed/cancelled` 状态；一次 issue 可产生多个不可覆盖的 task 记录。
- **\[可验证实现\]** `server/internal/handler/task_lifecycle.go` 实现 daemon 重启后的 orphan task 恢复、失败事件与 retry 管道，以及 session/work_dir 持久化。
- **\[可验证实现\]** `server/migrations/055_task_lease_and_retry.up.sql` 增加 attempt、max_attempts、parent_task_id、failure_reason、last_heartbeat_at；`server/internal/service/` 下有 claim race、complete race、cancel reconcile、batch claim、failure event 等测试。
- **\[可验证实现\]** Web/移动端存在 issue realtime/WS updater 与测试，README 架构说明 Go 后端使用 `gorilla/websocket`，daemon 通过 WebSocket 获取任务。
- **\[可验证实现\]** 许可证原文不是 Apache-2.0：它在完整 Apache 文本之前加入附加条件，限制未经商业许可的第三方托管与商业嵌入，并要求 UI 品牌保留/后端归因。
- **\[推断\]** 它是很强的 Coding Agent 执行工作台和任务事实源参考，但 issue/branch/diff 仍主要针对软件交付，不能自然覆盖文档、表格、审批单、客户记录等通用办公 artifact。
- **\[未知\]** README 的全部渠道、20 个 runtime 与云/自托管组合是否都通过真实 provider E2E；本轮没有启动整套服务。
**对原表的修订**
- “开源与 Docker/K8s”改成“源码公开、自托管；使用带 SaaS/嵌入限制的自定义许可证”。
- “enqueue → claim → start → complete/fail”应采用官方实际状态名，而不是简写。
- “22 个 Coding tools/agents”更新为 README 当前列出的 20 个 Agent CLI。
**对 WanWork 的可用启示**
- 优先借鉴 task/issue 分离、不可覆盖的 run history、lease/retry、orphan recovery、失败分类与实时投影。
- 不复制其许可证或把 coding issue 当通用协作对象；WanWork 的 Artifact 契约应独立于 Git。
### 4.5 Todos：公开文档已足以确认工作模型，但源码与许可证不开放
**当前身份与入口**
官方文档称 Todos 是“people and agents work together”的 product workspace。它不再只是一个营销 landing page：Docs、Changelog、Terms 均可访问，更新日志在 2026-08-18 仍有条目。
**证据拆分**
- **\[官方宣称\]** Chief 持有跨项目的常驻对话，把目标拆成 todo；Agent 有名称、模型、角色描述、Skills 和持久 memory。
- **\[官方宣称\]** 一个 todo 同时是一段与执行 Agent 的对话；build 产出代码修改前的 plan 和修改后的 diff，二者均版本化。
- **\[官方宣称\]** AI review 可先审 plan/implementation；多个 todo 在独立 branch 并行，完成后按 review 结果依次 merge。
- **\[官方宣称\]** machine 领取 run，而不是 Agent 固定占有机器；支持自有机器、平台机器、模型 provider、MCP server、secret、权限、远程 shell、调度与移动端。
- **\[可验证实现\]** 公开 Changelog 持续记录 machine presence lease、同步 push、失败传播、断线恢复、模型上下文限制等具体修复，说明产品至少有持续运营的详细行为规格；但这仍不是源代码证明。
- **\[可验证实现\]** Terms 第 3 节把服务定义为协调 project/todo/conversation/build/code review；第 4 节明确“service itself, including its software and design, remains ours or our licensors'”。因此不能把页面中 avatar 使用的 CC0 或其他素材许可解释为产品代码许可证。
- **\[推断\]** Todos 当前是偏 Git 项目和小团队的封闭协作产品，确定性 branch/review 比旧截图更成熟；其 Context、Machine claim、plan/diff 是强产品参考。
- **\[未知\]** 服务端状态机、消息幂等、分支冲突策略、权限强制点、沙箱边界和灾难恢复均无法由公开源码审计。
**对原表的修订**
- 保留“目标拆解、并行执行、分支隔离、模型可替换”，并将证据标为官方文档。
- 删除“许可证来源需核实”的模糊说法，改为“产品源码许可证未公开；Terms 表明服务软件归运营方/许可方”。
- “按依赖前序触发后序”的精确 DAG 语义，本轮没有在官方页面定位到足够直接的状态定义，仍应标未知。
**对 WanWork 的可用启示**
- 学习 plan/diff 的版本化评审和人类 gate，而不是把所有过程折叠在聊天里。
- 将 Machine、Agent、Todo 三者解耦；调度目标由能力、位置、风险和并发决定。
### 4.6 NEAR AI Agent Market：生产 API 闭环可核验，Market 后端与 TEE 不可混同
**当前身份与入口**
- `near.ai` 当前首页定位已转向 confidential AI：模型和 Agent 在硬件 enclave 中执行，强调请求保密与输出可验证。
- `market.near.ai/skill.md` 是生产端公开的 Market API 操作说明，version `0.3.0`，并链接 OpenAPI 3.1。
- 官方集成仓库 `nearai/agents-market-sdk` 是 MIT 许可证，提供 Node backend middleware、React 组件和 embed widget；它不是 Market 服务端源码。
**证据拆分**
- **\[官方宣称/L2 API\]** Agent 可注册、创建 job、浏览和投标；award 原子化锁定 escrow 并转为 `in_progress`；worker submit，requester accept 后释放资金。
- **\[官方宣称/L2 API\]** 支持 NEAR、USDC 与 USD；USD 走 Stripe Connect/内部余额；支持 human one-time login link。
- **\[官方宣称/L2 API\]** 生产说明公开 dispute、evidence、resolver ruling、WebSocket/webhook、消息等流程，并给出 job/assignment 状态机。
- **\[可验证实现\]** SDK README 与源码包含 `MarketClient`、Express middleware、`MarketPanel`、`ChatPanel`、`useJob` 及 `acceptJob` 等集成接口；许可证为 MIT。
- **\[推断\]** Market 的验收/争议/付款模型可转译为企业内部“提交证据 → 复核 → 接受/退回 → 升级仲裁”，不必引入代币。
- **\[未知\]** Market 后端源代码、合约/托管实现的完整审计、生产争议仲裁质量；更重要的是，`near.ai` 的 enclave 叙事是否覆盖 Market 每一种 worker 执行路径。
**对原表的修订**
- “悬赏、执行、验证、结算/争议闭环”可以提升为有生产 API 文档证据。
- “TEE”必须从 Market 产品能力中拆出：当前只验证到 NEAR AI 母平台的 confidential infrastructure 声明。
- “官方 SDK 开源”不能写成“Agent Market 后端开源”。
**对 WanWork 的可用启示**
- 引入验收证据、request changes、dispute/复核状态和清晰的责任角色。
- 内部任务先做权限/信用/审计闭环；支付和经济机制应作为可插拔域，而不是内部协作默认依赖。
### 4.7 Slock → Raft：品牌迁移已确认，核心源码仍未确认
**当前身份与入口**
- 访问 `https://slock.ai/` 会重定向到 `https://raft.build/`，因此当前产品名应写为 **Raft（原 Slock）**。
- 官网定位为“Where humans and AI agents build together”；Docs 定义 Agent 为房间中的真实队友，拥有持久身份、记忆与协作能力。
**证据拆分**
- **\[官方宣称\]** Chat is the workspace：channels、DM、threads 是统一工作面，人和 Agent 共享上下文。
- **\[官方宣称\]** Long-running agents：每个 Agent 是带自身 memory 的持久进程，保留代码库、偏好和历史对话。
- **\[官方宣称\]** Your computers, your agents：通过轻量 daemon 在用户硬件执行。
- **\[可验证实现\]** 官方 GitHub 组织 `botiverse` 公开 `raft-docs`、`raft-external-agents`、`create-raft-app` 等；文档仓库和外部 Agent 插件说明产品存在可集成的公开边界。
- **\[可验证实现\]** 公开组织中未发现名为 Raft server/web/client 的完整产品核心仓库；公开 docs 仓库的“无许可证识别”也不能代表产品许可。
- **\[推断\]** Raft 在“群聊即工作区 + 常驻 Agent + 本地 runtime”上与 WanWork 接近，但公开证据不足以确认任务图、Artifact 版本、组织权限和消息事务性。
- **\[未知\]** 产品核心代码、许可证、daemon 的进程/凭据隔离、离线恢复、消息保留、企业多租户与审计模型。
**对原表的修订**
- 全文把 Slock 更新为“Raft（原 Slock）”。
- “任务、知识和文件协作”中，当前官网可直接确认的是聊天、身份、memory 和本地执行；精确任务/文件状态应继续标未知。
**对 WanWork 的可用启示**
- 群聊可以成为高频入口，但必须和任务、Artifact、Needs You 等结构化投影同源。
- “常驻进程”不是稳定业务身份的充分条件；身份、runtime、thread 和组织角色要分离。
### 4.8 Mindra：企业治理叙事有官方页面，仍缺可审计实现
**当前身份与入口**
Mindra 官网当前将产品定义为“AI Employees/AI coworkers”：用户描述业务任务，系统组建多个 Agent 协作完成。页面不只是列集成 Logo，也给出了“工作如何运行”和安全控制说明。
**证据拆分**
- **\[官方宣称\]** Mindra 是“一整个 AI coworker department”，会规划工作、分配角色、协调并交付；Agent 之间传递工作，用户可查看完整对话。
- **\[官方宣称\]** 官网写明“3,000+”集成，并称任何有 API 或 MCP server 的系统也可连接；页面展示 Slack、Google Workspace、Notion、GitHub、Figma、广告平台、CRM、监控与 n8n/Zapier 等类别。
- **\[官方宣称\]** Security 页面列出 SSO、RBAC、exportable audit logs、human-in-the-loop、runtime policy/guardrail、reversible action 与预算限制。
- **\[官方宣称\]** Security 页面声明 SOC 2 Type II、GDPR compliant、Zero Data Retention、传输与静态加密、tenant isolation。
- **\[官方宣称\]** 敏感动作可以暂停，用户从应用内、Slack 或邮件批准；首页示例称花钱、发帖等重要动作必须取得用户同意。
- **\[可验证实现\]** 本轮未找到由官网链接出的官方产品源码仓库、许可证、公开状态机或可自部署版本；也未读取独立 SOC 2 报告正文。
- **\[推断\]** 它证明企业会为“岗位结果 + 集成 + 审批 + 合规”购买，但无法从公开材料判断其 Agent 协作是动态 handoff，还是厂商预设 workflow 的包装。
- **\[未知\]** RBAC 的资源粒度、Agent credential 隔离、审批的 TOCTOU 防护、审计不可篡改性、回滚语义、数据驻留和故障恢复。
**对原表的修订**
- 原表“RBAC/审计/SOC2/GDPR 等治理叙事”已有直接官方页面，可从“待找来源”改成“官方宣称已核实”。
- 仍不能写“已通过我方安全审计”或“实现已验证”；合规 badge 与控制文案不等于独立报告验证。
- “可能偏 n8n/Zapier”只能保留为竞争推断，不能写成产品事实。
**对 WanWork 的可用启示**
- 把 approval、audit、credential、tenant、budget 做成底层统一机制，而不是每个岗位模板各自实现。
- 产品页可以用岗位结果表达价值，内部仍要用任务、Artifact、策略和授权证据表达真相。
### 4.9 OpenWorker：本地优先、人工许可和审计都有直接代码证据
**当前身份与活跃度**
- 官方仓库 `andrewyng/openworker`，README 标注 open beta。
- 本轮 GitHub 快照：最新 release `v0.1.7`（2026-07-30），仓库 2026-08-19 仍有更新。
- 产品由 Python agent server、React/Tauri 桌面壳、Rust 语音组件构成；README 说明引擎建立在 `aisuite` 上。
**证据拆分**
- **\[官方宣称\]** 本地运行，支持 BYOM（OpenAI、Anthropic、Gemini、DeepSeek、Qwen、Kimi、Ollama 等）；用户选择的模型和集成之外，数据留在本机。
- **\[官方宣称\]** 支持 25+ connector、任意 MCP tool、定时 automation、Slack 入口，以及文档/表格/网页等真实交付物。
- **\[可验证实现\]** `coworker/risk.py` 定义 `read`、`write_local`、`exec`、`external` 四类风险；非只读行为进入 permission engine，第三方工具可依据 metadata 的 `requires_approval` 分类。
- **\[可验证实现\]** `coworker/inbox.py` 把 approval、question、notification、directory、plan 统一成跨 session Inbox；状态为 `pending → resolved`，并以 `(session_id, tool_call_id)` 幂等，采用 first-responder-wins。
- **\[可验证实现\]** `coworker/audit.py` 使用本地 SQLite 存审计事件，对 token、secret、password、API key、正文与浏览器输入做脱敏/摘要。
- **\[可验证实现\]** `coworker/mcp/tools.py` 把 MCP schema 映射为 Agent tool，并把 `requires_approval` 送入统一风险元数据；GUI 仓库存在 approval card 与 standing approvals 的 E2E 测试。
- **\[可验证实现\]** 根许可证是标准 MIT，不是只覆盖 SDK 的局部许可证。
- **\[推断\]** OpenWorker 当前仍以单个本地 coworker/多 session 执行为中心；公开代码没有证明它提供一等多 Agent 组织、跨 Agent 任务图或 Artifact 事务。
- **\[未知\]** 25+ connector 每一个的生产 E2E、OAuth broker 的服务端实现和 SLA、跨设备同步，以及恶意 MCP server 的完整隔离能力。
**对原表的修订**
- “核心与托管 API 的开放边界需核验”可细化为：桌面与 agent 核心 MIT；README 明示唯一云组件是 connector OAuth handshake broker，broker 服务端是否同仓公开仍需单独确认。
- “高风险动作批准”已有代码证据，不只是官网截图。
**对 WanWork 的可用启示**
- 直接借鉴 risk class、action intent 审批、跨会话 Inbox、幂等 resolution 和审计脱敏。
- 继续增加组织责任、委派链、Artifact 版本和分布式 outbox/inbox；本地权限不能只靠 UI 弹窗。
### 4.10 Pi Agent：小核心与扩展优先，原表列出的能力并非内置
**当前身份与活跃度**
- 旧仓库 `badlogic/pi-mono` 现在由 GitHub API 解析到 `earendil-works/pi`；当前官方入口为 `pi.dev`。
- 根仓库是 AI agent toolkit monorepo，包含统一 LLM API、agent loop、TUI 与 coding-agent CLI。
- 本轮 GitHub 快照：最新 release `v0.84.2`（2026-08-14），MIT，2026-08-19 仍有推送。
**证据拆分**
- **\[可验证实现\]** `packages/coding-agent/docs/usage.md` 的 Design Principles 原文：Pi 刻意不内置 MCP、sub-agents、permission popups、plan mode、to-dos 或 background bash；工作流特性放在 extensions、skills、prompt templates 与 packages。
- **\[可验证实现\]** 根 README 的 Permissions & Containerization 原文：Pi 没有用于限制 filesystem、process、network 或 credential access 的内置权限系统；默认继承启动它的用户/进程权限。
- **\[可验证实现\]** 官方建议用 Gondolin extension、Docker 或 OpenShell 提供更强隔离；这证明隔离是外部部署责任，不是 Pi 核心保证。
- **\[官方宣称\]** Slack/chat automation 和 workflows 应查看独立的 `earendil-works/pi-chat`，不能把它们算进 Pi 核心 CLI。
- **\[可验证实现\]** 根 LICENSE 是标准 MIT。
- **\[推断\]** Pi 的价值在可扩展低层 loop、模型/API/TUI 与插件边界；它适合作为 WanWork runtime port，而不是协作事实源或授权边界。
- **\[未知\]** 某个具体第三方扩展是否安全实现 MCP/sub-agent/permission；不能因生态里存在扩展就说 Pi 原生具备。
**对原表的强制修订**
原表“最值得借鉴：MCP、sub-agent、权限提示、plan mode、todo”必须替换为：
> 小核心、扩展/Skill/Package 优先、统一 LLM API、agent loop 与 TUI；MCP、sub-agent、权限弹窗、plan/todo 等明确不是内置。
同时在安全部分增加：
> Pi 默认以启动者权限运行；WanWork 接入时必须由平台 sandbox、tool policy 与凭据代理提供边界。
### 4.11 Gotaa Pi.Agent：当前域名证据指向“不可用”，不能保留功能事实
**当前身份与入口**
- 语雀截图给出的 URL 是 `https://pi.gottaa.com/`。
- 本轮 `pi.gottaa.com` 无法 DNS 解析。
- `https://gottaa.com/` 只用脚本跳转到 `/lander`；lander 加载 `parking-lander` 资源并设置 GoDaddy parking 信号，不是可用产品站。
**证据拆分**
- **\[可验证实现\]** 只能验证域名当前状态：子域不存在/无法解析，根域停放。
- **\[官方宣称\]** 无可访问的当前官方产品页面，因此本轮没有可使用的官方声明。
- **\[推断\]** 产品可能已下线、更名、迁移或域名过期；四种可能都不能在现有证据中确定。
- **\[未知\]** 产品是否仍运营、当前名称、SOP/岗位/知识库/审批能力、客户、部署方式、源码与许可证。
**对原表的修订**
- 原表关于“企业岗位 Agent 平台、SOP、知识库、HR/财务岗位与审批”的描述必须降级为“语雀历史表记录，2026-08-19 无官方来源复核”。
- 不能用“关闭性/平台耦合”评价当前产品，因为连当前产品身份都无法确认。
- 后续若找到品牌迁移，应以新域名和法律主体重新建档，而不是复用旧结论。
### 4.12 CodexLoom：对象与代码真实存在，但许可证和治理边界要准确表达
**当前身份与活跃度**
- 官方仓库 `yan5xu/codexloom`；2026-08-18 仍有推送，但本轮未发现 GitHub Release。
- README 定位为“built on Codex”：不重写 agent runtime 或复制 thread history，而是在 Codex thread 上增加稳定身份、Profile、Team、有限协作和外部交付。
- 当前目标用户首先是高级个人/One Person Company Owner；README 明确说企业多租户管理和通用公司运营不是主要方向。
**证据拆分**
- **\[官方宣称\]** Agent 有稳定 ID、名称、Profile、primary thread；其他 Agent 通过 bounded Message/Topic 与显式 Artifact handoff 协作，不接管对方 primary thread。
- **\[可验证实现\]** 仓库包含 `internal/hub/artifact.go`、`topic.go`、`conversation.go`、`external_message.go`、`profile.go`、`scheduler.go` 及对应测试；CLI 有 `commands_message.go`、`commands_topic.go`；WebUI 有 NeedsYou、Topics、Messages、Schedules 和 Artifact preview。
- **\[可验证实现\]** 仓库包含 message delivery recovery、Artifact corp/preview、Topic/Artifact、Conversation 等测试，说明这些不是官网图中的纯概念对象；但本轮未运行测试。
- **\[可验证实现\]** canonical Chinese Owner Guide 明确：Organization 与 Collaboration 只是声明结构，不自动授予 repository、deployment、credential、外部发送或生产写入权限，也不强制每条 Message 路由。
- **\[可验证实现\]** Guide 还明确：silent stall 不会自动创建 Needs You，Codex tool approval 走独立 pending-approval；“没有 Needs You”不代表没有待批事项或系统健康。
- **\[可验证实现\]** 根 LICENSE 是 Elastic License 2.0，限制把软件作为托管/管理服务提供等用途；GitHub API 的 `NOASSERTION` 不改变 LICENSE 原文。
- **\[推断\]** CodexLoom 在“长期责任而非一次性 task”与外部 Interface Agent 边界上值得研究，但它深度依赖 Codex thread 连续性，不能直接充当跨 runtime 的通用事实模型。
- **\[未知\]** 在多进程高并发、跨节点部署和企业多租户下的性能/恢复；无 release 不等于不可用，但成熟度不能由 README 自证。
**对原表的修订**
- “线程团队化”成立，但需要补充其稳定 ID、Profile、Topic、Artifact 和外部 Membership 对象，不应简化成多个 terminal thread。
- “组织/协作图”不能写成 ACL 或工具权限系统。
- “开源”必须改成“源码公开、Elastic License 2.0 source-available”，避免与 Apache/MIT 混淆。
- “代码质量风险”若没有具体缺陷、复现和文件位置，不应作为事实保留；本报告只记录可验证边界与未运行项。
**对 WanWork 的可用启示**
- 长期 Agent 身份、责任 Profile、Topic 临时协作、显式 Artifact handoff、Interface Agent 是有价值的对象拆分。
- WanWork 仍应以事件、任务、授权和 Artifact 为跨 runtime 真相源，把 Codex/Pi/DeepSeek Harness thread 当外部执行上下文。
### 4.13 Coze（扣子）：托管办公产品与开源 Coze Studio 必须拆开
**当前身份与入口**
- `coze.cn` 当前 title/description 把扣子定位为 AI 办公助手一站式平台，覆盖写作、PPT、表格、设计、播客、生图、视频与办公自动化。
- `coze.cn/space-preview` 称其为基于 AI Agent 的智能办公平台，提供写作、PPT、网页开发与设计。
- 官方 `coze-dev/coze-studio` 仓库定位为一站式 AI Agent 开发平台/视觉开发工具，不等同于当前办公产品的全部前台体验。
**证据拆分**
- **\[官方宣称\]** Coze Studio 支持构建、发布和管理 Agent，配置 workflow、知识库等资源；可构建 app、workflow、plugin、database、prompt，并提供 API/Chat SDK。
- **\[可验证实现\]** `coze-dev/coze-studio` 是大型 Go + React/TypeScript 仓库，可 Docker Compose 自部署；README 说明其源于扣子开发平台，并称“core engine completely open”。
- **\[可验证实现\]** 仓库的 `LICENSE-APACHE` 是标准 Apache License 2.0；本轮 GitHub 快照最新 release 为 `v0.5.1`（2026-02-05），2026-07-29 有主分支推送。
- **\[可验证实现\]** README 对公网部署明确给出安全风险提示，包括账户注册、workflow code node 的 Python 执行、监听地址、SSRF 和部分 API 水平越权风险；这比“可自部署”营销语更值得关注。
- **\[未知\]** 本轮未找到官方当前页面把产品命名为“Coze 3.0”，也未找到可直接验证的“Agent → Team、共享上下文、人工协作”专页或对应开源状态机。
- **\[未知\]** 托管扣子办公平台的全部前后端是否来自 Coze Studio、哪些高级能力未开源、云端权限/审计/数据域实现。
- **\[推断\]** 旧表可能把扣子办公/空间、Coze Studio 开发平台与某一阶段“3.0 Agent Team”宣传混为一个产品，需要拆分研究对象。
**对原表的修订**
- “商业闭源”改为：“当前托管办公产品的完整实现未开放；Coze Studio 核心仓库 Apache-2.0，可自部署”。
- “Agent Team、长任务规划、共享记忆、人机协作”在找到当前官方对应文档前全部标“待核”。
- 开源仓库能证明 Agent/workflow/app 开发底座，不能证明托管办公产品的团队协作体验已开源。
**对 WanWork 的可用启示**
- Coze Studio 可作为低代码 Agent/workflow/RAG/plugin 资源管理参考。
- WanWork 的核心差异仍应是人和多个 Agent 的责任、任务、Artifact、授权与恢复，而不是再造一个工作流画布。
### 4.14 OpenAgents：当前已是完整 Workspace/Launcher/Network SDK，不只是协议实验
**当前身份与活跃度**
- 官方仓库 `openagents-org/openagents`，默认分支 `develop`，Apache-2.0。
- 本轮 GitHub 快照：最新 release `launcher-v0.9.15`（2026-08-18），2026-08-19 仍有推送。
- 当前 README 使用“OpenAgents Workspace — The Collaborative OS for Agents”，并把产品分为 Workspace、Launcher、Network SDK；旧 Studio 代码仍在仓库，但已不是唯一产品表述。
**证据拆分**
- **\[官方宣称\]** Workspace 把不同机器上的 Claude Code、Codex CLI、Cursor、OpenClaw、Hermes、OpenCode 等放入统一 URL，支持线程、`@mention`、共享文件、共享浏览器和 tunnel。
- **\[官方宣称\]** Launcher 提供 runtime 安装、实例配置、凭据配置、后台 daemon 与 workspace 连接；README 当前列出 10+ supported/preview runtime。
- **\[可验证实现\]** 仓库同时包含 Python SDK、Workspace 前后端、Launcher packages、legacy/current Studio、registry、tests 与 Docker 部署，不是只有 landing page。
- **\[可验证实现\]** `sdk/src/openagents/core/transports/a2a.py`、`a2a_registry.py`、`connectors/a2a_connector.py`、A2A task store、converter 和 examples 证明 A2A 不只是 README 兼容性口号。
- **\[可验证实现\]** README 对 adapter 成熟度主动分级：DeepSeek Harness 为 Preview，Aider/Goose 为 Beta；Aider/Goose 的真实 provider E2E 仍明确写 pending，不能把“支持”一概解释成生产验证。
- **\[可验证实现\]** Goose adapter 的官方说明披露重要边界：headless 模式把 approval 模式强制为 `auto`，工作目录不是硬 sandbox；文件写入不会因 stop 自动回滚。
- **\[可验证实现\]** 根 LICENSE 是标准 Apache-2.0。
- **\[推断\]** OpenAgents 已经比旧表所写的“开放 Agent 网络基础设施”更上层，实际包含可用协作工作区；但任务验收、组织授权、Artifact 版本和企业审计仍不是它当前最强的公开对象。
- **\[未知\]** 公共 workspace 托管层与仓库的精确版本映射、企业租户隔离/SLA、共享浏览器的安全隔离，以及所有 runtime adapter 的真实 E2E 覆盖。
**对原表的修订**
- “Studio、协议兼容”可保留，但要更新为当前三层产品：Workspace、Launcher、Network SDK；Studio 是仓库中的一部分/历史命名，而非完整当前定位。
- “更偏基础设施”不再完全准确：Workspace 已提供面向人的实时协作产品面。
- 必须保留各 adapter 的成熟度标签和安全边界，不能把 Preview/Beta 写成稳定支持。
**对 WanWork 的可用启示**
- 可参考其 runtime connector、统一 workspace 地址、跨机器 daemon、共享文件/浏览器和 A2A adapter。
- WanWork 需要补上 action-level approval、结构化 task/handoff、版本化 Artifact、组织策略和恢复语义；外部 runtime 连接后也不能默认获得用户权限。
## 5. 开放性与许可证专项结论
### 5.1 公开仓库快照
以下快照来自 GitHub 官方 API，访问日均为 2026-08-19。Stars 等易变指标不作为能力证据，因此不纳入结论。

| 产品/组件 | 官方仓库 | 本轮可见版本/活跃度 | LICENSE 原文 | 能覆盖的范围 |
| --- | --- | --- | --- | --- |
| 千问 AI Skills | `QianWen-AI/qianwen-ai` | 2026-08-13 有提交 | Apache-2.0 | Skills 源码，不覆盖托管模型平台 |
| 千问 CLI | `QianWen-AI/qianwen-cli` | 2026-08-13 有提交 | Apache-2.0 | CLI，不覆盖 SaaS 服务端 |
| Multica | `multica-ai/multica` | `v0.4.30`，2026-08-19 | 自定义 Multica License | 产品 monorepo；有托管/嵌入/品牌限制 |
| NEAR Market SDK | `nearai/agents-market-sdk` | 2026-05-04 有提交，无 Release | MIT | 集成 SDK/UI，不覆盖 Market 后端 |
| Raft Docs | `botiverse/raft-docs` | 2026-08-19 有提交 | GitHub API 未识别 | 公开文档，不覆盖产品核心 |
| OpenWorker | `andrewyng/openworker` | `v0.1.7`，2026-08-19 更新 | MIT | 桌面 UI、agent backend、connectors 等仓库代码 |
| Pi | `earendil-works/pi` | `v0.84.2`，2026-08-19 | MIT | toolkit/CLI monorepo |
| CodexLoom | `yan5xu/codexloom` | 2026-08-18 有提交，无 Release | Elastic License 2.0 | 产品仓库，source-available，非 OSI 开源 |
| Coze Studio | `coze-dev/coze-studio` | `v0.5.1`；2026-07-29 有提交 | Apache-2.0 | Agent 开发平台核心，不等于托管办公产品全量 |
| OpenAgents | `openagents-org/openagents` | `launcher-v0.9.15`，2026-08-19 | Apache-2.0 | Workspace/Launcher/Network SDK 主仓库 |


### 5.2 不能再使用的模糊说法
- 不说“GitHub 上有代码，所以产品开源”，而说清仓库、许可证和覆盖范围。
- 不说“Apache-based”，而要看是否加入额外限制。Multica 就是典型反例。
- 不说“协议 MIT”，除非能找到可下载规范、版本、LICENSE 或兼容测试；FloatIM 当前只有官网声明。
- 不说“自托管 = 可用于商业 SaaS”。许可证可能允许内部部署但限制对外托管。
- 不说“云产品闭源，所以公司没有开源能力”。Coze 托管产品与 Coze Studio 必须分别评价。
## 6. 对 `04_competitor_landscape.md` 的逐项修订清单

| 原表位置/说法 | 修订动作 | 修订后的安全表述 |
| --- | --- | --- |
| Pi：MCP、sub-agent、权限提示、plan、todo | **事实错误，必须改** | Pi 刻意保持小核心，这些均非内置，可由扩展/外部工具实现 |
| Pi：权限能力可借鉴 | **反向补充风险** | 默认继承启动者 filesystem/process/network/credential 权限，应由外部 sandbox 和平台授权收口 |
| Multica：“开源” | **加许可证限定** | 源码公开、自托管；自定义许可证限制第三方托管、商业嵌入和品牌移除 |
| Multica：22 个 agent/tool | **更新数字** | 当前 README 列 20 个 Agent CLI；数字随版本变化，引用时带日期 |
| Todos：许可证来源待核 | **升级为明确结论** | 未见产品源码许可证；Terms 说明服务软件/设计归运营方或许可方 |
| Todos：依赖触发工作树 | **保守降级** | 分支并行、plan/diff、review 有官方文档；精确 DAG 依赖语义仍待核 |
| NEAR Market：TEE | **拆分母平台与产品** | Market 的任务/托管/争议由 API 文档确认；TEE 只确认 NEAR AI 基础设施声明 |
| Slock | **更名** | Raft（原 Slock），旧域名已重定向 |
| Mindra：RBAC/SOC2/GDPR 待来源 | **来源已找到** | 官方 Security 页面明确声明，但未做独立报告/源码验证 |
| OpenWorker：审批 | **从宣传升级为实现证据** | risk class、Inbox、idempotent resolve、audit redaction 可定位到 MIT 源码 |
| Gotaa Pi.Agent：岗位/SOP/审批 | **全部降级** | 历史语雀表记录；当前子域失效、根域停放，产品状态未知 |
| CodexLoom：组织图即治理 | **加边界** | Organization/Collaboration 是声明结构，不自动授予权限或强制消息路由 |
| CodexLoom：开源 | **改为 source-available** | Elastic License 2.0，非 OSI 开源 |
| Coze 3.0：商业闭源 | **拆成两层** | 托管办公产品完整实现未公开；Coze Studio 核心 Apache-2.0 |
| Coze 3.0：Agent Team 已成立 | **等待官方对应页** | 当前只验证到办公平台与 Studio 的 Agent/workflow/app 能力 |
| OpenAgents：只是基础设施 | **更新产品边界** | 当前已有 Workspace + Launcher + Network SDK；仍缺企业治理/验收上层 |
| FloatIM：开放协议 | **加证据等级** | 官网声称 IACT/Selfware MIT，未找到规范仓库/LICENSE，不能说产品开源 |


## 7. 对 WanWork 竞品决策的直接影响
### 7.1 已被市场反复验证的能力
至少三个相互独立产品都在强化以下方向，因此不应再视为可选装饰：
1. **Agent 一等身份与群聊/线程入口**：FloatIM、Raft、OpenAgents、CodexLoom。
2. **本地/自有机器 runtime**：FloatIM、Raft、Multica、Todos、OpenWorker、OpenAgents。
3. **可见任务运行记录和 review gate**：Multica、Todos、OpenWorker、NEAR Market。
4. **模型/runtime 可替换**：千问组件、Multica、Todos、OpenWorker、OpenAgents。
5. **人类注意力队列**：Multica Inbox、Todos Inbox、OpenWorker Inbox、CodexLoom Needs You、Mindra approvals。
6. **外部协议与工具生态**：MCP、A2A、Agent Skills 在多家产品中各自扮演不同层次角色。
### 7.2 仍然没有竞品同时做好之处
基于可验证公开资料，仍未看到一个产品同时提供：
- 群聊中的独立 Agent 身份；
- 版本化、可重规划的确定性任务图；
- 原子 task + Artifact 提交和冲突处理；
- action intent 级授权、审批、执行回读；
- 委派责任链和组织级审计；
- 跨 runtime/harness 的统一恢复语义；
- MCP/A2A/Agent Skills 外部互操作；
- 本地、企业私有化和 SaaS 的一致对象模型。
这仍是 WanWork 的组合机会，但“没有竞品做”不能只靠竞品首页下结论；MVP 必须选择可验证、可跑通的一个高价值闭环。
### 7.3 最值得直接参考的实现映射

| WanWork 子系统 | 第一参考 | 第二参考 | 不应照搬的部分 |
| --- | --- | --- | --- |
| Task/run 状态、lease/retry | Multica | Todos 文档 | Coding issue/branch 作为唯一 Artifact |
| 本地 runtime 与多 CLI adapter | OpenAgents | Multica | 默认把外部 CLI 的权限带入平台 |
| Action approval / Inbox | OpenWorker | Mindra 产品体验 | 粗粒度“信任模式”或只做 UI 弹窗 |
| Agent 长期身份与责任 | CodexLoom | Raft | 把 thread/process 当业务身份本身 |
| 群聊一等 Agent UX | FloatIM | OpenAgents/Raft | 用聊天 history 当唯一状态源 |
| 交付验收/争议 | NEAR Market | Todos review | 把代币与支付做成内部协作前置依赖 |
| 模型/技能供给适配 | 千问 Skills/CLI | Pi extensions | 让 provider/runtime 内部 todo 成为平台真相 |
| 低代码 Agent/workflow 资源 | Coze Studio | Mindra integrations | 再造一个只有 canvas 的自动化平台 |


## 8. 未解决问题与下一轮实测优先级
### P0：会改变架构或许可证选择
1. **Multica**：在隔离环境启动 self-host，实际验证 claim/start/heartbeat/fail/retry/orphan recovery；法务确认自定义许可证是否允许内部参考、修改和未来 SaaS 产品使用。
2. **OpenWorker**：运行 approval/inbox/audit 测试，验证“批准的 action intent”在参数变化后是否重新审批，检查 MCP tool 默认分类是否 fail-closed。
3. **OpenAgents**：自托管 Workspace，接入两个不同 runtime，验证 channel session 隔离、断线恢复、共享文件冲突、Stop 的进程树清理和 A2A task store。
4. **Coze Studio**：启动开源版，比较与当前扣子办公产品的能力差集，避免把云端能力错误归给开源仓库。
### P1：会改变产品交互设计
1. **Todos**：真实走通 goal → Chief 拆解 → plan review → parallel branches → AI/human review → merge，记录每一步的状态与失败恢复。
2. **Raft/FloatIM**：注册体验群聊、Agent 邀请、临时组队、人工 check-in 和本地 daemon；观察群消息与任务/产物是否有结构化关联。
3. **Mindra**：申请 security/trust 文档，核验 RBAC 资源粒度、审计导出、审批回读和“3,000+ 集成”的实现类型。
4. **CodexLoom**：运行仓库测试与最小多 Agent demo，重点验证 message delivery recovery、Artifact handoff、Needs You 与外部发送之间的授权边界。
### P2：持续追踪
1. **YouMind**：主站恢复后记录 Board、引用、成品版本和 Agent 身份；确认中文域名与主域法律/账号关系。
2. **Gotaa Pi.Agent**：只在出现新的官方域名、公司公告或仓库后重启研究；不要围绕停放域名继续猜测。
3. **FloatIM IACT/Selfware**：等待公开规范、版本和 LICENSE；没有机器可读 schema/测试向量前，不作为 WanWork 协议依赖。
4. **Coze “3.0”**：寻找官方发布日期/版本说明；若只是历史营销名称，应从当前竞品名中移除版本号。
## 9. 官方来源台账
> 下列页面和仓库均在 2026-08-19 访问或通过 GitHub API 核对。GitHub blob 链接优先固定到本轮观察到的提交，避免主分支后续变化造成证据漂移。
### 9.1 YouMind
- [YouMind 主站](https://youmind.com/) — 本轮连接超时，仅记录可达性失败。
- [YouMind 中文站](https://youmindchina.com/zh-CN) — title、description、canonical/OG 与组织结构化数据。
- [语雀竞品表截图：overview](../screenshots/04_yuque_multi_agent_overview.jpeg) — 二手需求来源，不作为实现证明。
### 9.2 千问AI
- [千问AI 官网](https://www.qianwenai.com/) — 模型矩阵、原生应用、API/Agent 能力与运营入口。
- [QianWen-AI GitHub 组织](https://github.com/QianWen-AI) — 官方公开组件清单。
- [QianWen AI Skills README（固定提交）](https://github.com/QianWen-AI/qianwen-ai/blob/214e6468257f579f634a16264e1b6132e777f9a7/README.md) — Skills 功能与 Apache-2.0 声明。
- [QianWen CLI README（固定提交）](https://github.com/QianWen-AI/qianwen-cli/blob/5023d508032eaa217080a0970ed63d0914610f26/README.md) — JSON、退出码、用量/账单/Skill 命令。
### 9.3 FloatIM
- [FloatIM 官方页](https://floatboat.ai/floatim) — First-Class Agents、本地运行、自组织团队、IACT/Selfware 声明。
- [Floatboat 隐私政策](https://floatboat.ai/privacy) — 产品法律/隐私入口；不证明协议开源。
### 9.4 Multica
- [Multica 官方仓库](https://github.com/multica-ai/multica) — monorepo 与 release。
- [Tasks 文档（固定提交）](https://github.com/multica-ai/multica/blob/c1d07a1ed63136bdcc25f6e231e18dc854259bdb/apps/docs/content/docs/tasks.mdx) — task/issue 分离、状态、重试与超时。
- [Task lifecycle handler（固定提交）](https://github.com/multica-ai/multica/blob/c1d07a1ed63136bdcc25f6e231e18dc854259bdb/server/internal/handler/task_lifecycle.go) — orphan recovery、session pin、rerun。
- [Lease/retry migration（固定提交）](https://github.com/multica-ai/multica/blob/c1d07a1ed63136bdcc25f6e231e18dc854259bdb/server/migrations/055_task_lease_and_retry.up.sql) — attempt/lease/failure 数据结构。
- [Multica License（固定提交）](https://github.com/multica-ai/multica/blob/c1d07a1ed63136bdcc25f6e231e18dc854259bdb/LICENSE) — 第三方托管、嵌入、品牌与归因限制。
- [Self-hosting 文档](https://github.com/multica-ai/multica/blob/main/SELF_HOSTING.md) — Docker/自托管入口。
### 9.5 Todos
- [Todos 官网](https://todos.dev/) — Agent Team、Chief、并行分支与 live preview。
- [Todos Docs](https://todos.dev/docs) — Todo、Chief、Agent、plan/diff、AI review、Machine、MCP、权限。
- [Todos Changelog](https://todos.dev/docs/changelog) — 运行行为与持续更新证据。
- [Todos Terms](https://todos.dev/terms) — 服务边界、软件所有权、自托管/托管机器与仓库条款。
### 9.6 NEAR AI Agent Market
- [NEAR AI 官网](https://near.ai/) — confidential AI/enclave 当前定位。
- [NEAR Agent Market](https://market.near.ai/) — 市场入口。
- [Agent Market Skill/API Guide](https://market.near.ai/skill.md) — job/bid/escrow/submit/accept/dispute 状态与 API。
- [Agent Market OpenAPI](https://market.near.ai/openapi.json) — 生产接口 schema；本报告未整份载入，仅依据操作说明核对入口。
- [Agents Market SDK README（固定提交）](https://github.com/nearai/agents-market-sdk/blob/d6465adcae05abf617498990b366ba3f44da8e6f/README.md) — SDK packages 与集成流程。
- [Agents Market SDK LICENSE（固定提交）](https://github.com/nearai/agents-market-sdk/blob/d6465adcae05abf617498990b366ba3f44da8e6f/LICENSE) — MIT。
### 9.7 Raft（原 Slock）
- [旧 Slock 域名](https://slock.ai/) — 当前重定向到 Raft。
- [Raft 官网](https://raft.build/) — Chat workspace、long-running agents、本地 daemon。
- [Raft Docs](https://docs.raft.build/welcome/) — 持久身份、记忆、人类 steer 与 onboarding。
- [Botiverse GitHub 组织](https://github.com/botiverse) — Raft docs、external agent 等公开边界；未见完整产品核心。
- [Raft Docs 固定提交](https://github.com/botiverse/raft-docs/tree/1f0698e3d5ebef19524faa89b6579671367f1de3) — 文档源码快照。
### 9.8 Mindra
- [Mindra 官网](https://mindra.co/) — AI employee team、3,000+ integrations、工作与审批叙事。
- [Mindra Security](https://mindra.co/security) — RBAC/SSO、audit、HITL、policy、SOC 2/GDPR/ZDR 声明。
- [Mindra Integrations](https://mindra.co/integrations) — 集成入口；数量为厂商声明。
### 9.9 OpenWorker
- [OpenWorker 官网](https://openworker.com/) — 产品入口。
- [OpenWorker README（固定提交）](https://github.com/andrewyng/openworker/blob/fc3aa28d9f205c9928ff8d9ecd9638d86fda59be/README.md) — beta、本地优先、BYOM、connector、approval。
- [Risk classes（固定提交）](https://github.com/andrewyng/openworker/blob/fc3aa28d9f205c9928ff8d9ecd9638d86fda59be/coworker/risk.py) — read/write/exec/external 分类。
- [Inbox（固定提交）](https://github.com/andrewyng/openworker/blob/fc3aa28d9f205c9928ff8d9ecd9638d86fda59be/coworker/inbox.py) — 状态、幂等和跨 session attention queue。
- [Audit store（固定提交）](https://github.com/andrewyng/openworker/blob/fc3aa28d9f205c9928ff8d9ecd9638d86fda59be/coworker/audit.py) — SQLite 审计与脱敏。
- [MCP tools（固定提交）](https://github.com/andrewyng/openworker/blob/fc3aa28d9f205c9928ff8d9ecd9638d86fda59be/coworker/mcp/tools.py) — schema/approval metadata 映射。
- [OpenWorker LICENSE（固定提交）](https://github.com/andrewyng/openworker/blob/fc3aa28d9f205c9928ff8d9ecd9638d86fda59be/LICENSE) — MIT。
### 9.10 Pi Agent
- [Pi 官网](https://pi.dev/) — 当前产品入口。
- [Pi 官方仓库](https://github.com/earendil-works/pi) — toolkit monorepo 与 releases。
- [Pi Usage / Design Principles（固定提交）](https://github.com/earendil-works/pi/blob/d57e531f5dc57974e2f9ce7a618730e5358a45db/packages/coding-agent/docs/usage.md) — 明确列出非内置能力。
- [Pi README / Permissions（固定提交）](https://github.com/earendil-works/pi/blob/d57e531f5dc57974e2f9ce7a618730e5358a45db/README.md) — 默认权限与容器化建议。
- [Pi LICENSE（固定提交）](https://github.com/earendil-works/pi/blob/d57e531f5dc57974e2f9ce7a618730e5358a45db/LICENSE) — MIT。
### 9.11 Gotaa Pi.Agent
- [历史产品 URL](https://pi.gottaa.com/) — 2026-08-19 DNS 无法解析。
- [Gotaa 根域](https://gottaa.com/) — 当前跳转 GoDaddy parking lander。
- [语雀竞品表截图：rows 7-11](../screenshots/06_yuque_products_rows_7_11.jpeg) — 历史二手记录，不能证明当前功能。
### 9.12 CodexLoom
- [CodexLoom 中文官网](https://codexloom.ai/zh-cn/) — 当前产品定位。
- [CodexLoom README（固定提交）](https://github.com/yan5xu/codexloom/blob/cc5b0dec22313d8fc1519ff47b5ab779baf06296/README.md) — 长期 Agent、Message/Topic/Artifact 与外部边界。
- [Canonical Chinese Owner Guide（固定提交）](https://github.com/yan5xu/codexloom/blob/cc5b0dec22313d8fc1519ff47b5ab779baf06296/docs/owner-guide.zh-CN.md) — 权限/关系/Needs You 的明确边界。
- [Artifact hub implementation（固定提交）](https://github.com/yan5xu/codexloom/blob/cc5b0dec22313d8fc1519ff47b5ab779baf06296/internal/hub/artifact.go) — Artifact 对象代码。
- [Topic hub implementation（固定提交）](https://github.com/yan5xu/codexloom/blob/cc5b0dec22313d8fc1519ff47b5ab779baf06296/internal/hub/topic.go) — Topic 对象代码。
- [CodexLoom LICENSE（固定提交）](https://github.com/yan5xu/codexloom/blob/cc5b0dec22313d8fc1519ff47b5ab779baf06296/LICENSE) — Elastic License 2.0。
### 9.13 Coze（扣子）
- [扣子官网](https://www.coze.cn/) — 当前 AI 办公助手平台定位。
- [扣子 Space Preview](https://www.coze.cn/space-preview) — Agent 智能办公平台定位。
- [Coze Studio 官方仓库](https://github.com/coze-dev/coze-studio) — 开源 Agent 开发平台核心。
- [Coze Studio README（固定提交）](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/README.md) — Agent/workflow/app、架构、自部署与安全风险。
- [Coze Studio Apache License（固定提交）](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/LICENSE-APACHE) — Apache-2.0。
### 9.14 OpenAgents
- [OpenAgents 官网](https://openagents.org/) — 当前 Workspace 入口。
- [OpenAgents README（固定提交）](https://github.com/openagents-org/openagents/blob/bd0e4e5e2cc49c446b01fa2c5b3b71567de7d9e5/README.md) — Workspace、Launcher、Network SDK、runtime 成熟度与安全边界。
- [A2A transport（固定提交）](https://github.com/openagents-org/openagents/blob/bd0e4e5e2cc49c446b01fa2c5b3b71567de7d9e5/sdk/src/openagents/core/transports/a2a.py) — A2A transport 代码。
- [A2A registry（固定提交）](https://github.com/openagents-org/openagents/blob/bd0e4e5e2cc49c446b01fa2c5b3b71567de7d9e5/sdk/src/openagents/core/a2a_registry.py) — A2A 注册/发现相关代码。
- [OpenAgents Studio concept（固定提交）](https://github.com/openagents-org/openagents/blob/bd0e4e5e2cc49c446b01fa2c5b3b71567de7d9e5/docs/concepts/openagents-studio.mdx) — Studio 仍存在于仓库，但不代表全部当前产品。
- [OpenAgents LICENSE（固定提交）](https://github.com/openagents-org/openagents/blob/bd0e4e5e2cc49c446b01fa2c5b3b71567de7d9e5/LICENSE) — Apache-2.0。
## 10. 可复核性声明
本报告刻意遵守以下边界：
- 未把官网宣传写成已跑通实现；
- 未把 SDK/协议仓库写成产品后端开源；
- 未把存在测试文件写成测试已由本轮执行通过；
- 未把域名失效写成公司或产品已经终止；
- 未把未找到功能写成功能一定不存在；
- 未把 GitHub stars、营销客户 Logo 或厂商自报合规直接当成产品质量证据；
- 未使用飞书/企微发送消息或询问任何人；
- 未记录、输出或引用任何 API Key/凭据。
因此，这份报告适合作为竞品事实底稿与下一轮实测清单，不应替代法律许可证意见、安全审计或商业尽调。

---

来源：https://app.notion.com/p/3c1ead4b996e81f9b5eddebebc96d30a?pvs=204

