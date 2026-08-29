# mainline_continue_quantum_entanglement 独立工作流

> 分支：`mainline_continue_quantum_entanglement`  
> 起点：`main@ced51b432eab2c5e17269718d14fbc999c1205a4`  
> 创建时间：2026-08-28（Asia/Shanghai）  
> 合并策略：不自动合并；保留 worktree 与远端分支，等待用户人工审阅

## 工作区

本分支只在以下 linked worktree 中开发：

```text
/Users/lwblx/huapohen/agent/execute/infinite/worktrees/quantum_entanglement/mainline_continue_quantum_entanglement
```

正式主仓 `/Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement` 不承载本分支改动。

独立 Notion 审阅空间：

<https://app.notion.com/p/3c9ead4b996e8108aea6c97c694d6587?pvs=204>

## 当前目标

E1 `CONTRACT_EXECUTABLE` 已完成并完成独立 Notion 回读。当前继续
`NATIVE_IM_EARLY_INTEGRATION_PLAN.md` 的 E2 inbound-only 垂直切片：

1. provider profile、inbound-only config/`SecretRef` 和 raw-body verifier 已实现；
2. migration 5 六表、backup/restore/topology registry 已实现；
3. profile-bound durable nonce claim 已实现；
4. 保留 exact request 时可重开 replay 并与 checkpoint 对账的 read preparation 已在 `4ab745b` 实现；
5. nonce、verified page、event/verification/link rows、read CAS、checkpoint 与独立 readback 的
   单事务 admission 已在运行源码 `9cf1bfe` 完成；
6. default-off composition、显式 inbound-only adapter、bounded parser、process-bound lifecycle/
   kill switch、typed safe logging、canary、recorded probe 和 zero-network gate 已在运行源码
   `2bdaea1` 完成；
7. provider approval、Mapper/Transport/Bundle TCK、zero-network exchange、read-exchange evidence、
   pure mapper、增强 provenance 和 migration-v6 durable readback 已在 `ee0666f` 离线闭环；
8. 当前下一步不是直接打开网络，而是取得真实 IM 后端合同和测试 scope，实现 production exchange
   与第一个真实 provider bundle，并修订 `SERVICE_BOUNDARY.md`；
9. 每个小改变独立 commit 并推送本分支；稳定节点批量同步独立 Notion 空间并回读。

## 不变边界

- E1 不解析真实 endpoint，不读取任何环境 credential，不打开网络；
- fake outbound 默认关闭，只允许测试进程内不可序列化 permit；
- 不向飞书、企微、任何个人、群聊、bot 或 webhook 发消息；
- 不连接真实原生 IM，也不执行真实 outbound；
- 不修改、重写或降级已冻结 V1 wire contract；
- 本分支完成后不合并、不删除，等待用户人工审阅。

## 状态

| 阶段 | 状态 | 说明 |
|---|---|---|
| E0 | 已继承 | 主线恢复分支、tag、bundle 与回读证据已完成 |
| E1 | 已完成 | provider-neutral 合同、fake、zero-network 证据和 Notion 回读已闭环 |
| E2 | 进行中 | provider bundle 离线闭环 `ee0666f` 已完成；真实 sandbox 未连接，下一门禁是真实 provider contract/scope/production exchange |
| E3 | 进行中 | Result Authority opt-in rehearsal：原子 acceptance、heartbeat fencing、recovery/projection |
| E4 | 未开始 | fake-only Action Plane |
| E5 | 未开始 | 需另行明确授权的单会话 sandbox outbound |

## E3 continuation checkpoint (2026-08-29)

The same branch now carries the opt-in Result Authority continuation through `7bed2b6`:

- migration 7 activation/reopen remains explicit and default-off;
- result-specific active backup/restore now binds the migration-7 topology, database bytes,
  page geometry and table counts, with no-overwrite restore and post-restore verification;
- a private scoped PURE supervisor now enforces first-heartbeat admission, heartbeat-loss fencing,
  timeout/cancellation drain and late-result discard; its acceptance seam keeps heartbeat fencing
  active until an exact `AcceptedV2`/`ObservedV2` outcome; product dispatch remains disabled;
- a committed result graph can be read back while its exact job/attempt owner is still
  `RUNNING` and reconciled by a receipt-bound, non-emitting CAS;
- the API is idempotent (`RECONCILED` / `ALREADY_RECONCILED`), rejects stale or malformed owners,
  detects competing CAS and trigger side effects, and rolls back the complete transaction;
- successful reconciliation changes only the owner job/attempt rows; it creates no event, outbox,
  publication, lease, capability or external message.

The next local gates are business projection, crash/kill/two-process recovery and compatibility/
rollback evidence. The opt-in API can issue process-bound `AcceptedV2` only after a fresh COMMIT ACK;
production worker dispatch, real IM connectivity and all outbound effects remain disabled. Notion synchronization stays
`local_pending` until this checkpoint is closed and then receives one batch upload plus page-by-page
readback.
