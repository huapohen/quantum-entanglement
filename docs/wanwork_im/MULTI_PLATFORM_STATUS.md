# v0版多端应用状态与交付路线

更新时间：2026-08-30
分支：`dev_wanwork_quantum_entanglement`
状态：本地优先，未同步 Notion

## 结论先行

当前仓库有一个可运行的浏览器 Web 产品体验切片和一个本地 Go IM API demo，**没有可安装的
iOS、iPadOS、Android、鸿蒙、macOS、Windows 或 Linux 原生客户端**。仓库中出现的 mobile
截图是同一 Web 页面在移动 viewport 下的响应式验收证据，不是 IPA、APK、HAP 或桌面安装包。

因此当前能直接体验的是：

1. 在 macOS/Windows/Linux 桌面浏览器打开本地 Web 体验；
2. 使用 `scripts/start_web_client.sh --lan`，让同一 Wi-Fi 下的 iPhone/iPad/Android/鸿蒙浏览器直接体验同一 Web/PWA；
3. 用生产构建部署 `dist/` 后，可在支持 PWA 的浏览器中添加到主屏幕；
4. 使用 Go IM API demo 检查本地 HTTP envelope、路由和 fake 数据边界。

手机或另一台电脑可在显式 `--lan` 模式下通过 Vite 代理访问 Web；Go API 仍只绑定 `127.0.0.1`，
不要将该验收服务暴露到公网。

## 当前交付矩阵

| 平台 | 当前交付物 | 能否安装 | 当前证据 | 当前状态 |
| --- | --- | --- | --- | --- |
| Web（桌面浏览器） | `scripts/start_web_client.sh` + React/Vite | 无需安装 | `docs/wanwork_im/LOCAL_IM_ACCEPTANCE_GUIDE.md`、Playwright 截图 | 可体验 |
| Web/PWA（移动 viewport/真实设备） | 同一 Web 页面 + manifest/service worker + `--lan` | 可添加到主屏幕，但不是原生安装 | `clients/im-web/public/manifest.webmanifest`、响应式构建、局域网启动器 | 可体验/可安装 Web 壳，不是原生 App |
| macOS | 无 `.app/.dmg` | 否 | 无原生 bundle、签名或安装验收 | 未开始 |
| Windows | 无 `.exe/.msix` | 否 | 无 Windows 构建流水线或签名产物 | 未开始 |
| Linux | 无 AppImage/deb/rpm | 否 | 无 Linux 打包与运行验收 | 未开始 |
| iPhone/iPad | 无 `.ipa`、TestFlight 或 Xcode 工程 | 否 | 无 iOS/iPadOS target | 未开始 |
| Android | 无 `.apk/.aab` 或 Gradle mobile 工程 | 否 | 无 Android target | 未开始 |
| 鸿蒙 | 无 `.hap`、DevEco/ArkTS 工程 | 否 | 无 HarmonyOS target | 未开始 |
| Go IM API | `scripts/start_im_demo.sh`、`apps/im-api` | 服务进程形式 | Go 单测、loopback demo、HTTP envelope | 本地 fake/demo |

“未开始”只表示该平台的客户端工程和可安装产物尚未交付，不表示产品架构没有考虑该平台。

## 现在如何体验

### Web 协同 Agent 切片

在当前 worktree 根目录运行：

```bash
./scripts/start_local_trial.sh
```

它会在本机启动 loopback 服务并打开浏览器。页面可提交任意自定义指令，显示确定性 DAG、模型
narration、三个 Markdown Artifact、Needs You 和事件时间线。默认不会连接或发送飞书、企微、
融云消息。

离线无模型体验：

```bash
./scripts/start_local_trial.sh --synthetic
```

### IM API demo

```bash
./scripts/start_im_demo.sh
```

该 demo 用于核验 API envelope、基础 IM fake projection 和本地生命周期，不代表真实 Clerk、
融云 outbound 或生产部署已经打开。完整步骤见 `docs/wanwork_im/LOCAL_IM_ACCEPTANCE_GUIDE.md`
和 `apps/im-api/README.md`。

## 为什么现在还没有原生 App

当前优先收口的是跨端共同依赖的业务真相和安全边界：

- Clerk verified subject 不能直接充当平台 principal、tenant 或 Actor；
- identity binding、human principal、tenant membership、human Actor 必须在同一数据库快照中
  重新解析；
