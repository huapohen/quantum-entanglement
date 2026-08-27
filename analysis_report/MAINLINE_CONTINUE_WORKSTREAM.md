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

继续 `NATIVE_IM_EARLY_INTEGRATION_PLAN.md`，从 E1 `CONTRACT_EXECUTABLE` 开始：

1. 把 `docs/architecture/NATIVE_IM_CONTRACT_V1.md` 的冻结合同实现为 exact Python 值模型；
2. 实现 strict dict/JSON codec、canonical bytes、digest 与幂等键派生；
3. 固定 golden vectors；
4. 实现零网络 fake adapter、receiver ledger、ACK-loss 与 acceptance query；
5. 完成 Python 3.9/3.12/3.13、全量测试、Ruff、Mypy 和零网络证据；
6. 每个小改变独立 commit 并推送本分支；阶段结束同步独立 Notion 空间。

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
| E1 | 进行中 | 当前实现入口 |
| E2 | 未开始 | 专用沙箱 inbound-only |
| E3 | 未开始 | verified inbound → PURE Agent 草稿 |
| E4 | 未开始 | fake-only Action Plane |
| E5 | 未开始 | 需另行明确授权的单会话 sandbox outbound |
