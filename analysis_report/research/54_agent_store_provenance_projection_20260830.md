# Agent Store 版本 provenance 与安全 digest 投影（2026-08-30）

## 目的

Agent Store 详情需要让验收者确认“当前看到的 Agent 版本到底对应哪个不可变发布内容”，但不能把
manifest/persona 原文、制品字节、Secret locator 或任何运行时凭据送进浏览器。本阶段在 Web-first
synthetic 纵切片中补齐只读的版本 provenance 投影：后端从已审阅的 `TrustPassport.Release()` 计算字段，
Web 只展示摘要，不参与授权决策。

## API 投影

`GET /api/v1/demo/im/agents` 的每个 `agent` 增加以下只读字段：

| 字段 | 内容 | 安全边界 |
| --- | --- | --- |
| `artifactDigest` | 制品内容的 SHA-256 小写 64 位十六进制摘要 | 只给内容身份，不给制品字节或下载地址 |
| `manifestDigest` | 权限、数据路线等 release manifest 的 SHA-256 摘要 | 不返回 manifest 原文、SecretRef 或 token |
| `personaDigest` | persona 配置的 SHA-256 摘要 | 不返回 prompt/persona 原文 |
| `versionProvenance.publisherId` | 发布者目录 ID | 目录证据，不是认证凭据 |
| `versionProvenance.definitionRevision` | definition 修订号 | 只读 catalog 证据 |
| `versionProvenance.releaseRevision` | release 修订号 | 只读 immutable release 证据 |
| `versionProvenance.passportRevision` | Trust Passport 修订号 | 只读审阅快照证据 |
| `versionProvenance.publishedAt` | UTC RFC3339Nano 发布时间 | 不代表当前运行授权仍有效 |
| `versionProvenance.digestAlgorithm` | 固定为 `sha256` | 与三个摘要字段配套，避免算法歧义 |

摘要值保持与 `agentstore.SHA256Digest.Hex()` 一致的 canonical 小写 64 位十六进制格式；客户端不应把
摘要当作下载凭据、签名或 bearer token。`definitionStatus`、`releaseStatus`、`passportStatus` 和
动作时 Trust Passport 校验仍是安装、调用和撤权的准入依据，provenance 仅用于审阅和排查。

## Web 展示

Agent Store 卡片新增“版本 provenance”区域：显示 digest 算法、publisher、发布时间和三类 revision，
并把 artifact/manifest/persona 摘要压缩为可读短串；鼠标悬停可查看完整摘要。完整摘要仍只是 hash，
不含制品内容或凭据。旧状态的安装、最小能力选择和 retain/archive/delete 撤权控件保持不变。

## 验证证据

新增 `TestAgentStoreProjectionExposesSafeVersionProvenance`，逐个检查：

1. 三类摘要均能按 SHA-256 canonical 格式解析，且与后端 release 快照完全一致；
2. publisher、definition/release/passport revision 和 UTC 发布时间与同一 Trust Passport 一致；
3. JSON 投影不含 `api_key`、password、secret、credential 等凭据标记。

执行：

```bash
cd apps/im-api
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./internal/localdemo -run TestAgentStoreProjectionExposesSafeVersionProvenance -count=1
GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS=-mod=readonly \
  go test ./internal/localdemo ./internal/app ./internal/agentstore -count=1
cd ../../clients/im-web
npm run build
```

## 生产边界

本提交只实现 authenticated localdemo 的只读投影与 Web 观察面。进入生产前仍必须由真实 tenant-bound
repository 读取已批准的 release/package snapshot，并把 artifact signature、SBOM、provenance
attestation、撤销状态、审计事件和下载授权分别纳入 host-owned admission；任何 digest 漂移必须在
安装、升级、启动和 action-time invocation 前 fail-closed。当前 synthetic/fake provider、内存 catalog
和 loopback API 不构成真实制品仓库、签名验证或生产供应链证明。

本文件按当前工作约定保留在本地/Git `local_pending`，暂不上传 Notion。
