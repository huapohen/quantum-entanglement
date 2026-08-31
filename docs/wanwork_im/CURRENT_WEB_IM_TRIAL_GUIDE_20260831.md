# Quantum Entanglement v0版 Web IM 当前阶段体验教程

> 适用版本：`dev_wanwork_quantum_entanglement`，HEAD `f4c4dba`（2026-08-31）  
> 目标：在本机浏览器中体验当前 Web-first vertical slice，并核验 Agent Store、普通群聊、Agent 子群、动态协作指令、Workboard 审阅和产物发布。

这是一套可运行的本地验收台，不是生产 IM。默认 synthetic 模式完全零外网，不连接飞书、企微或真实融云，也不会给任何人、群聊、机器人或 webhook 发消息。

## 0. 先确认你启动的是当前阶段

当前可验收实现位于开发 worktree，不在尚未合并的 `main`：

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/dev_wanwork_quantum_entanglement
git branch --show-current
git rev-parse --short HEAD
```

预期输出：

```text
dev_wanwork_quantum_entanglement
f4c4dba
```

如果你看到 `main` 或更早的提交，请切回上面的 worktree；不要为了体验而合并分支。当前开发分支已经推送到远端 `origin/dev_wanwork_quantum_entanglement`。

## 1. 环境要求

需要以下命令可用：

```bash
go version       # Go，用于本地 IM API
node --version   # Node.js，建议使用项目现有兼容版本
npm --version
curl --version
```

首次启动会在 `clients/im-web` 执行 `npm ci --ignore-scripts`，并触发 Go 首次编译，可能需要一两分钟。依赖和编译缓存都在本机处理，不会上传到飞书、企微或 Notion。

## 2. 推荐：一条命令启动 Web IM

在当前开发 worktree 根目录执行：

```bash
./scripts/start_web_client.sh --no-open
```

脚本会同时启动：

- Go fake IM API：默认 `127.0.0.1:18080`；
- React/Vite Web：默认 `127.0.0.1:5173`；
- Vite 的 `/api` 代理会自动指向当前 IM API 端口。

打开终端打印的地址，默认是：

```text
http://127.0.0.1:5173
```

macOS 也可以省略 `--no-open`，脚本会尝试自动打开浏览器。停止体验时，回到这个启动终端按 `Ctrl-C`；前后端会一起退出，内存中的 fake 数据也会随进程清空。

### 2.1 端口被占用时

同时改 IM API 和 Web 端口，脚本会自动更新代理：

```bash
./scripts/start_web_client.sh \
  --im-port 19080 \
  --web-port 5174 \
  --no-open
```

然后打开 `http://127.0.0.1:5174`。查看全部参数：

```bash
./scripts/start_web_client.sh --help
```

### 2.2 已安装依赖时跳过 npm 安装

```bash
./scripts/start_web_client.sh --no-install --no-open
```

如果报 `未找到 node_modules`，去掉 `--no-install` 让脚本自动执行 `npm ci`。

## 3. 手机、iPad、Android、鸿蒙浏览器体验

当前手机和平板交付形态是浏览器/PWA，不是原生 `.ipa`、`.apk`、`.hap`。让设备和开发机连到同一 Wi-Fi，在开发机执行：

```bash
./scripts/start_web_client.sh --lan --no-open
```

脚本会尽量打印类似下面的局域网地址：

```text
http://192.168.1.23:5173
```

用手机或平板浏览器打开该地址即可。建议先用桌面浏览器完成一次启动，再用移动设备验证窄屏布局和同一套群聊数据。

如果地址显示 `<本机局域网IP>`，在 macOS 可用下面的命令找地址：

```bash
ipconfig getifaddr en0
ipconfig getifaddr en1
```

设备无法访问时依次检查：

1. 两台设备是否在同一个 Wi-Fi，且 Wi-Fi 没有开启客户端隔离；
2. 系统防火墙是否允许 Vite 端口 `5173`（或你自定义的 Web 端口）；
3. URL 是否使用开发机局域网 IP，而不是 `127.0.0.1`；
4. 启动终端是否仍在运行。

