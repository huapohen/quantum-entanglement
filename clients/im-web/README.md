# WanWork v0版 Web 客户端

这是第一版可运行 React Web 客户端，使用 TypeScript、Zustand、Tailwind 和 shadcn 风格的无依赖
基础组件。它直接消费当前 loopback IM demo 的 `code/data/message/requestId` envelope，不连接飞书、
企微或真实融云。

## 本地运行

### 一条命令启动（推荐）

在仓库根目录执行：

```bash
./scripts/start_web_client.sh
```

脚本会检查 Go、npm 和 lockfile，首次运行时用 `npm ci --ignore-scripts` 安装依赖，然后启动
loopback-only Go IM demo 和 Vite Web。macOS 会自动打开浏览器；回到同一个终端按 `Ctrl-C` 会
同时停止前端和后端。Go 首次冷编译可能需要一两分钟，脚本最多等待 180 秒并在 10 秒后提示仍在
等待。可用 `--no-open` 关闭自动打开浏览器，或用 `--no-install` 强制要求依赖已存在：

```bash
./scripts/start_web_client.sh --no-open
./scripts/start_web_client.sh --no-install --im-port 19080 --web-port 5174
```

`--im-port` 会通过 `WANWORK_IM_WEB_API_PORT` 同步 Vite `/api` proxy，不需要手工修改配置。
端口必须是 `1` 到 `65535` 的整数。

终端一：启动 Go IM demo：

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement
./scripts/start_im_demo.sh --port 18080
```

终端二：启动 Vite Web 客户端：

```bash
cd /Users/lwblx/huapohen/agent/execute/infinite/quantum_entanglement/clients/im-web
npm install
npm run dev
```

打开 <http://127.0.0.1:5173>。Vite 只监听 `127.0.0.1`，`/api` 代理到 `127.0.0.1:18080`。

如果需要使用不同的后端端口，修改 `vite.config.ts` 的 proxy target；当前本地 fake bearer token
是公开的 demo fixture，不可用于生产。

## 当前可验收范围

- 左侧工作空间与群列表；
- 新建普通群；
- 普通文本发送、编辑、撤回和 reload 后的消息 projection；
- `@v0版 Agent` 指令；
- 父群关联的 Agent 子群、Invocation、工作卡状态和 Agent 回复；
- 运行模式、auth provider、provider status 与 network-call 安全提示；
- 窄屏响应式布局。

## 生产边界

当前页面是 fake/demo client：token、群成员和消息均来自本地 demo 服务；没有 Clerk 登录、真实
多租户 session、文件上传、SSE resume、真实融云 outbound、离线缓存或生产错误重试。生产版必须
改用服务端签发的 session、action-time tenant/Actor resolver、统一 API schema 和 provider
capability negotiation，不能把 `VITE_LOCAL_BEARER_TOKEN` 或客户端字段当作授权证明。

构建检查：

```bash
npm run build
```

构建产物位于 `dist/`，已被 Git 忽略；依赖锁文件 `package-lock.json` 需要提交。
