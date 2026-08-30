# Agent Store 阶段验收证据（2026-08-30）

## 结论

当前 Web-first IM 分支已交付一个可从浏览器验收的 Agent Store 最小闭环：后端以认证后的
tenant 投影返回已审阅 Agent 的 definition、release、Trust Passport、capability、data route
和 installation；前端展示这些治理信息，并允许把处于 `available` 状态的 Agent 显式安装到工作
空间，再把处于 `active` 状态的 Agent 邀请到当前普通群；同时提供显式的 offboard/撤权动作，
清理 provider 群成员、本地 conversation 成员/access 投影，并使后续 `@Agent` fail-closed。

该实现是本地 synthetic/fake provider 纵切片，默认零网络，不连接飞书、企微或真实融云，也不
宣称已经是公共 marketplace 或生产安装服务。

## 实现位置

| 层 | 文件/入口 | 作用 |
| --- | --- | --- |
| 值对象与不变量 | `apps/im-api/internal/agentstore/catalog.go` | definition、release、Trust Passport、capability、data route |
| 安装与治理 | `apps/im-api/internal/agentstore/installation.go` | installation 生命周期、授权子集、offboarding 请求 |
| provider 投影 | `apps/im-api/internal/agentstore/provider_projection.go` | 只向 provider 暴露经过审阅的非秘密身份投影 |
| local demo projection | `apps/im-api/internal/localdemo/agents.go` | 认证后的 Agent Store 投影与安装动作 |
| local demo offboard | `apps/im-api/internal/localdemo/agents_offboard.go` | 幂等数据处置、provider 成员移除、用户撤权、本地投影清理 |
| HTTP API | `GET /api/v1/demo/im/agents`、`POST /api/v1/demo/im/agents/:definitionId/install`、`POST /api/v1/demo/im/agents/:definitionId/offboard` | 统一 `{code,data,message,requestId}` envelope |
| Web UI | `clients/im-web/src/App.tsx` | Agent Store 卡片、授权能力/数据路线/审阅声明、安装、邀请、停用并撤权 |

## 当前演示对象

| 字段 | 当前值 |
| --- | --- |
| Agent | `v0版研究 Agent`（预装）与 `v0版规划 Agent`（可安装） |
| release | `1.0.0`，`published` |
| installation | 预装项 `active`；可安装项 `available`，安装后变为 `active` |
| runtime isolation | `process`（local fake 只用于合同验收） |
| granted capability | `conversation.read` |
| data route | `conversation.context → local, provider:rongcloud` |
| Trust Passport | `publisher_verified`、`security_reviewed`、`data_routes_reviewed` |

## 可复核命令

在本分支 worktree 根目录执行：

```bash
./scripts/verify_web_first.sh
```

该门禁实际完成以下检查：

1. React/Vite TypeScript 生产构建；
2. 启动 synthetic loopback API；
3. 认证请求读取 Agent Store，确认同时存在 `active` 和 `available` 条目；
4. 通过安装 API 以 idempotency key 安装 `agd_local_planner`，确认新 Agent actor 和 `active` 状态；
5. 重放相同安装请求，确认 `replayed=true` 且不创建第二个安装；
6. 动态指令创建 Agent 子群，确认 Agent 回复不进入父群；
7. Task → Artifact → Needs You，接受后确认状态闭环；
8. 发布已接受 Artifact 的父群引用，并确认重复发布 `replayed=true`；
9. 对已安装 Agent 执行 `dataDisposition=archive` 的 offboard，确认 installation 变为
   `offboarded`、返回被清理的 parent/child conversation IDs，并确认重复撤权请求幂等回放；
10. offboard 后再次 @Agent，确认业务 envelope 为 `code=40301`，不会创建新 invocation、子群或消息；
11. 业务失败仍通过 HTTP 200 envelope 返回。

专项 Go 测试：

```bash
cd apps/im-api
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./internal/agentstore ./internal/localdemo ./internal/app -count=1
```

## 验收路径

```bash
./scripts/start_web_client.sh --no-open
```

打开 `http://127.0.0.1:5173`，右侧 Agent Store 卡片中可以：

- 查看 Agent 名称、release、installation 状态；
- 查看 release 请求能力与租户实际授予能力的区别；
- 查看数据路线和 Trust Passport 审阅声明；
- 对 `available` Agent 点击“安装到当前工作空间”，安装后可见 Agent actor；
- 选择普通群并点击“邀请到当前群”；
- 在该群发布自定义指令，进入隔离的 Agent 子群。
- 对已安装 Agent 选择 `retain`、`archive`（默认）或 `delete` 后点击“停用并撤权”，确认二次确认
  后 installation 变为 `offboarded`，群成员投影被清理；撤权后再次发布指令应显示业务拒绝。