不要把 `--lan` 服务暴露到公网。Go API 仍只绑定本机回环，移动设备通过 Vite 代理访问。

## 4. 页面布局与第一次检查

页面分为三列，窄屏会变成纵向布局：

| 区域 | 用途 | 你应看到的内容 |
| --- | --- | --- |
| 左侧工作空间 | 会话列表、建群、单聊、会话筛选 | 一个演示父群、当前身份和 Agent Store 摘要 |
| 中间会话 | 消息、搜索、编辑、撤回 | 当前群消息；选择 Agent 子群时会显示 `Agent 子群` 标签 |
| 右侧工作台 | Workboard、Agent Store、Mention Router、Runtime | 任务/产物/待确认、Agent 安装授权、动态指令和网络安全提示 |

启动完成后，先确认右下角 Runtime 显示 `READY`，并确认：

- `zero-network fake`；
- `network calls` 为 `0`；
- Agent runtime 为 `synthetic`；
- Agent Store 中有已安装的 `v0版研究 Agent`，以及可安装的 `v0版规划 Agent`。

## 5. 10 分钟完整验收路径

以下步骤可以只用浏览器完成，顺序建议保持不变。

### 5.1 创建普通群

1. 在左侧“新建群聊”输入框输入任意群名，例如“Web IM 体验群”。
2. 保持“创建时邀请已安装 Agent”勾选，点击 `+`。
3. 新群会出现在左侧并自动选中；该群是父群，成员投影中包含已安装 Agent。

如果想验证真人普通群边界，取消该勾选后再创建一个群。也可以点击“单聊”创建与当前已安装 Agent 的单聊。`@Agent` 子群不是普通群，不能从子群再次创建子群。

### 5.2 发送普通消息、编辑、撤回和搜索

在中间输入框发送任意文本，点击 `发送`；也可使用 `⌘/Ctrl + Enter`。自己的消息下方会出现：

- `编辑`：修改文本并重新加载消息投影；
- `撤回`：消息变成“（已撤回）”；
- 顶部“搜索当前会话”：只搜索当前会话的消息。

这一步验证的是普通 IM 消息投影，不会触发 Agent。

### 5.3 在 Agent Store 查看版本和最小权限安装

右侧 Agent Store 卡片展示 definition、release、版本 provenance、artifact/manifest/persona digest、Trust Passport、数据路线、安装状态和已授权能力。

1. 找到 `installationStatus=available` 的 `v0版规划 Agent`。
2. 安装前可取消某些能力；建议第一次只保留 `conversation.read`，演示最小权限。
3. 点击 `安装到当前工作空间`。
4. 页面提示应显示“已安装到当前工作空间”、命令状态 `已提交`，且已授权能力只包含你勾选的集合。
5. 再次刷新或重新请求 Agent Store，确认该 Agent 变成 `active`，并且父群可邀请它。

安装动作绑定幂等 key。页面每次点击会生成新的 key；同一个 key 的重复请求不会创建第二个安装。客户端勾选框只是请求，后端仍会以 Trust Passport 的 `requestedCapabilities` 为上限，不能通过 UI 越权。

### 5.4 把已安装 Agent 邀请进当前群

选中一个普通群，在 Agent Store 的 active 卡片点击 `邀请到当前群`。成功后会显示“已邀请 …”；重复邀请不会产生重复成员。创建群时已勾选 Agent 的群可以跳过这一步。

### 5.5 发布任意自定义协作指令

确认当前选中的是含 Agent 的普通群，然后在右侧“发布协作指令”文本框输入你自己的任务，例如：

```text
比较三种团队知识库方案，给出适用场景、风险、验证步骤和推荐结论。
```

这里不是固定示例，可以替换为任意具体指令；建议包含目标、约束和期望输出格式。点击 `@v0版 Agent` 后检查：

1. 页面自动切换到新建的 Agent 子群；
2. 右侧结果卡显示 `COMMITTED`、子群 ID 和 Invocation ID；
3. 中间消息区显示 Agent 回复，且回复只出现在子群；
4. 左侧会话列表同时保留原父群；
5. 回到父群，只能看到受限 work-card（子群 ID、invocation、Agent ID、状态等），看不到完整 prompt、Agent 回复、Artifact 内容或子群 ACL；
6. 再换一条指令，会生成另一个与父群关联的子群。

