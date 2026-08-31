# 当前阶段体验教程｜Quantum Entanglement v0版

> 适用代码：`mainline_continue_quantum_entanglement@28a4a46`<br>
> 生成日期：2026-08-31（Asia/Shanghai）<br>
> 体验范围：本机 loopback、synthetic 离线 fixture，以及显式配置的 GPT OpenAI-compatible runtime<br>
> 安全承诺：本教程不会连接飞书、企微或真实 IM，也不会向任何人、群聊、bot 或 webhook 发消息。

这份教程回答一个问题：**现在拿到代码后，怎样在 10–20 分钟内完整体验当前阶段，并判断它是否值得继续做？**

当前版本有两条入口：

| 入口 | 适合看什么 | 默认网络行为 | 结果形态 |
| --- | --- | --- | --- |
| Python 产品试用页 | 自定义指令 → 分析/生成/复核三 Agent DAG → Artifact/事件时间线 | synthetic 零网络；GPT 模式只访问显式模型端点 | 三个可预览、可下载的 Markdown Artifact |
| React Web IM | 群聊、普通消息、编辑/撤回、Agent Store、Agent 子群、Workboard | synthetic 零网络；GPT 模式只访问显式模型端点 | 父群 + 独立 Agent 子群 + 任务/产物/Needs You |

推荐先跑 synthetic，再跑一次 GPT。synthetic 用来核对产品结构和安全边界，GPT 用来核对“任意自定义指令确实进入模型”的体验；两者都不会触碰飞书、企微或真实融云。

## 0. 先确认你运行的是当前阶段代码

当前评审分支的代码在独立 worktree，不在正式 `main` 目录。请从下面路径开始：

```bash
export QE_REVIEW_ROOT=/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/mainline_continue_quantum_entanglement
cd "$QE_REVIEW_ROOT"
git status --short --branch
git rev-parse --short HEAD
```

应看到分支 `mainline_continue_quantum_entanglement` 和短提交 `28a4a46`（或之后由你明确验收的新提交）。如果当前目录显示的是正式 `main`，不要据此判断本阶段功能不存在；切换到上面的 worktree 再试。

工作结束时，所有服务都可以用启动它的终端里的 `Ctrl-C` 停止。体验过程不会修改 Git 跟踪文件；浏览器下载的 Artifact 会落在浏览器默认下载目录。

## 1. 前置检查与第一次启动

### 必需工具

```bash
python3 --version   # 支持 3.9–3.13，3.14+ 会被启动器拒绝
go version          # Web IM 需要
npm --version       # Web IM 需要
```

Python 产品试用页只依赖标准库，不需要安装第三方 Python 包。Web IM 首次运行会在 `clients/im-web` 执行 `npm ci --ignore-scripts`；如果你想提前安装：

```bash
cd "$QE_REVIEW_ROOT/clients/im-web"
npm ci --ignore-scripts --no-audit --no-fund
cd "$QE_REVIEW_ROOT"
```

### 端口约定

- Python 产品试用页：`127.0.0.1:8765`
- Go IM API：`127.0.0.1:18080`
- React/Vite 页面：`127.0.0.1:5173`

端口冲突时只需换本机端口，不要关闭不认识的进程：

```bash
./scripts/start_local_trial.sh --synthetic --port 8877
./scripts/start_web_client.sh --no-open --im-port 19080 --web-port 5174
```

## 2. 入口 A：Python 产品试用页（建议先体验）

### 2.1 离线 synthetic：零网络、最快验收

在仓库根目录执行：

```bash
cd "$QE_REVIEW_ROOT"
./scripts/start_local_trial.sh --synthetic --no-open
```

终端会打印带一次性 `#token=…` 的完整地址，例如 `http://127.0.0.1:8765/#token=…`。复制**完整地址**到浏览器，不要直接双击 `examples/product_trial/index.html`，也不要删掉 fragment 中的 token。

页面打开后：