- IM 消息只是 transport/projection，不能冒充 Task、Invocation、Artifact、Approval 或审计真相；
- 所有客户端必须使用统一的 HTTP envelope、cursor、idempotency、错误码和 capability matrix；
- `@Agent` 创建父群关联的工作子群，消息、Mention、Task 和 Artifact 的生命周期要先冻结；
- callback、replay、outbox、effect-unknown、恢复和权限门禁尚未达到可让原生客户端放心依赖的程度。

如果现在先做多个原生壳，容易把尚未稳定的 provider 字段、客户端 tenant/actor 参数和临时消息
状态固化进各端，后续会产生多套不可兼容的行为。因此原生端不是被忽略，而是排在共同合同稳定
之后。

## 推荐实现顺序

### P0：共同客户端合同（当前主线之后）

先完成并版本化：

1. RFC 6750 Bearer middleware 与可信请求上下文；
2. action-time membership/access resolver；
3. 统一 `code/data/message/requestId` envelope、分页 cursor 和幂等键；
4. message/edit/recall/reaction/file/mention 的 canonical event 与 projection；
5. Agent Store、安装/授权、父群和 Agent 子群的 API；
6. SSE/WebSocket resume、断线、重放和 capability negotiation；
7. OpenAPI/JSON Schema 与跨语言 contract tests。

### P1：Web/PWA 作为第一可交付客户端

沿用规划中的 React + shadcn/ui + Tailwind + Zustand：

- 先把当前 demo 页面拆为可维护的 `im-web` workspace；
- 同一套 TypeScript domain/API client 用于浏览器和 PWA；
- 加入登录、组织/联系人、群聊、消息、搜索、文件、已读和 Agent Store；
- PWA 只在明确的缓存/离线策略完成后开启，不能把聊天真相放在浏览器缓存里；
- 首个多用户验收仍只使用 sandbox/fake provider。

### P2：桌面端（macOS、Windows、Linux）

建议采用 Tauri 2 壳，共用 `im-web` UI 和 TypeScript API client，原生能力通过窄插件端口暴露：

- macOS：`.app/.dmg`、签名/公证、自动更新回滚；
- Windows：`.msix` 或签名 `.exe`、安装升级和代理网络验收；
- Linux：AppImage + deb（发行版矩阵明确后再扩展 rpm）；
- 每个桌面端都必须有相同的 auth/session、深链接、文件选择、通知和安全存储合同；
- 原生壳不能绕过 HTTP/API authorization，也不能直接写 IM 数据库。

### P3：iPhone/iPad、Android、鸿蒙

移动端应在 Web/API 合同稳定后再做，建议保持共享业务层、按平台处理系统能力：

- iPhone/iPad：React Native/TypeScript 共享 API/domain；Xcode target、Keychain、APNs、后台
  生命周期和 TestFlight 验收；
- Android：同一共享 API/domain；Keystore、FCM、后台限制、APK/AAB 和 Play 内测验收；
- 鸿蒙：ArkTS/ArkUI 原生 target（或经过正式兼容性验证的跨端方案）；应用签名、分布式能力、
  Push 和 `.hap` 验收；
- 三个平台都必须以服务端 cursor/resume 为准，不能把本地通知送达或本地 optimistic UI 当作
  消息已提交；
- 真实融云 SDK 接入必须在 provider adapter、signature/replay、external ID mapping、outbox
  和 kill switch 通过后进行。

## “什么时候可以接 IM”与多端的关系

原生客户端可以在真实融云 outbound 之前开始做 UI 和 fake adapter，但要让各端接入真实原生 IM，
至少需要先通过：

- trusted identity context 与 action-time access；
- canonical message/event、inbox dedupe、cursor/resume；
- 父群/Agent 子群关联和 Mention 路由；
- provider binding、签名 webhook、nonce/replay 与 ACK unknown/reconcile；
- durable Task/Attempt/Artifact/Acceptance、outbox 和恢复矩阵；
- 客户端 contract tests、桌面/移动最低支持版本和隐私/密钥存储审计。

在这些条件前，Web demo 可以继续用于产品交互验收；原生端即使能编译，也只能标记为 prototype，
不能宣称已接入生产 IM。

## 验收命名规则

以后新增客户端必须同时提交：

1. 平台工程目录和构建锁文件；
2. 可重复的本地启动/构建命令；
3. 安装包 checksum、版本和 commit；
4. 桌面/移动真实设备或模拟器截图；
5. 端到端 contract/e2e 测试证据；
6. 明确标注 fake、sandbox、real provider 和 production 的边界。

只有 viewport 截图、静态 HTML 或“能打开页面”不能计为对应平台 App 交付。