核心隔离不变量是：`agentReply.conversationId == childConversationId`，且不等于父群 ID。父群和子群是两个独立权限边界，不是把父群消息复制给 Agent。

### 5.6 在 Workboard 审阅任务和 Artifact

发布指令后，右侧 Workboard 会出现一张任务卡：

- Task 初始状态：`waiting_for_review`；
- Artifact 初始状态：`draft`；
- Needs You 初始状态：`open`。

先阅读草稿内容，再选择：

- `接受产物`：Needs You 变为 `resolved`，Artifact 变为 `accepted`，Task 变为 `completed`；
- `退回`：Needs You 变为 `resolved`，Artifact 和 Task 变为 `rejected`，不会把草稿当作正式结论。

接受后，任务卡会出现 `发布引用到父群`。点击它只会向父群发布 digest 绑定的 Artifact 引用，不会把未经审阅的完整草稿伪装成普通消息。再次点击同一按钮应显示幂等重放，父群只保留一条引用。

### 5.7 验证停用和撤权（可选）

在 active Agent 卡片选择“停用后的数据处置”：

- `归档`（推荐）：保留历史数据但结束当前安装；
- `保留`：继续留存历史数据；
- `删除`：删除历史数据，不可恢复。

点击 `停用并撤权` 并确认。三种策略都会先撤销 Agent 身份、移出父群/子群成员投影；选择项只决定历史数据处置。撤权后再发布 `@v0版 Agent` 指令应被拒绝，不会创建新的 invocation、子群或消息。第一次体验建议用“归档”，避免误删。

## 6. 用 curl 做黑盒复核

浏览器使用的是同一组 API。另开终端，先定义地址和公开 demo fixture token：

```bash
API=http://127.0.0.1:18080
AUTH='Authorization: Bearer demo.local.signature'
```

`demo.local.signature` 是代码内公开的合成 fixture，不是 API Key，也不能访问外部系统。

### 6.1 健康、运行快照和 Agent Store

```bash
curl --fail "$API/health/live"
curl --fail -H "$AUTH" "$API/api/v1/demo/im" | python3 -m json.tool
curl --fail -H "$AUTH" "$API/api/v1/demo/im/agents" | python3 -m json.tool
```

快照应包含：`mode=zero-network-fake`、`networkCalls=0`、fake auth/provider 标识和 `agentRuntime.mode=synthetic`。Agent Store 应同时有 active Agent 和可安装 Agent，并区分 `requestedCapabilities` 与 `grantedCapabilities`。

### 6.2 发送动态指令并拿到 ID

下面的指令只是可复制的起点，替换 `instruction` 即可；`messageId` 每次应唯一：

```bash
curl --fail -H 'Content-Type: application/json' -H "$AUTH" \
  --data '{
    "conversationId":"cnv_local_demo_parent",
    "messageId":"msg_manual_web_001",
    "instruction":"为一个五人团队设计 Agent 协作会议流程，输出步骤、权限边界和验收指标"
  }' \
  "$API/api/v1/demo/im/mentions" | python3 -m json.tool
```

成功时检查：

- HTTP status 为 `200`；
- envelope `code=200`；
- `parentConversationId` 为请求的父群；
- `childConversationId` 以 `cnv_at_` 开头；
- `invocationId` 以 `inv_at_` 开头；
- `agentReply.conversationId` 等于子群 ID且不等于父群 ID；
- 有 `taskId`、`artifactId`、`needsYouId`；
- 首次 `providerStatus=committed`。

同一个 `messageId + instruction` 再发一次，响应仍为 HTTP 200，但 `data.replayed=true`；同一个 `messageId` 改写 instruction，则仍为 HTTP 200，业务 `code=40902`。这里的 HTTP 200 是统一 envelope 约定，不表示业务动作一定成功。

### 6.3 查询 Workboard

