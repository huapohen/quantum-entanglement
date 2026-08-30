# 10 小时全量目标执行状态（2026-08-30）

更新时间：2026-08-30（集成验证后）
当前评审分支：`mainline_continue_quantum_entanglement`
当前 HEAD：`af3bd43`（已推送 `origin/mainline_continue_quantum_entanglement`）
来源分支：`dev_wanwork_quantum_entanglement`（已保留并合入本评审分支）
安全备份：`backup_0830_121258`

## 结论

当前已经有一条可从浏览器直接体验的 Web-first IM 主线：群聊、Agent Store、普通文本消息、
编辑/撤回、自定义指令、Agent 子群隔离、父群工作卡、显式 Task/Artifact/Needs You 工作台、
产物接受/退回、消息搜索、PWA 和局域网移动设备访问均可运行。默认 synthetic，不访问外网，
不会向飞书、企微或真实融云发送消息。

“所有任务”中的生产集成和原生安装包仍不能诚实地标记为完成：真实 Clerk/JWKS、真实融云 SDK/
回调、PostgreSQL 业务投影与 crash recovery、QE invocation bridge、Tool/Skill/Action Plane、
SSE/WebSocket resume、文件/已读/通知/reaction、原生 `.app/.exe/.ipa/.apk/.hap` 仍是后续工程。
本账本保留这些缺口，避免用 fake 结果冒充生产完成。

## 已完成并推送（当前分支）

| 主题 | 结果 |
| --- | --- |
| Web/PWA | React + TypeScript + Vite + Zustand；manifest；仅静态 shell service worker；API/聊天不缓存 |
| IM API | Go + Fiber loopback；统一 HTTP 200 envelope；direct/group 会话、群成员、消息、编辑、撤回、搜索 |
| Agent 拓扑 | Agent Store 投影；普通用户式 Agent actor；父群 → 独立子群；ACL 隔离；父群只写受限工作卡 |
| 任务工作台 | Task；Artifact 草稿；Needs You；接受/退回；幂等重放；独立于聊天正文 |
| 模型 runtime | synthetic 默认；显式 OpenAI-compatible Responses/SSE；Key 不进入日志/报告/事件 |
| 本地体验 | 一键启动；`--lan` 真实移动浏览器访问；脱敏 GPT 试用启动器 |
| 测试门禁 | Python 2,964 项全量；Ruff；strict mypy；compileall；Go test/vet；Web build；Web-first 脚本覆盖 envelope、Agent Store、子群和 Workboard |
| 研究与文档 | `analysis_report/research` 既有调研快照；Web、跨端、启动和审阅检查点已补齐 |

以下产品提交来自 `origin/dev_wanwork_quantum_entanglement`，现已随集成节点纳入本评审分支：

- `5199e1d`：Task/Artifact/Needs You Workboard；
- `d97fbc9`：局域网设备体验入口；
- `bd8da4b`：受 ACL 保护的消息搜索；
- `d181a9b`：脱敏 GPT runtime 启动器；
- `e5db53d`：Workboard 自动化门禁；
- `f898ca8`：PostgreSQL v2 attempt/fence 合同强化。

本次集成提交：

- `4783f61`：保留 QE Result Authority 内核，合入 Web-first IM API、React 客户端、PostgreSQL
  authority 文档、启动脚本和 synthetic 验收门禁；已推送到
  `origin/mainline_continue_quantum_entanglement`。

本次继续冲刺提交：

- `e5acef0`：注册 PostgreSQL migration 11 durable message projection schema；
- `af3bd43`：将 authority cutover 测试夹具/golden 更新到当前 schema 11，并完成 Go 全模块 test/vet。

## 今晚直接验收

桌面端：

```bash
./scripts/start_web_client.sh --no-open
```

手机/平板（同一 Wi-Fi）：

```bash
./scripts/start_web_client.sh --lan --no-open
```

GPT runtime（可选，Key 只在子进程环境）：

```bash
./scripts/start_gpt_im_trial.sh \
  --input-file /Users/lwblx/huapohen/agent/automation/2026/05_08/1/26/input/0.txt \
  --no-open
```

最短动作：创建含 Agent 的群 → 发布自定义指令 → 进入子群看回复 → Workboard 接受或退回产物 →
切回父群确认只有工作卡 → 搜索当前会话消息。

自动门禁：

```bash
./scripts/verify_web_first.sh
```

## 生产化剩余主线（按优先级）

1. 可信认证上下文和 action-time tenant/principal/membership resolver；
2. PostgreSQL Task/Thread/Message/Artifact/Needs You event/projection、inbox/outbox、恢复与对账；
3. QE invocation bridge、Attempt/Budget/Approval、Tool/Skill/Action Plane 和 artifact 发布；
4. 融云 adapter：普通用户 provisioning、签名回调、nonce/replay、mapping drift、ACK unknown/reconcile；
5. Web 的 SSE/WebSocket resume、断线、离线同步、文件/已读/reaction/通知；
6. 真实 sandbox 端到端后再做 Tauri 桌面、iOS/iPadOS、Android、鸿蒙打包与签名。

Notion/Yuque 同步按此前“先本地收口、最后统一上传”的授权处理；本阶段先以本地 Git 为实现真相源，
不会为了同步阻塞 10 小时主线。
