# Report sync bundle 运行手册

- 适用版本：`quantum-entanglement.report-sync-bundle` schema v3
- 更新日期：2026-08-26
- 实现：`scripts/report_sync_bundle.py`
- 测试：`tests/test_report_sync_bundle.py`

## 1. 先明确边界

Report sync bundle 是一个**确定性的本地同步库存与证据包**。它回答的是：当前仓库有哪些报告源、
每个源准备映射到 Notion 或语雀的哪个页面、历史 manifest 的摘要是否仍与本地字节一致、受限截图
有哪些，以及生成后的库存是否仍精确绑定当前本地内容。

它不是 Notion/语雀同步器，也不是远端状态探针。运行脚本时不会访问或写入 Notion、语雀、飞书、
企微或其他服务，不会读取浏览器登录态，也不会发送消息。bundle 中所有
`liveReadbackPerformed` 和 `accessPolicy.liveRemoteReadbackPerformed` 必须为 `false`；schema 不接受
`remote_verified`。

因此：

- `historical_manifest_claim_digest_match` 只表示历史 manifest 曾记录回读，且它保存的摘要仍匹配
  当前本地字节；不表示本次运行访问或确认了远端；
- `local_pending` 表示文件已进入本地库存，但没有与当前字节匹配的历史 manifest 声明；
- 只有另一个经过明确授权的远端写入流程真正完成写入并回读后，才可以更新历史 manifest；随后
  应重新生成 bundle；
- 不得根据 bundle 推导“Notion 已同步”“语雀已同步”或“远端内容仍一致”。

## 2. 输入与输出

脚本只读取仓库内受控路径：

- canonical Markdown：`analysis_report/README.md`、主报告、`research/*.md` 与截图说明/manifest；
- 语雀镜像 Markdown：`analysis_report/yuque_sync/source/*.md`；
- 历史控制文件：`analysis_report/notion_sync_manifest.json`、
  `analysis_report/yuque_sync/mapping.json`、`analysis_report/screenshots/manifest.json`；
- 截图：`analysis_report/screenshots/` 下受命名规则和 manifest 约束的 PNG/JPEG。

截图目录还允许三个固定名称的本地验收 sidecar JSON：
`local_im_acceptance_manifest.json`、`local_im_basic_acceptance_manifest.json` 和
`local_im_edit_recall_acceptance_manifest.json`。它们只作为受控 loopback 证据输入，必须是合法
JSON 且继续经过凭据字段扫描；不会被当成远端同步声明，也不会替代主 `manifest.json` 的逐图摘要。

持久输出只能是仓库内
`analysis_report/report_sync_bundles/<name>.json` 的直接子文件。默认不会覆盖已有文件；只有显式
传入 `--overwrite` 才会请求替换。建议每个阶段使用新的、带日期或里程碑的文件名，把旧 bundle
作为该时点的不可变本地证据保留。成功覆盖使用原子 exchange：新文件进入目标名，旧 inode 保留
在同目录随机的 `.report-sync-*` 隐藏 recovery 名下；工具不会自动删除 recovery。这里的 recovery
保证是旧 inode 的 namespace 留存，不是对旧文件既有数据的 crash-durability 承诺：工具不会在
exchange 前替此前的 writer 补做旧目标 `fsync`，所以旧文件在本轮前尚未持久化的写入仍可能在
断电或内核崩溃后丢失。

bundle 顶层包含：

| 字段 | 含义 |
|---|---|
| `format` / `schemaVersion` | 固定格式标识与严格 schema 版本 |
| `accessPolicy` | 截图分级、不可公开分发和“未做实时远端回读”边界 |
| `controls` | 三个历史控制文件的路径、字节数和摘要 |
| `sourceTargets` | `(path, target)` 库存、双摘要、条目标识、真实/计划目标页标识与状态 |
| `images` | 受限截图的类型、尺寸、字节数、摘要和脱敏状态 |
| `sourceSummary` | 源文件数与总字节数 |
| `previousManifestDiagnostics` | 历史映射相对当前库存的缺失、陈旧与额外项 |
| `imageDiagnostics` | 固定的未登记图片 fail-closed 策略；成功结果不会携带例外数组 |
| `normalization` / `statusSemantics` | Markdown/JSON 规范化规则与状态的内嵌定义 |