```bash
curl --fail -H "$AUTH" "$API/api/v1/demo/im/tasks" | python3 -m json.tool
curl --fail -H "$AUTH" "$API/api/v1/demo/im/artifacts" | python3 -m json.tool
curl --fail -H "$AUTH" "$API/api/v1/demo/im/needs-you" | python3 -m json.tool
```

从 `needsYou[0].id` 复制 ID 后接受产物（也可以把 `accept` 改为 `reject`）：

```bash
NEEDS_ID='把上一步返回的 needs_local_... 填在这里'
curl --fail -H 'Content-Type: application/json' -H "$AUTH" \
  --data '{"decision":"accept"}' \
  "$API/api/v1/demo/im/needs-you/$NEEDS_ID/resolve" | python3 -m json.tool
```

从响应中复制 `data.artifact.id`，发布父群引用：

```bash
ARTIFACT_ID='把上一步返回的 artifact_local_... 填在这里'
curl --fail -H 'Content-Type: application/json' -H "$AUTH" \
  --data '{}' \
  "$API/api/v1/demo/im/artifacts/$ARTIFACT_ID/publish" | python3 -m json.tool
```

发布响应中的消息应回到 `cnv_local_demo_parent`，`extInfo` 含 `artifact_reference`；重复发布应返回 `replayed=true`。

### 6.4 查看会话和消息隔离

```bash
curl --fail -H "$AUTH" "$API/api/v1/demo/im/conversations?limit=50" | python3 -m json.tool
PARENT_ID=cnv_local_demo_parent
curl --fail -H "$AUTH" "$API/api/v1/demo/im/conversations/$PARENT_ID/messages?limit=100" | python3 -m json.tool
```

在 conversations 响应里找到 `parentConversationId` 等于父群的子群，再将它填入下一条命令读取 Agent 回复：

```bash
CHILD_ID='把 conversations 中的 cnv_at_... 填在这里'
curl --fail -H "$AUTH" "$API/api/v1/demo/im/conversations/$CHILD_ID/messages?limit=100" | python3 -m json.tool
```

## 7. synthetic 与 GPT runtime

### 7.1 synthetic（推荐验收模式）

`start_web_client.sh` 默认就是 synthetic：

- 零模型网络请求；
- 回复是确定性的本地合成结果；
- 不需要 API Key；
- 最适合验收群拓扑、Agent Store、授权、幂等、Workboard 和边界。

### 7.2 显式 GPT 试用（可选）

只有你明确希望模型生成时才启用；它仍然不会获得 IM/provider 权限，也不会连接飞书、企微或真实融云。使用本机已授权输入文件时：

```bash
./scripts/start_gpt_im_trial.sh \
  --input-file /Users/lwblx/huapohen/agent/automation/2026/05_08/1/26/input/0.txt \
  --no-open
```

该启动器只读取输入文件中的第一组 HTTPS endpoint、第一条 `sk-` Key，并将模型名固定为 `gpt-5.6-sol`；Key 只存在于子进程环境，不写入 Git、日志、截图、报告或 Notion。不要把 Key 粘贴到命令行、教程、截图或聊天中。

也可以手工提供完整配置：

```bash
export WANWORK_IM_AGENT_RUNTIME=openai-compatible
export WANWORK_IM_MODEL_API_KEY='<通过本机 secret manager 注入>'
export WANWORK_IM_MODEL_BASE_URL='https://<已审阅的 OpenAI-compatible endpoint>'
export WANWORK_IM_MODEL='gpt-5.6-sol'
./scripts/start_web_client.sh --model-runtime openai-compatible --no-open
```

三个变量必须成套匹配。模型断流、401、403 或超时时，先停止进程并切回 synthetic；不要把网络失败解释为 Agent 成功，也不要在日志中记录完整 Key。修改 `.env` 或环境变量后要彻底重启后端。

## 8. 自动化 Web-first 验证（不打开浏览器）

需要快速确认构建、Agent Store、动态指令、子群隔离和 Workboard 闭环时执行：

```bash
WANWORK_IM_VERIFY_PORT=18149 ./scripts/verify_web_first.sh
```

