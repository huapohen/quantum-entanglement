# v0版全端体验入口（Web/PWA first）

今晚验收以同一套 Web/PWA 作为跨端结果：桌面端直接浏览器打开，手机/平板通过局域网打开，
均共享 Go loopback API、Agent Store、群聊、Agent 子群和 Workboard。

## 桌面端（macOS / Windows / Linux）

在开发 worktree 根目录执行：

```bash
./scripts/start_web_client.sh --no-open
```

打开终端输出的 `http://127.0.0.1:5173`。macOS 可省略 `--no-open` 自动打开浏览器；Windows/Linux
使用 Chrome/Edge/Firefox 打开同一地址即可。

## 手机和平板（iPhone / iPad / Android / 鸿蒙）

让设备与开发机连接同一 Wi-Fi，在开发机执行：

```bash
./scripts/start_web_client.sh --lan --no-open
```

脚本会尽量打印本机局域网地址，例如 `http://192.168.x.x:5173`。在手机浏览器打开该地址即可；
生产构建后可以使用“添加到主屏幕”安装 PWA。设备不能访问时先检查系统防火墙和 Wi-Fi 隔离，
不要为了验收把服务暴露到公网。

## 最短验收路径

1. 保持创建群时邀请 Agent，创建一个群。
2. 在右侧发布自定义指令，进入 Agent 子群并查看回复。
3. 在 Workboard 查看 Task、Artifact 草稿和 Needs You。
4. 点击“接受产物”或“退回”，观察 `accepted/completed` 或 `rejected` 状态。
5. 切回父群确认只有受限工作卡，子群隔离仍成立。

## 交付边界

- 这是当前所有平台可直接体验的 Web/PWA 结果，不是原生安装包。
- 当前 loopback API 默认 synthetic、零外网；不会连接或向飞书、企微、真实融云发送消息。
- `.app/.dmg/.exe/.msix/AppImage/.deb/.ipa/.apk/.aab/.hap` 原生打包属于后续阶段，必须在共同
  API/认证/provider 合同稳定后分别做签名、升级和真机验收。
- 手机访问的是开发机上的本地服务，重启服务会清空 fake 数据；生产持久化仍待 PostgreSQL/W2。