同一个 canonical source 可以同时拥有 Notion 和语雀目标；`sourceTargets` 以 `(path, target)`
区分。每条记录的 identity 必须按下面三层理解：

- `entryKey`：仅标识一个本地 `(path, target)` 条目，由二者确定；历史映射、摘要或状态变化不会
  改变它；
- `targetPageKey`：历史 control manifest 中真实存在的 Notion page key 或语雀 `yuque_slug`；
  多个 Notion source 可以共享同一个值；
- `proposedTargetPageKey`：没有历史映射时生成的确定性本地计划 key，不代表远端页面已创建、
  存在、写入或回读。

`targetPageKey` 与 `proposedTargetPageKey` 恰好一个非空。已知历史目标即使摘要漂移，仍保留真实
`targetPageKey`，但状态降为 `local_pending`。Notion 页面绑定多个 source 时，只有历史 manifest
声明已回读、所有绑定 source 均存在且 raw digest 全部匹配，该页所有条目才统一得到
`historical_manifest_claim_digest_match`；任一 source 缺失或漂移会让整页条目统一降级。语雀的真实
页标识直接来自 mapping 的 `yuque_slug`，不会用本地生成 key 冒充。

因此绝不能把“源文件数”“source-target entry 数”和“远端唯一页面数”混为一谈。例如同一
Notion 页面绑定两个 source 时会有两个 entry，但只有一个历史页面 identity。

schema v3 verifier 严格拒绝 v2。v2 没有完整保留语雀 slug，并混淆了 Notion 次要 source 的页面
identity，不能只靠旧 bundle 无损升级；应在相同 checkout 和 controls 上用当前生成器创建一个新
checkpoint 文件，保留旧文件。如需验证旧格式，使用产生它的历史提交，不要覆盖式“修 JSON”。

## 3. 快速使用

项目当前兼容窗口为 Python 3.9–3.13。以下命令均可在仓库根目录执行；脚本本身也会从文件位置解析
默认仓库根目录。

仅预览 canonical JSON，不落盘：

```bash
python3 scripts/report_sync_bundle.py
```

生成一个新 checkpoint：

```bash
python3 scripts/report_sync_bundle.py \
  --output analysis_report/report_sync_bundles/checkpoint-20260826.json
```

验证保存的 checkpoint 与当前仓库报告字节仍完全一致：

```bash
python3 scripts/report_sync_bundle.py \
  --verify analysis_report/report_sync_bundles/checkpoint-20260826.json
```

成功时 verifier 只输出：

```text
report-sync bundle verified
```

若确实要替换同名 checkpoint，必须显式声明：

```bash
python3 scripts/report_sync_bundle.py \
  --output analysis_report/report_sync_bundles/checkpoint-20260826.json \
  --overwrite
```

覆盖会改变该文件表达的历史时点，常规阶段发布不应这样做。优先创建新文件；只有修复同一阶段
尚未发布的错误时才考虑 `--overwrite`，并在提交前审阅完整 diff。覆盖成功后应核对目标文件和
隐藏 recovery；只有确认没有并行 writer、目标已验证且 recovery 已另行留存时，才由操作员在工具
之外处置 recovery。不要把删除 recovery 做成自动步骤。

## 4. 推荐的阶段发布流程

1. 完成并审阅 canonical 报告、语雀镜像和截图 manifest 的修改；
2. 单独提交内容修改，避免把报告事实变更和生成器实现混成一个不可审计提交；
3. 选择一个未使用的 checkpoint 文件名并执行 generate；
4. 立即对该文件执行 `--verify`；
5. 审查 source 数、source-target entry 数、两个 target 的分布、共享的真实页面 identity、
   `local_pending` 项、历史 manifest diagnostics、未登记图片 fail-closed 策略和所有
   live-readback flag；
6. 运行专项测试和静态门禁；
7. 将工具/测试/文档与生成的 checkpoint 按阶段提交；
8. 在最终提交的隔离 checkout 中重跑完整仓库门禁。

生成和验证的最小门禁：

```bash
python3 -m unittest tests.test_report_sync_bundle -v
ruff check scripts/report_sync_bundle.py tests/test_report_sync_bundle.py
ruff format --check scripts/report_sync_bundle.py tests/test_report_sync_bundle.py
mypy --strict --python-version 3.9 scripts/report_sync_bundle.py tests/test_report_sync_bundle.py
python3 -m py_compile scripts/report_sync_bundle.py tests/test_report_sync_bundle.py
git diff --check
```