脚本会：

1. 构建 Web 客户端；
2. 临时启动 synthetic API；
3. 验证 HTTP 200 envelope、零网络快照和 Agent Store；
4. 验证最小权限安装与幂等重放；
5. 验证动态指令创建子群、Task、Artifact、Needs You；
6. 验证接受并发布 Artifact 引用；
7. 验证停用撤权后再次 mention 被拒绝；
8. 退出时清理临时进程和日志。

端口已占用时换一个未使用的端口，例如 `WANWORK_IM_VERIFY_PORT=18150`。

## 9. 停止、重置和常见问题

### 服务怎么停

回到运行 `start_web_client.sh` 的终端按 `Ctrl-C`。如果只运行 `start_im_demo.sh`，同样按 `Ctrl-C`。

### 重启后为什么群和任务不见了

当前快速体验使用单进程内存 fake store，重启会清空演示数据。这是预期行为；PostgreSQL durable repository、事件恢复、生产 outbox/inbox 和 provider reconcile 仍属于后续生产阶段。

### 浏览器能打开但页面一直 BOOTING

确认启动终端没有 Go 编译错误，并检查：

```bash
curl --fail http://127.0.0.1:18080/health/live
```

若自定义了 `--im-port`，将命令中的 `18080` 换成对应端口。Go 首次编译最多等待约 180 秒，失败时脚本会打印临时 API 日志。

### 页面显示没有 Agent 或按钮不可用

等待 Runtime 变为 `READY`，然后刷新页面。`@v0版 Agent` 只有在“普通群 + active Agent 已成为群成员”时可用；Agent 子群不能再次发起子群。

### 局域网设备访问不了

确认使用了 `--lan`、使用的是开发机局域网 IP、端口已放行且设备在同一 Wi-Fi。API 回环绑定是故意的安全边界，不要把 Go API 改成公网监听。

### GPT runtime 失败或“启动即断流”

先用 synthetic 验证本地闭环。模型模式只访问显式配置的 endpoint；检查 endpoint、model、Key 是否成套匹配、网络/VPN/出口是否可用。任何 provider 失败都会保留为错误，不会静默生成合成成功。

## 10. 当前能力边界（验收时不要误判）

当前 Web IM 可以真实体验的是本地 vertical slice：

- Web/PWA first 的响应式聊天界面；
- 普通群、单聊、消息发送/编辑/撤回/搜索；
- Agent Store 的 definition/release/Trust Passport/installation 投影；
- 最小能力安装、成员邀请、撤权和幂等；
- 父群与 Agent 子群隔离、Invocation、受限 work-card；
- Task、Artifact、Needs You 的人审闭环和父群引用发布；
- synthetic 与显式 OpenAI-compatible 文本 runtime 切换。

当前尚未完成、不能宣称生产可用的部分：

- 真实 Clerk 登录、JWKS、issuer/audience、session revoke 和多租户 session；
- 真实融云 SDK、callback 签名、replay 防护、限流、对账和 outbound；
- PostgreSQL durable Agent Store/thread plan 的完整生产迁移、崩溃恢复和 reconcile worker；
- 原生 macOS/Windows/Linux/iOS/iPadOS/Android/鸿蒙安装包、推送和离线同步；
- 文件、音视频、复杂搜索、多设备已读游标和完整办公 IM 能力；
- 生产级模型工具执行、secret broker、观测、SLO、IaC、签名发布和合规。

本教程的绿色结果证明的是本地合同和可重复的 Web-first 体验，不等于真实 IM 已接入生产。尤其不要把 `demo.local.signature`、前端字段或任何本地 fake provider 标识当作生产授权凭据。

## 11. 相关文档

- [统一全端体验入口](./ALL_PLATFORM_TRIAL_GUIDE.md)
- [本地 IM 验收合同与 API 细节](./LOCAL_IM_ACCEPTANCE_GUIDE.md)
- [Web-first 阶段检查点](./WEB_STAGE_CHECKPOINT_20260830.md)
- [Web 客户端说明](../../clients/im-web/README.md)
- [IM 产品/架构/实施导航](./README.md)
