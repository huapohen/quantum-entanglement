# 10 小时全量目标执行状态（2026-08-30）

更新时间：2026-08-30 15:12（Asia/Shanghai）
主线分支：`dev_wanwork_quantum_entanglement`  
当前 HEAD：见 Git 远端 `origin/dev_wanwork_quantum_entanglement`（本文件不硬编码可变 SHA）
最近安全备份：`backup_0830_151334`（固定指向本轮 offboard 文档收口节点）
固定验收标签：`v0.2.0-web-im-20260830`

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
| Agent Store | 认证目录投影；Trust Passport；requested/granted capability 分离；available Agent 幂等安装；Agent actor provisioning；安装后加入根群；显式 offboard/撤权与 provider 成员清理 |
| Artifact 发布 | 人工接受后才允许发布；父群只接收 Artifact ID/digest 引用；确定性 client message ID；重复发布返回 replay，不复制产物正文 |
| 模型 runtime | synthetic 默认；显式 OpenAI-compatible Responses/SSE；Key 不进入日志/报告/事件 |
| 本地体验 | 一键启动；`--lan` 真实移动浏览器访问；脱敏 GPT 试用启动器 |
| 测试门禁 | Go unit/race/vet；Web build；Web-first 脚本覆盖 envelope、Agent Store、子群和 Workboard |
| 研究与文档 | `analysis_report/research` 既有调研快照；Web、跨端、启动和审阅检查点已补齐 |

近期提交均已推送到 `origin/dev_wanwork_quantum_entanglement`：

- `5199e1d`：Task/Artifact/Needs You Workboard；
- `d97fbc9`：局域网设备体验入口；
- `bd8da4b`：受 ACL 保护的消息搜索；
- `d181a9b`：脱敏 GPT runtime 启动器；
- `e5db53d`：Workboard 自动化门禁；
- `f898ca8`：PostgreSQL v2 attempt/fence 合同强化。
- `9faf969`：Web 直接创建人-Agent 单聊；
- `6c877f4`：HTML 状态报告与索引。
- `857eb08`：Agent Store 本地 catalog 安装闭环；
- `8bda56a`：Web 暴露 Agent Store 安装动作；
- `8095c3b`：Agent Store 安装/回放门禁；
- `4babd88`：接受后的 Artifact 引用发布；
- `a99338a`：Workboard 暴露 Artifact 发布按钮；
- `536395c`：Artifact 发布/回放门禁。
- `6e039d2`：Agent Store 安装与 invocation 的 action-time Trust Passport 准入加固。
- `1f86e5a`：补充 Agent Store action-time gate 验收证据与阶段提交台账。
- `0427a8c`：增加 provider user revoke/member removal 合同与 fake provider 效果语义。
- `d63ae39`：增加 Agent Store 幂等 offboard、数据处置选择、本地成员/access 清理与撤权测试。
- `62a5ca0`：Web Agent Store 暴露“停用并撤权”，并把 offboard 加入 Web-first 门禁。

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

15:05 阶段复核：`WANWORK_IM_VERIFY_PORT=18133 ./scripts/verify_web_first.sh` 通过；Go
`go test ./... -count=1`（含 PostgreSQL/authoritycutover 全包）通过；本次验证未产生外部网络或
飞书/企微消息。

本轮 Agent Store 追加复核：安装 planner 后完成动态指令和 Artifact 发布，再以
`dataDisposition=archive` 执行 offboard；响应包含 parent/child conversation 清理清单，重复
offboard 返回 `replayed=true`，撤权后的 mention 返回 HTTP 200 envelope + `code=40301`。本地
fake provider 已验证成员移除和普通用户撤权的 committed/replayed/conflict 语义；真实 provider
callback、durable 事务和 reconcile 仍未实现。

## 生产化剩余主线（按优先级）

1. 可信认证上下文和 action-time tenant/principal/membership resolver；
2. PostgreSQL Task/Thread/Message/Artifact/Needs You event/projection、inbox/outbox、恢复与对账；
3. QE invocation bridge、Attempt/Budget/Approval、Tool/Skill/Action Plane 和 artifact 发布；
4. 融云 adapter：普通用户 provisioning、签名回调、nonce/replay、mapping drift、ACK unknown/reconcile；
5. Web 的 SSE/WebSocket resume、断线、离线同步、文件/已读/reaction/通知；
6. 真实 sandbox 端到端后再做 Tauri 桌面、iOS/iPadOS、Android、鸿蒙打包与签名。

截至本次更新时间，新增 Agent Store 安装、Artifact 引用发布与 offboard/撤权变更均保持 `local_pending`，尚未上传
Notion；按用户授权，截止时间前再统一批量同步并回读。完整代码和证据仍以本地 Git 为实现真相源，
Notion 只做阅读镜像，不包含任何 API Key；语雀未操作。
