# Agent Store 当前状态与下一步（2026-08-30 23:30 +08:00）

状态：`local_pending`。Notion 按当前调度暂不上传，截止时间前统一同步。

## 现在已经可以验收的部分

当前开发分支 `dev_wanwork_quantum_entanglement` 的 Agent Store 已形成一条可运行的 Web-first synthetic
vertical slice：

- 认证后的 catalog projection：definition、published release、Trust Passport、installation 状态；
- 版本 provenance 只读投影：publisher、definition/release/passport revision、发布时间和 SHA-256 摘要；
- 安装动作：action-time Passport 有效期检查、能力子集解析、重复能力拒绝、幂等重放和请求冲突；
- 撤权动作：明确 `retain/archive/delete` 数据处置、provider 成员清理、provider 用户撤销和幂等重放；
- provider effect fail-closed：只接受 `committed`/`replayed`，`unknown` 不推进本地状态；
- `@v0版 Agent` 子群隔离、Task → Artifact → Needs You 审阅闭环；
- 零网络 fake provider 与 loopback HTTP envelope，Web 页面可直接体验。

## PostgreSQL Agent Store 已有的底层能力

- migration `0011`：definition/release/passport/installation head+snapshot 表、复合外键和 FORCE RLS；
- migration `0012`：四个精确参数 `SECURITY DEFINER` CAS 写函数，runtime 仅拥有表 `SELECT` 与函数 `EXECUTE`；
- migration `0013`：数据库侧 capability grammar、requested/prohibitions 不相交和 installation grant 值约束；
- Go tenant repository：读取时重建并重验 domain snapshot，写入时 canonical codec + CAS；
- `UnitOfWork.ExecuteAgentStore`：`agent.*` 命令命名空间、serializable transaction、advisory lock、durable
  receipt、重放和 commit-unknown fresh-connection readback。
- provider effect outbox 已有 provider-neutral contract 与单进程 append+fsync recovery fixture；它能保留
  `queued/sent/unknown/committed/failed` 状态并拒绝过期 lease，但尚未替代生产 PostgreSQL outbox。

## 仍不能宣称生产完成的边界

1. localdemo 安装/撤权尚未切换到 PostgreSQL repository；
2. provider effect outbox 当前只有本地文件 fixture，尚无 PostgreSQL durable 表、function-only 写入口和 worker；
3. runtime 还没有带真实 Clerk tenant/actor context 的 Agent Store HTTP composition；
4. 真实 RongCloud provider、receipt readback、网络重试与 `effect_unknown` 对账矩阵尚未完成；
5. 仍缺 kill-9/断电、backup/restore、旧凭据 drain 和灾备演练。

因此当前结论是：Agent Store 的 Web 验收面和数据库控制平面已具备，生产接入 IM 前仍应先完成 durable
安装/撤权命令与 provider effect outbox/reconcile；不要把 synthetic 成功当作线上外部副作用已完成。

## 本阶段证据

```text
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go test ./... -count=1
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly go vet ./...
npm run build                         # clients/im-web
WANWORK_IM_VERIFY_PORT=18148 ./scripts/verify_web_first.sh
```

上述门禁覆盖 Agent Store 安装/撤权、幂等重放、最小 capability grant、provider unknown fail-closed、子群隔离
和 Workboard 审阅闭环；PostgreSQL integration 在无测试 DSN 时按设计 skip。

## 下一步（按价值排序）

1. 在保持 `UnitOfWork.ExecuteAgentStore` 契约不变的前提下，把安装/撤权的 aggregate mutation 接到 durable
   repository，并让 provider effect 以可对账状态落库；
2. 添加 provider outbox/reconcile worker 与 commit-unknown fresh readback，覆盖重试、拒绝、未决和人工处理；
3. 接入真实身份/Provider adapter 后，再把相同 HTTP 合同从 demo composition 提升为 production composition；
4. 最后执行 crash/restore 矩阵并更新发布门禁，再统一同步 Notion。
