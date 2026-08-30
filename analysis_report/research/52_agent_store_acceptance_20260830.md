# Agent Store 阶段验收证据（2026-08-30）

## 结论

当前 Web-first IM 分支已交付一个可从浏览器验收的 Agent Store 最小闭环：后端以认证后的
tenant 投影返回已审阅 Agent 的 definition、release、Trust Passport、capability、data route
和 installation；前端展示这些治理信息，并允许把处于 `active` 状态的 Agent 邀请到当前普通群。

该实现是本地 synthetic/fake provider 纵切片，默认零网络，不连接飞书、企微或真实融云，也不
宣称已经是公共 marketplace 或生产安装服务。

## 实现位置

| 层 | 文件/入口 | 作用 |
| --- | --- | --- |
| 值对象与不变量 | `apps/im-api/internal/agentstore/catalog.go` | definition、release、Trust Passport、capability、data route |
| 安装与治理 | `apps/im-api/internal/agentstore/installation.go` | installation 生命周期、授权子集、offboarding 请求 |
| provider 投影 | `apps/im-api/internal/agentstore/provider_projection.go` | 只向 provider 暴露经过审阅的非秘密身份投影 |
| local demo projection | `apps/im-api/internal/localdemo/agents.go` | 认证后的 Agent Store JSON 读模型 |
| HTTP API | `GET /api/v1/demo/im/agents` | 统一 `{code,data,message,requestId}` envelope |
| Web UI | `clients/im-web/src/App.tsx` | Agent Store 卡片、授权能力/数据路线/审阅声明、邀请动作 |

## 当前演示对象

| 字段 | 当前值 |
| --- | --- |
| Agent | `v0版研究 Agent` |
| release | `1.0.0`，`published` |
| installation | `active` |
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
3. 认证请求读取 Agent Store，确认 installation 为 `active`；
4. 动态指令创建 Agent 子群，确认 Agent 回复不进入父群；
5. Task → Artifact → Needs You，接受后确认状态闭环；
6. 业务失败仍通过 HTTP 200 envelope 返回。

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
- 选择普通群并点击“邀请到当前群”；
- 在该群发布自定义指令，进入隔离的 Agent 子群。

## 明确边界

当前还没有交付以下生产能力：第三方 Agent 上传/认领、公共目录搜索、真实制品仓库、签名与
SBOM 扫描流水线、组织审批 UI、PostgreSQL 持久化安装记录、升级/回滚、真实 Clerk/JWKS、
真实融云 provisioning/callback、跨租户 resolver 和原生客户端安装包。后续实现必须保留
“声明、审阅、安装授权、运行时能力”四者分离，不能把目录展示或 `active` 状态当成 bearer
credential。