1. 确认右上角为 `LOCAL ONLY`，Runtime 显示 `synthetic / deterministic-fixture`；
2. 在“发布你的协作指令”中输入一条代码里没有出现过的任务，例如：

   ```text
   比较三个面向知识工作的 Agent 协作产品，给出目标用户、关键差异、风险和一个两周可执行的验证计划。
   ```

3. 点击“发布指令并运行”，等待状态变为“运行完成 · 可审计”；
4. 依次点击三个 Artifact，检查 Markdown 原文、版本号和 digest；
5. 点击“下载 .md”保存最终版本；
6. 展开“给工程师看的原始结果”，核对 JSON 与页面数字一致。

一次成功运行应看到：

| 检查点 | 预期 |
| --- | --- |
| 任务 DAG | `research`、`design`、`review` 按依赖顺序完成 |
| Narration | 3 段非空 Agent 输出 |
| Artifact | `01_analysis.md`、`02_result.md`、`03_final_review.md` |
| Needs You | 0 个阻断（当前只读生成场景） |
| 事件时间线 | 25 个事件，顺序可回放 |
| 系统图 | 架构图、消息执行时序图、平台状态关系图均可见 |

### 2.2 GPT 自定义指令：真实模型路径

模型模式不会从环境中“猜”配置。必须同时提供 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`，并保证它们属于同一 OpenAI-compatible 网关。Key 只放在本机 `.env` 或进程环境，不能写入 Git、截图、Artifact、日志、Notion 或回复。

```bash
cd "$QE_REVIEW_ROOT"
cp .env.example .env       # 仅第一次执行
chmod 600 .env
$EDITOR .env               # 填入同一套 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
./scripts/start_local_trial.sh --no-open
```

默认模型名模板是 `gpt-5.6-sol`。端点必须是 `https://…`，不要把 DeepSeek 官网 Key、公司受 IP 白名单限制的 Key 或其他供应商 Key 与错误端点混用。修改 `.env` 后必须彻底停止并重新启动后端，配置是进程内单例。

页面验收步骤与 synthetic 相同，但这次要确认：

- Runtime 显示 `model / openai-compatible / gpt-5.6-sol / configured`；
- 三段 narration 不是固定 fixture 文案，而是随指令变化；
- Artifact 内容与页面当前指令相关；
- 页面仍显示 `externalMessaging=false`、`productionApproved=false`、`gateStatus=A-E closed`。

模型网关失败时页面会显示受控的 `trial_run_failed`，不会把供应商原始错误或 Key 回显到浏览器。

### 2.3 CLI：只看内核的确定性结果

```bash
cd "$QE_REVIEW_ROOT"
./scripts/start_local_trial.sh --cli
```

CLI 运行的是原有合成协作 demo，会把完整 JSON 打到终端；它不是网页模型模式，也不会调用模型或连接聊天平台。

## 3. 入口 B：React Web IM（群聊协同体验）

### 3.1 一条命令启动 synthetic Web IM

```bash
cd "$QE_REVIEW_ROOT"
./scripts/start_web_client.sh --no-open
```

脚本会启动：

```text
Go fake IM API：  http://127.0.0.1:18080
React/Vite 页面： http://127.0.0.1:5173
```

打开 `http://127.0.0.1:5173`。页面右上角应显示 `LOOPBACK APP`，会话区和右侧 Agent Store 自动加载。默认是 synthetic：`networkCalls=0`，不读取模型 Key，不连接真实 IM。

### 3.2 按这条顺序体验群聊