完整仓库发布仍以 `docs/production/RELEASE_GATES.md` 为准。bundle 验证通过不是生产晋级、签名、
远端同步证明或完整 release evidence。

## 5. 安全与完整性控制

实现采用 fail-closed 输入验证：

- 一个 pinned read session 从已固定的 repository root fd 开始，逐级用 descriptor-relative
  `openat` 打开中间目录；每级要求 directory + no-follow，并持有到 generate/verify 结束；
- regular input 使用 `O_NOFOLLOW | O_NONBLOCK`、`fstat` 类型检查和有界 `pread`，因此 leaf 在
  检查窗口被换成 FIFO、socket、device 或 symlink 时会固定失败而不会阻塞；
- source、control、image 以及 verifier 的 bundle fd 都贯穿解析、整仓生成和比较；成功返回前会
  重新全量读取并计算摘要，复核 mode/owner/size/mtime/ctime、可见目录项到 inode 的绑定、中间目录
  绑定以及受控目录的完整 inventory；verifier 最后再次复核 pinned bundle；
- 只接受 regular file/directory，拒绝 symlink、路径逃逸、敏感路径组件和异常文件名；
- 对 Markdown、manifest、图片、目录项、文件数、总字节数、JSON 深度和 JSON 结构设置硬上限；
- JSON 使用严格 UTF-8、拒绝重复 key、非有限数字和浮点；bundle 输出 schema 拒绝未知字段；
  历史输入 manifest 只严格校验本工具实际消费的字段，不声称拒绝所有未知输入字段；
- Markdown 在摘要前做 UTF-8、BOM、换行与 NFC 规范化，同时保留 raw SHA-256；
- Markdown 和 JSON 执行递归字段与常见 token/header 形态的凭据扫描；它覆盖列表、表格、内联/
  压缩 JSON、Authorization、Cookie 和常见 credential key，允许明确的环境变量或脱敏占位符；
  错误只输出固定、非敏感 code，不回显被拒绝内容；
- PNG 使用受限流式解压并校验 chunk、scanline、filter、尺寸和解码预算；
- JPEG 校验 marker 顺序、frame/scan、量化表、Huffman 表、restart 与尺寸引用关系；
- 输出目录用 descriptor-relative 操作和 pinned candidate/target fd 防止 inode ABA；create 在
  Linux 使用 `renameat2(RENAME_NOREPLACE)`、macOS 使用 `renameatx_np(RENAME_EXCL)`；overwrite
  使用 `RENAME_EXCHANGE`/`RENAME_SWAP`，不调用覆盖式 `rename`、`replace` 或按名称 `unlink`；
- staging 使用 `O_RDWR` 创建，经显式 `os.write` 完整短写循环写入后改为 `0400` 并 `fsync`；
  publish 前后分别对候选和
  旧目标的 pinned fd 重新读取，比较 inode、mode、owner、link count、size、mtime 和摘要，同
  inode、同长度的原地改写也会失败；
- 原子 publish 后执行双端 inode/content、目录 `fsync` 和目录绑定复核，不自动 rollback；并发
  冲突或提交结果不确定时保留所有仍有名字的 inode，交由操作员核验；
- 输出为 sorted UTF-8 canonical JSON，恰好一个末尾 LF，验证时重新生成并逐字节比对。

这些控制把读取窗口绑定到一组 pinned inode，并在成功返回前检测可见绑定和内容漂移，但普通
POSIX 文件系统并不提供跨多个文件的原子快照：拥有同一 UID 写权限的进程仍可能在最后一次复核
之后立即改写文件。输出 post-check 之后也存在同样的剩余窗口。这些控制降低了本地报告库存被
路径、文件类型、解析器放大和并发替换欺骗的风险，但不能证明工作站本身可信，也不能替代只读
checkout、权限隔离、文件系统快照、磁盘加密、恶意软件防护、签名或可信 CI。
原子 publish 当前只支持提供上述原生 primitive 的 Linux/macOS 文件系统；平台、libc 或文件系统
不支持时固定报错并停止，不会降级到可能覆盖无关 inode 的实现。

## 6. 截图数据处理

所有截图强制标记为 `restricted-internal` 且
`notForPublicDistribution: true`。bundle 记录图片元数据与摘要，不复制图片字节，也不会上传图片。

提交或交付前必须人工确认：

