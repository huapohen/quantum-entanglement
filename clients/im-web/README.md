# WanWork v0版 Web 客户端

这是第一版可运行 React Web 客户端，使用 TypeScript、Zustand、Tailwind 和 shadcn 风格的无依赖
基础组件。它直接消费当前 loopback IM demo 的 `code/data/message/requestId` envelope，不连接飞书、
企微或真实融云。生产构建同时带有一个只缓存静态 shell 的 PWA manifest/service worker，聊天 API
永远走网络，不会把消息真相写进浏览器缓存。

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
# 同一 Wi-Fi 下用 iPhone/iPad/Android/鸿蒙浏览器体验
./scripts/start_web_client.sh --lan --no-install
```

默认 Agent runtime 是 `synthetic`，不会产生模型网络请求。要在本地 Web 群聊中显式试用
OpenAI-compatible GPT runtime，先在当前终端提供完整配置，再启动：

```bash
export WANWORK_IM_AGENT_RUNTIME=openai-compatible
export WANWORK_IM_MODEL_API_KEY='<从本机 secret manager 注入，不要写入 Git>'
export WANWORK_IM_MODEL_BASE_URL='https://<reviewed-openai-compatible-host>/v1'
export WANWORK_IM_MODEL='gpt-5.6-sol'
./scripts/start_web_client.sh --model-runtime openai-compatible --no-open
```

三个模型变量必须成套提供；启动器不会读取聊天软件凭据，也不会把 Key 写入日志、截图、事件或
页面。模型输出是不可信数据，只会作为 Agent 子群回复，不会获得发送飞书、企微或其他外部消息
的能力。模型调用失败会返回业务依赖错误，不会静默伪装成合成成功。

`--im-port` 会通过 `WANWORK_IM_WEB_API_PORT` 同步 Vite `/api` proxy，不需要手工修改配置。
端口必须是 `1` 到 `65535` 的整数。

`--lan` 仅让 Vite 页面监听局域网，Go API 仍绑定本机回环并由 Vite 代理；设备和开发机需在同一
Wi-Fi，脚本会打印可访问的局域网地址。该模式只用于验收，不要暴露到公网。

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
- 新建群时可勾选邀请当前已安装 Agent（成员 ID 由 Agent Store API 投影提供）；
- 在当前已打开群里点击“邀请到当前群”，通过受保护的成员动作完成幂等拉群；
- 从认证 Agent Store API 读取 Agent 定义、release、Trust Passport 和 installation 投影；
- 新建普通群；
- 普通文本发送、编辑、撤回和 reload 后的消息 projection；
- `@v0版 Agent` 指令；
- 父群关联的 Agent 子群、Invocation、工作卡状态和 Agent 回复；
- 运行模式、auth provider、provider status 与 network-call 安全提示；
- 窄屏响应式布局。

启动后可用以下只读接口复核右侧 Agent Store 卡片（响应仍是 HTTP 200 envelope）：

```bash
curl --fail -H 'Authorization: Bearer demo.local.signature' \
  http://127.0.0.1:18080/api/v1/demo/im/agents
```

本地响应中的 `requestedCapabilities` 是 Agent 发布版本声明，`grantedCapabilities` 是当前租户安装
决定，`dataRoutes` 是经过声明的抽象数据路线，`attestations` 是 Trust Passport 的审阅声明；这些
字段不会被客户端当作凭据，也不会改变 action-time 授权。

安装 API 还接受可选的 `grantedCapabilities` 请求字段。后端逐项校验它必须属于 Trust Passport
声明的 `requestedCapabilities`，因此客户端可以选择最小能力集合；省略字段继续授予完整 reviewed
集合以兼容旧调用。显式空数组、重复项或非法值会被拒绝，已提交幂等 key 改授权集合会返回冲突。
这只是后端授权决策的输入，不能被前端字段当作凭据或越权依据。

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

### PWA 体验

生产构建（`npm run build` 后用静态服务器托管 `dist/`）会暴露 `/manifest.webmanifest`，支持在
桌面浏览器或移动浏览器中“添加到主屏幕”。service worker 只做静态资源 cache-first 和导航离线
回退；`/api/` 请求、登录态和聊天数据不缓存。它是 Web/PWA 交付，不是 `.app`、`.exe`、`.ipa`、
`.apk` 或鸿蒙 `.hap`，原生端仍以后续 Web-first 验收为前置条件。
