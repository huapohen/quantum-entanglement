# Agent Store 实时门禁记录（2026-08-30 16:59 +0800）

## 结果

在 `dev_wanwork_quantum_entanglement` 当前 HEAD `010c277b0f5b1dd2457b63537d3afe753ac84578` 上，重新执行：

```bash
WANWORK_IM_VERIFY_PORT=18138 ./scripts/verify_web_first.sh
```

门禁通过：

```text
Web-first synthetic 验证通过（构建、envelope、Agent Store 安装/撤权、子群隔离、Workboard 审阅闭环）
```

## 本次实际覆盖

- React/Vite/TypeScript 生产构建；
- 认证后的 Agent Store catalog 投影；
- `available` Agent 安装、幂等回放和最小权限能力集合；
- 未审阅能力拒绝；
- 自定义指令创建隔离 Agent 子群，Agent 回复不进入父群；
- Task → Artifact → Needs You → 接受 → 父群发布引用；
- `archive` 撤权、provider 成员清理、普通用户撤权和幂等回放；
- 撤权后的 `@Agent` 业务拒绝（HTTP 200 envelope + `code=40301`）。

## 远端与同步边界

- 当前分支与远端 `origin/dev_wanwork_quantum_entanglement` 同 SHA；
- `backup_0830_164443` 与当前 HEAD 同 SHA；
- 本记录属于本地/Git `local_pending`，按用户要求暂不上传 Notion；
- synthetic/fake provider、内存状态和 loopback API 仍不能代表真实生产 Marketplace、Clerk、融云或 PostgreSQL durable 安装服务。