安装后的 Agent 生成 Artifact 并经人工接受后，Workboard 还可执行“发布引用到父群”；父群只会
看到带 Artifact ID/digest 的引用消息，不会自动展开或复制产物正文。该发布动作同样是幂等的。

## Action-time Trust Passport 加固（`6e039d2`）

目录卡片只是发现投影，不能被当成执行授权。当前安装动作会在持有服务锁时重新检查：

- definition 必须为 `active`；
- release 必须为 `published`；
- Trust Passport 必须仍为 `active` 且所有审阅声明未过期。

同一检查也应用于 `@Agent` invocation；已安装 Agent 的 Passport 过期或被撤销后，新调用立即
fail-closed，而不会因为旧 membership 投影仍存在就继续运行。新增测试覆盖“目录仍保留但安装/调用
拒绝”的 action-time 边界。该检查是本地纵切片的安全加固，不替代生产的持久化 resolver、撤销广播和
action-time PEP。

## 安装时最小权限能力选择（本阶段增量）

安装 API 接受可选的 `grantedCapabilities` 数组，用于把经过 Trust Passport 审阅的能力声明
进一步收窄到租户实际需要的最小集合：

```json
{
  "idempotencyKey": "manual/store/install/planner-least-privilege",
  "grantedCapabilities": ["conversation.read"]
}
```

后端在安装动作锁内重新解析并规范化能力值，再逐项调用当前 Trust Passport 的授权判断；列表中
任何未在 release `requestedCapabilities` 中声明、或被 Passport prohibition 禁止的能力都会以
`40301` 业务错误拒绝，无法借由已 active 的安装 replay 绕过。语法错误、重复项和显式空数组分别
以校验失败处理；能力列表的排序和 release/制品/manifest/persona digest 会纳入幂等请求指纹，
同 key 改变授权集合会返回 `40902`，不会静默改变既有安装。

省略 `grantedCapabilities`（或保持旧客户端不发送该字段）仍按历史行为授予 Passport 声明的完整
requested 集合，以保持兼容；这不是扩大权限，因为集合完全由已审阅 Passport 决定。安装响应同时
返回 `requestedCapabilities` 与实际 `grantedCapabilities`，验收时应确认后者是前者的严格子集或
相等集合。该 local demo 仍是内存 synthetic 实现；生产需要在 tenant-bound durable UoW 中持久化
canonical grant set、版本化授权决策并在每次 action-time 解析。

## Offboard / 撤权闭环（`0427a8c`、`d63ae39`、`62a5ca0`）

当前 synthetic/fake provider 已提供可验证的生命周期尾端：

1. `POST /api/v1/demo/im/agents/:definitionId/offboard` 要求调用方显式选择
   `retain | archive | delete` 数据处置策略，并携带幂等 key；
2. 对 provider-bound 的 parent/child 群先执行声明了 `ProviderCapabilityMemberWrite` 的 member
   removal，再执行声明了 `ProviderCapabilityUserRevoke` 的 Agent 普通用户 revoke；缺失任一能力
   时 fail-closed；
3. 两类 provider effect 都具备 committed/replayed/conflict 语义，任一步失败都不会标记本地成功；
4. 本地 installation 迁移到 `offboarded`，清除所有 conversation 的成员和 access 投影；当没有
   active installation 时，移除 requester 的 `InvokeAgent` 权限；
5. 相同 key+策略重试返回原结果并标记 `replayed=true`，相同 key 改策略返回冲突；撤权后 invocation
   在动作时重新检查 installation/status，直接 fail-closed。

这是一条 fake provider 的纵切片，证明的是合同、顺序、幂等和拒绝边界。生产接入仍需真实 provider
callback/readback、durable UoW、outbox/inbox、reconcile worker、credential/session lease revoke、
审计事件和租户隔离事务，不能把本地撤权结果当作生产完成。

## 明确边界

当前还没有交付以下生产能力：第三方 Agent 上传/认领、公共目录搜索、真实制品仓库、签名与
SBOM 扫描流水线、组织审批 UI、PostgreSQL 持久化安装记录、升级/回滚、真实 Clerk/JWKS、
真实融云 provisioning/callback、跨租户 resolver 和原生客户端安装包。后续实现必须保留
“声明、审阅、安装授权、运行时能力”四者分离，不能把目录展示或 `active` 状态当成 bearer
credential。