1. **普通消息**：在当前群发送一条普通文本；刷新页面确认消息仍由本地 projection 返回。
2. **编辑/撤回**：在自己的消息下点击“编辑”，再点击“撤回”，确认状态保留为 `recalled`，正文显示“（已撤回）”。
3. **新建群**：左侧输入群名，保持“创建时邀请已安装 Agent”勾选并点击 `+`。取消勾选再创建一个普通群，比较两种成员列表。
4. **邀请 Agent**：选中普通群，点击 Agent Store 的“邀请到当前群”。重复点击应显示 Agent 已在群中，而不是重复加成员。
5. **任意自定义指令**：在含 Agent 的普通群中，在“发布协作指令”输入一条新的、非固定示例的指令，点击 `@v0版 Agent`。
6. **子群隔离**：页面会切换到新建的 `Agent 子群`。检查父群只出现受限工作卡，Agent 回复的 `conversationId` 等于子群 ID，而不是父群 ID。
7. **Workboard**：右侧查看任务状态、Artifact 预览和 `Needs You`。synthetic 路径通常先显示草稿/待复核；点击“接受产物”后应变为 `accepted / completed / resolved`。如果只是看安全边界，也可以点“退回”。
8. **Agent Store**：检查 definition、release、Trust Passport、installation、`requestedCapabilities`、`grantedCapabilities` 和 data routes 分开显示；它们来自后端投影，不是前端固定字符串。
9. **消息搜索**：在当前会话顶部搜索刚才发送的关键词，确认只返回当前会话的消息。

这条链路可以概括为：

```mermaid
flowchart LR
    U[真人输入指令] --> P[父群消息 / Mention Router]
    P --> C[受限工作卡]
    P --> S[独立 Agent 子群]
    S --> R[Agent 回复]
    P --> W[Workboard]
    W --> A[Artifact 草稿]
    A --> H[Needs You 接受或退回]
```

### 3.3 GPT Web IM：使用本机输入文件启动

如果要使用你提供的本机 endpoint/Key 输入文件，推荐使用专用启动器。它只读取文件中的第一组 HTTPS endpoint 和第一条 `sk-` Key，把 Key 注入子进程环境后立即启动 Web IM；Key 不会打印、落盘、进入页面、事件、截图或 Git。

```bash
cd "$QE_REVIEW_ROOT"
./scripts/start_gpt_im_trial.sh \
  --input-file /Users/lwblx/huapohen/agent/automation/2026/05_08/1/26/input/0.txt \
  --no-open
```