- `analysis_report/screenshots/manifest.json` 中每张图片的脱敏状态真实；
- `imageDiagnostics.unmanifestedPolicy` 必须是 `fail-closed`；磁盘中出现任何未登记图片都会让
  generation 直接以 `unmanifested_image_forbidden` 失败，不存在成功 bundle 内的例外数组；
- 没有 API Key、cookie、Authorization header、个人隐私或不必要聊天内容；
- 接收方和存储位置允许处理 restricted-internal 材料。

凭据扫描是 defense-in-depth heuristic，不是完整 DLP，也不保证检测所有编码、拆分或新型凭据；
“通过扫描”不等于完成隐私、商业秘密和个人信息审查。图片像素不会被文本扫描替代，仍需要独立
人工复核。

## 7. 失败处理

CLI 退出码：

- `0`：生成成功，或验证通过；
- `2`：参数错误或固定 code 表示的校验/写入失败；
- 信号中断或解释器级故障不构成成功，不得使用不完整输出。

常见 fixed code 的处置方向：

| code | 检查方向 |
|---|---|
| `output_exists` | 使用新的 checkpoint 名；不要为省事直接覆盖历史证据 |
| `path_escape` / `unsafe_symlink` | 确认路径位于受控仓库和 bundle 目录，移除 symlink/路径替换 |
| `output_atomic_publish_unsupported` | 当前 OS/libc/文件系统没有所需原子 primitive；不要降级为普通 replace |
| `output_concurrent_change` | staging、目标或 recovery 在提交窗口被其他 writer 改变；停止并按 inode 审核所有目录项 |
| `output_commit_uncertain` | 原子调用已开始或提交后复核失败；目标可能已经更新，禁止盲目重试或清理隐藏文件 |
| `credential_content_forbidden` | 在源文档中删除或正确脱敏凭据；不要把原值贴进日志或工单 |
| `source_changed_during_read` | 输入内容、metadata、目录 inventory 或可见 inode 绑定在本轮发生漂移；停止并排除并行 writer 后重试 |
| `source_close_failed` | 输入 fd 已尝试关闭但系统返回错误；不要假定成功，检查主机/文件系统状态 |
| `bundle_hash_drift` | 报告字节已变化；先审阅变化，再生成新的 checkpoint |
| `bundle_non_canonical` / `bundle_schema_invalid` | 文件被改写或 schema 不匹配；不要手工修 JSON，重新生成 |
| `image_hash_drift` / `image_dimension_drift` / `image_mime_drift` | 核对实际图片、manifest 摘要、尺寸、格式与脱敏状态 |
| `output_write_failed` | 提交前写入、权限或持久化失败；目标未被确认提交，隐藏 staging 仍可能保留 |

不要通过放宽代码中的上限、跳过摘要或删掉失败测试来“修复”证据。先判定是源内容、manifest、
图片、路径还是并发操作问题，再做最小且可审计的更改。

出现隐藏 `.report-sync-*` 时，先暂停同目录 writer，记录每个文件的路径、大小和 SHA-256，再判断
哪个是候选、旧目标或并发写入内容。`output_commit_uncertain` 下应先对目标执行 `--verify`；只有
完成取证和留存后才能人工移动或删除 recovery。本工具故意不提供自动 GC，因为 POSIX 没有可移植
的“仅当名称仍指向预期 inode 时删除”原子操作。recovery 只证明 exchange 后旧 inode 仍有一个
目录名称；它不能替旧文件先前未执行的 `fsync` 提供崩溃持久性。

## 8. 远端同步后的正确更新顺序

当且仅当另一个明确授权的流程真实完成 Notion/语雀写入，并从远端重新读取、规范化和比对后：

1. 保存远端页面标识、写入响应、回读摘要和核验时间到相应历史 manifest；
2. 保持“写入成功”和“回读内容一致”为两个独立事实；
3. 不把 Notion 的证据复用于语雀，反之亦然；
4. 重新运行本工具，确认新的 manifest 与本地字节绑定；
5. 生成新 checkpoint，验证并提交；
6. 在任何远端回读缺失、失败或内容漂移时继续保留 `local_pending` 或显式异常，禁止补写
   `remote_verified`。

本工具故意不实现远端凭据、API client、浏览器控制或消息发送，避免“生成库存”这一只读、
可复现步骤隐式获得外部副作用权限。
