# 提前接入原生 IM 前状态与恢复检查点

> 记录时间：2026-08-27 20:00:10 +08:00  
> 决策：保留当前稳定状态并完成三层备份；后续改走原生 IM 提前接入路径  
> 代码基线：`1d399e555fb0416f9c6225811269b9e5a2407728`  
> 安全边界：本检查点不授权向飞书、企微、任何个人、群聊、bot 或 webhook 发消息

## 1. 为什么建立这个检查点

此前路线要求完成 `IM-P0` 至 `IM-P3` 后才连接独立原生 IM 的专用沙箱。用户现决定提前接入
IM，因此必须先把当前稳定状态、已知缺口、可恢复引用和提前接入边界固定下来，避免后续试验性
adapter、provider schema 或网络配置污染已验证基线。

“提前接入”在本检查点中的含义是：尽早实施 provider-neutral 模型、fake adapter 和独立原生
IM 测试后端的受控集成，并允许在最小安全切片通过后开展 **sandbox inbound-only** 合同探测。
它不等于生产发布批准，也不自动授权真实 outbound send。

## 2. 基线精确状态

| 项目 | 2026-08-27 检查点事实 |
|---|---|
| 当前分支 | `main` |
| 当前、本地 `main`、`origin/main` | `1d399e555fb0416f9c6225811269b9e5a2407728` |
| 工作区 | clean |
| worktree | 仅主 worktree |
| 原生 IM V1 文档 | 已冻结并完成三路独立审计 |
| `IM-P0 CONTRACT_READY` | **未完成** |
| 原生 IM 实现模块/契约测试/迁移 | 尚不存在 |
| Heartbeat worker | admission 模型存在，但 dispatch 明确保持 disabled |
| 真实 IM endpoint/credential | 未进入仓库，也未连接 |
| 飞书/企微/任何聊天写入 | 禁止，且本检查点没有改变该限制 |

合同冻结文件：

- `docs/architecture/NATIVE_IM_CONTRACT_V1.md`
- 合同冻结提交：`250ec56286a09d40de61d460b40fdbd9843e14d6`
- 当前同步库存提交：`1d399e555fb0416f9c6225811269b9e5a2407728`

当前实现具有可复用的 durable invocation lease、Result Receipt/Observed、Artifact、SQLite
transaction、Outbox 与 process-identity 基础，但它们不能被解释为 IM Action Plane 已完成。尤其是
`src/quantum_entanglement/invocation_worker.py` 明确声明未来 worker 尚不能 dispatch；通用
`OutboxPublisher` 也不得直接连接 IM port。

## 3. 三层备份

### 3.1 GitHub 可读恢复分支

```text
backup_0827_200010
  -> 1d399e555fb0416f9c6225811269b9e5a2407728
```

该分支只用于识别和恢复，不承载继续开发。后续实现继续进入 `main` 或短生命周期 worktree；不得
在此备份分支上追加提交。

### 3.2 Git annotated tag

```text
pre-native-im-20260827-200010
  tag object: cf0ff334e4a9543179f9c5e38547a42478ad577c
  peeled commit: 1d399e555fb0416f9c6225811269b9e5a2407728
```

标签用于表示语义稳定点。标签与分支已推送到用户的 GitHub 私有仓库并从远端 refs 回读一致。

### 3.3 离线 Git bundle

离线 bundle 保存在仓库外的统一备份目录：

```text
/Users/lwblx/huapohen/agent/execute/infinite/backups/quantum_entanglement/
```

bundle 文件、SHA-256 和 `git bundle verify` 结果在生成后写入同目录的 manifest；bundle 不进入
项目 Git，避免仓库递归备份自身。

## 4. 非破坏式恢复

优先新建独立 worktree，不切换或覆盖现有工作目录：

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement
git fetch origin backup_0827_200010 \
  refs/tags/pre-native-im-20260827-200010:refs/tags/pre-native-im-20260827-200010
git worktree add \
  /Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement_restore_0827_200010 \
  backup_0827_200010
```

如果原仓库不可用，从离线 bundle 恢复到一个新目录；不要在含未保存改动的工作区执行
`reset --hard`：

```bash
git clone \
  /Users/lwblx/huapohen/agent/execute/infinite/backups/quantum_entanglement/quantum-entanglement-pre-native-im-20260827-200010.bundle \
  /Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement_restore_bundle_0827_200010
```

## 5. 提前接入的最小安全切片

提前连接专用 IM 沙箱前，不等待全部商用加固，但至少必须完成以下内容：

1. provider-neutral V1 strict codec、canonical digest 和关键 golden vectors；
2. inbound verified envelope、认证/签名验证、稳定去重键、cursor/readback 和大小上限；
3. 独立测试 tenant、账号、channel/conversation allowlist 与非敏感合成数据；
4. inbound-only feature flag、总 kill switch、日志 secret/message-body 防泄漏；
5. 入站只进入隔离 inbox/观测链，不直接触发 Agent 执行、tool 或 outbound；
6. fake adapter 与真实 sandbox adapter 共用同一 provider-neutral port；
7. 默认配置不解析真实 endpoint，测试配置与 credential 只通过未入 Git 的 secret/config 注入；
8. 形成精确 source commit、测试结果、回退步骤和 sandbox 批准记录。

完成该切片后，可提前进行以下只读顺序：

```text
contract probe -> health -> inbound read -> dedupe -> cursor resume
```

以下动作仍不得提前：

- 向飞书或企微发送、回复、评论、@、上传或触发 webhook；
- 向任何真实用户、真实客户或生产 conversation 发送；
- 将模型输出或 Agent narration 当作 IM 接收证明；
- 在没有 Action Command、action-time authorization、fence、Receipt 和 unknown reconcile 时发送；
- 在用户没有针对具体测试环境再次明确 outbound 授权时执行 send/edit/delete/reaction。

## 6. 后续执行顺序

1. 先完成 V1 值模型、strict codec、golden vectors 和 authenticated fake adapter；
2. 增加 inbound-only sandbox adapter 与 contract probe，不注册 outbound；
3. 用 fake/sandbox 完成 auth、dedupe、ordering、cursor、disconnect 和 bounded-input 矩阵；
4. 再闭合 Atomic Result Writer 与 heartbeat PURE worker，使入站事件可以安全形成 Agent Result；
5. 单独实现 Action Plane；在此之前 UI/API 只能显示建议动作，不发送；
6. Action Receipt、`effect_unknown` 和 acceptance query 通过后，再申请单个 allowlisted
   conversation 的显式 outbound 测试授权；
7. 每个小改变仍独立 commit；GitHub 持续备份，Notion 改为每个稳定阶段结束后批量同步并远端回读。

## 7. 决策影响

这一决定把“何时开始观察真实 sandbox 协议”前移，但没有把安全事实层、作用域、幂等、恢复和
outbound 证明删除。预期收益是尽早发现 IM 后端的身份、cursor、ACK、acceptance query 与
capability 语义差距；代价是 provider adapter 和平台内核会并行演进，因此必须通过
provider-neutral port、feature flag 和独立备份限制返工范围。

本检查点只记录接入决策和恢复边界。任何后续“已经接入”“已经可发送”结论都必须绑定新的代码
SHA、专用沙箱证据和明确的权限范围，不能从本文件推导。