这条命令启动的是 Web IM，不是 Python 单页试用页；如果你要体验单页 GPT 路径，请按 [2.2](#22-gpt-自定义指令真实模型路径) 把同一套配置放入 `.env`。模型失败时不会静默回退 synthetic，便于区分“真实模型成功”和“离线 fixture 成功”。

### 3.4 手机/平板浏览器（可选）

同一 Wi-Fi 下，可以让 Vite 页面监听局域网：

```bash
./scripts/start_web_client.sh --lan --no-install
```

脚本会打印局域网地址。Go API 仍只绑定 `127.0.0.1`，仅通过 Vite 代理提供服务；这只用于本地验收，不要暴露到公网。当前没有可安装的原生 App，移动端看到的是响应式 Web 页面。

## 4. 用 curl 做可复核验收

以下命令针对 `start_web_client.sh` 的默认 API 端口；如果改了端口，把 `18080` 换成你的 `--im-port`。

### 4.1 运行快照与 Agent Store

```bash
curl --fail http://127.0.0.1:18080/health/live
curl --fail http://127.0.0.1:18080/api/v1/system/ping
curl --fail -H 'Authorization: Bearer demo.local.signature' \
  http://127.0.0.1:18080/api/v1/demo/im
curl --fail -H 'Authorization: Bearer demo.local.signature' \
  http://127.0.0.1:18080/api/v1/demo/im/agents
```

快照应包含 `mode=zero-network-fake`、`networkCalls=0`、fake Clerk/RongCloud-shaped provider 和当前 Agent runtime。`demo.local.signature` 是公开的代码内 synthetic fixture，不是 API Key，也不能访问外部系统。

### 4.2 发送一条动态 Mention

```bash
curl --fail \
  -H 'Authorization: Bearer demo.local.signature' \
  -H 'Content-Type: application/json' \
  --data '{
    "conversationId":"cnv_local_demo_parent",
    "messageId":"msg_tutorial_20260831_1",
    "instruction":"为一个三人产品团队设计 Agent 协同周报流程，列出输入、审批点和失败恢复。"
  }' \
  http://127.0.0.1:18080/api/v1/demo/im/mentions
```

成功响应是 HTTP 200、业务 `code=200`，并满足：`childConversationId` 以 `cnv_at_` 开头、`agentReply.conversationId` 等于子群 ID、`providerStatus=committed`。相同 `messageId + instruction` 重试会得到 `replayed=true`；相同 `messageId` 改写 instruction 会得到业务 `code=40902`，这是幂等冲突而不是 HTTP 传输失败。

### 4.3 自动 smoke gate

```bash
cd "$QE_REVIEW_ROOT"
./scripts/verify_web_first.sh
```

它会临时启动 synthetic API，检查 Web 构建、HTTP envelope、Agent Store、动态指令、子群隔离、Workboard 接受闭环，并在结束时清理进程和临时日志；不会打开浏览器、访问外网或读取模型/聊天平台凭据。

## 5. 产物、截图和数据在哪里

### Markdown Artifact

- Python 产品试用页点击下载后：浏览器默认下载目录（macOS 通常是 `~/Downloads`），文件名为 `01_analysis.md`、`02_result.md`、`03_final_review.md`。
- 未点击下载时：Artifact 只存在本次服务进程的内存 SQLite；停止服务后不会持久化。页面预览、下载正文和原始 JSON 来自同一份运行结果。
- Web IM：Artifact 在右侧 Workboard 和 `GET /api/v1/demo/im/artifacts` 中展示；当前本地 UI 没有单独的下载按钮，可复制预览内容保存。

### 已归档截图

仓库已有的产品/IM 证据在：

- `analysis_report/screenshots/10_local_trial_desktop_idle.png`
- `analysis_report/screenshots/11_local_trial_desktop_complete.png`
- `analysis_report/screenshots/12_local_trial_mobile_complete.png`
- `analysis_report/screenshots/13_local_trial_architecture_diagrams.png`
- `analysis_report/screenshots/14_model_backed_custom_instruction_gpt.png`
- `analysis_report/screenshots/35_local_im_acceptance_desktop.png`
- `analysis_report/screenshots/36_local_im_acceptance_mobile.png`
- `analysis_report/screenshots/38_local_im_basic_desktop.png`
- `analysis_report/screenshots/39_local_im_basic_mobile.png`
- `analysis_report/screenshots/40_local_im_edit_recall_desktop.png`
- `analysis_report/screenshots/41_local_im_edit_recall_mobile.png`

来源、完整 SHA-256 和“不可把旧 synthetic 截图冒充真实模型证据”的限制见 `analysis_report/screenshots/README.md` 与 `manifest.json`。新截图请自行在 loopback 页面采集，涉及模型输出时不要把含敏感内容的原图上传公共位置。

## 6. 停止、重启和清理

- 单页 Python 服务：回到启动它的终端按 `Ctrl-C`。
- 一键 Web IM：回到同一个终端按 `Ctrl-C`，启动脚本会同时清理 Go API。
- 手动双终端模式：分别在 Vite 终端和 Go API 终端按 `Ctrl-C`。
- CLI 和 `verify_web_first.sh`：完成后自动退出。

重启时重新执行启动命令即可；单页 token 每次启动都会重新生成。不要复用旧 URL，也不要为了清理端口执行范围过大的 `kill` 命令。

## 7. 常见故障速查

| 现象 | 处理 |
| --- | --- |
| `需要 Python 3.9–3.13` | 用 `QE_TRIAL_PYTHON=/绝对路径/python3` 指定受支持版本；不要用 3.14+。 |
| `Permission denied` | `chmod +x scripts/start_local_trial.sh scripts/start_web_client.sh scripts/start_gpt_im_trial.sh`。 |
| `Address already in use` | 为单页使用 `--port`，为 Web IM 使用 `--im-port` 与 `--web-port`。 |
| `启动令牌缺失` | 关闭旧页，重新运行启动脚本并复制完整 `#token=…` URL；不要直接打开 HTML 文件。 |
| `trial_run_busy` | 当前服务只允许一个单页运行；等待完成后再点一次。 |
| `model_configuration_missing_or_invalid` | 三个 GPT 字段必须整套存在；检查端点是 HTTPS、没有 query/fragment，Key 与 endpoint 属于同一供应商。 |
| `trial_run_failed` 或 IM `dependency_unavailable` | 先确认网络/VPN和端点连通，再确认网关支持 Responses API/结构化 input/stream；服务不会回显供应商原始错误。 |
| Web IM “IM API 未在 180 秒内就绪” | 查看脚本输出的临时日志；首次 Go 冷编译可能需要几分钟，确认 Go toolchain 可用后重试。 |
| 只有固定示例、没有动态结果 | 确认没有误用 `--cli` 或 `--synthetic`；模型路径必须显式配置并彻底重启。 |

排查凭据时只记录 Key 前缀、长度和 SHA-256 短指纹。不要在终端回显、截图、Issue、commit、报告、Notion 或本教程中写完整 Key。

## 8. 当前阶段能证明什么，不能证明什么

### 本阶段可直接体验并核验

- 任意自定义指令进入受治理的三 Agent DAG（分析 → 生成 → 复核）；
- 模型 narration、版本化 Markdown Artifact、Needs You 和因果事件时间线；
- 原生 IM 的父群/Agent 子群拓扑、普通消息生命周期、Agent Store 投影与幂等邀请；
- Workboard 对 Task、Artifact 和 Needs You 的分离，以及接受前保持草稿；
- loopback、一次性 token、受控请求体、CSP、无外部消息副作用和凭据脱敏边界。

### 当前明确不属于生产能力

- 真实 Clerk/JWKS、真实融云 SDK/回调/对账、生产 PostgreSQL 业务读写与多进程 HA；
- 生产 worker dispatch、真实 Agent/tool/browser/subprocess 执行、SSE/WebSocket resume、文件/音视频/通知/离线同步；
- 原生桌面或移动安装包，以及任何真实 outbound；
- Gate A–E、私有试点批准、商用发布和生产 SLO。页面上的 `productionApproved=false` 与 `gateStatus=A-E closed` 是有意保留的事实，不要用本地 fake 的绿色结果替代生产门禁。

下一步阶段、出口条件和依赖见 [`analysis_report/FINAL_COMPLETION_ROADMAP.md`](../analysis_report/FINAL_COMPLETION_ROADMAP.md)；当前安全状态见 [`docs/production/CURRENT_READINESS.md`](production/CURRENT_READINESS.md)。

## 9. 完成验收后给项目的反馈

建议记录以下四个问题（可直接复制到验收记录）：

1. 群聊、任务 DAG、Artifact、Needs You 是否像同一个自然工作空间？
2. 三个 Agent 的分工、依赖和“先记录再调用”是否一眼可懂？
3. 你最想增加的下一个真实动作是什么：审批、文件、搜索、多人同时协作，还是外部系统连接？
4. 在不打开真实 outbound 的前提下，哪一个产品行为已经足以让你愿意把 IM 接进来？

相关深入文档：

- [`docs/LOCAL_PRODUCT_TRIAL.md`](LOCAL_PRODUCT_TRIAL.md)：单页产品试用的完整页面说明；
- [`docs/wanwork_im/LOCAL_IM_ACCEPTANCE_GUIDE.md`](wanwork_im/LOCAL_IM_ACCEPTANCE_GUIDE.md)：原生 IM API/安全合同与 curl 验收；
- [`clients/im-web/README.md`](../clients/im-web/README.md)：React/Vite 客户端构建和生产边界；
- [`apps/im-api/README.md`](../apps/im-api/README.md)：Go API 组合、runtime 与 fake provider 说明；
- [`analysis_report/README.md`](../analysis_report/README.md)：调研、截图和阶段证据索引。
