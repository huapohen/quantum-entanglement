# 调研与证据索引

本目录是本地报告的单一真相源。报告正文会在研究、实现和验证完成后汇总为
`multi_agent_collaboration_report.md`。当前截至 `3a92f3c` 的稳定内容已经同步到私人 Notion 并完成
回读；之后的本地增量持续以 Git/GitHub 为事实源。当前评审分支最新集成提交为 `4783f61`，已推送
到 `origin/mainline_continue_quantum_entanglement`。当前代码已推进到 authenticated tenant context
HTTP seam（`f226ab5`，实现提交 `8c4fb3f`）；用户在 2026-08-28 进一步确认 Notion 会影响
开发速度，因此后续开发期间只更新本地文档并频繁 commit/push；当前计划任务全部完成后再一次性批量同步 Notion
并逐页回读。语雀按用户
最新指令保持不操作。

## Notion 镜像（最近已回读基线：3a92f3c；本地/GitHub 代码已推进至 `4783f61`）

- 私有 GitHub 仓库：<https://github.com/huapohen/quantum-entanglement>
- 项目主页：<https://app.notion.com/p/3c1ead4b996e81e289c7dde1d597f630?pvs=204>
- 综合报告：<https://app.notion.com/p/3c1ead4b996e819897daff4941dcbd44?pvs=204>
- 截图证据库：<https://app.notion.com/p/3c1ead4b996e8101997fecd0302714ba?pvs=204>
- 竞品信源核验：<https://app.notion.com/p/3c1ead4b996e81f9b5eddebebc96d30a?pvs=204>
- 当前实现证据：<https://app.notion.com/p/3c1ead4b996e81669cefcf330b894853?pvs=204>
- 原生 IM 接入决策：<https://app.notion.com/p/3c9ead4b996e81638c43e48ecc2e0bcc?pvs=204>
- 原生 IM V1 合同：<https://app.notion.com/p/3c9ead4b996e8114985cce2cc5af2b63?pvs=204>
- 提前接入检查点：<https://app.notion.com/p/3c9ead4b996e8103b53bf10409f08e1d?pvs=204>
- 提前接入执行计划：<https://app.notion.com/p/3c9ead4b996e818fb220c66126181863?pvs=204>
- 基础镜像仍按 33 页计；独立评审空间现为 1 个父页与 17 个唯一子页，共 18 页；两处合计
  51 个私人 Notion 页面。项目主页原有 32 个子页块和 1 个任务数据库在本批定点更新前后保持不变。
- 本批新增 1 个完整语义镜像：
  [E3 M1 Stored-Event Envelope Codec](https://app.notion.com/p/3caead4b996e81f99b71d0bed6ac3136?pvs=204)；
  刷新全局执行计划、就绪度、索引、独立与全局分支导航，并定点更新独立父页与项目主页。
  `research/28`、ADR、计划、就绪度、索引、分支目录与 Changelog 均上传了对应完整 Markdown 原件。
- 当前语义镜像最终稳定基线为评审分支 `mainline_continue_quantum_entanglement@3a92f3c`；
  M1 代码封板候选为
  `d889751e4cc3b7db548994a000a87e21688b4429`，固定 tree 为
  `57b608ed57f47a68d1f9433104cd88d820a19929`。M2 已在本地/GitHub 的 `dd0ba54` 完成，M3 private
  store adapter 已在 `504824c` 完成，M4 inactive schema / Artifact owner transaction / private
  backup topology 的代码封板为 `aef5f8b`、tree 为 `f72cd558…876327`；M2–M4 及后续增量尚未按新节奏
  批量同步 Notion。当前本地分支已另行完成 opt-in migration 7、atomic result writer/readback、
  process-bound `AcceptedV2`、heartbeat acceptance seam 与 ACK-loss/replay 证据；生产 worker、
  真实 IM 和 outbound 均未启用。
- 本批 8 个相关页面已逐页 fetch 回读；34 个内容 marker 与 3 个父子/数据库结构检查全部命中，
  0 个缺失、0 个回读失败。独立父页为 17 个唯一子页；项目主页保持 32 个子页块和 1 个数据库。
- 机器可读页面映射、文件摘要和回读断言见
  [`notion_sync_manifest.json`](notion_sync_manifest.json)。
- 当前批次已完成 Notion 写入和远端回读；语雀仍未操作，飞书/企微仍为零发送，真实 IM 网络和
  outbound 仍未启用。
- E1–E3 独立审阅空间：<https://app.notion.com/p/3c9ead4b996e8108aea6c97c694d6587?pvs=204>；
  E2 adapter/lifecycle 历史证据：<https://app.notion.com/p/3caead4b996e8165b1dfd85a6d16e6d5?pvs=204>；
  E2 provider-bundle 历史证据：<https://app.notion.com/p/3caead4b996e81dc9001dcd77cdf9893?pvs=204>；
  合同复核证据：<https://app.notion.com/p/3caead4b996e817aab3ae63a29ecfb5c?pvs=204>；
  当前 M1 codec 证据：<https://app.notion.com/p/3caead4b996e81f99b71d0bed6ac3136?pvs=204>。
- `research/28_stored_event_envelope_codec_evidence.md`、ADR、下一阶段计划、readiness、索引、分支目录与
  Changelog 已完成 Notion 同步和远端回读；真实 sandbox、Agent 驱动、tool/browser/subprocess 与
  outbound 仍保持关闭。

## 当前阶段交付

- [`10H_EXECUTION_STATUS_2026-08-30.md`](10H_EXECUTION_STATUS_2026-08-30.md)：当前 10 小时全量目标的真实完成矩阵、远端提交、跨端验收入口和生产剩余主线。
- [`html/10h_execution_status_20260830.html`](html/10h_execution_status_20260830.html)：同一状态的可视化 HTML 入口。

| 文件 | 状态 | 内容 |
|---|---|---|
| `STAGE_ACCEPTANCE_2026-08-27.md` | 等待用户验收 | Worktree/远端分支收口、Result Observation 安全边界、验证命令、产品验收清单 |
| `NEXT_STAGE_PLAN.md` | M1–M7 与 M7.5 projection 候选已完成，下一步认证作用域/recovery | 新参考项目复评入口、stored-event codec、reserved fence、atomic writer、Observed/Accepted、迁移、projection 与 worker 门禁的提交级计划 |
| `../docs/production/TESTING_STRATEGY.md` | 当前执行规则 | 小改动跑专项，阶段封板才跑全量；当前库存 2,969 项，最近集成封板 2,964 项通过 |
| `NATIVE_IM_INTEGRATION_PREREQUISITES.md` | 独立 IM 后端合同复核已完成 | 原生 IM P0–P3 高保证路线、验收清单、NO-GO 条件及接入后 TODO 分界；下一门禁仍是真实 provider contract/scope/exchange，真实网络关闭 |
| `PRE_NATIVE_IM_EARLY_INTEGRATION_CHECKPOINT_2026-08-27.md` | 基线与三层备份已完成 | `1d399e5` 状态、backup 分支、annotated tag、离线 bundle、恢复命令与提前接入边界 |
| `NATIVE_IM_EARLY_INTEGRATION_PLAN.md` | Level B 等待真实合同输入 | E0–E5、Level A–D、已交付矩阵、独立 IM 源码复核结论、下一真实 provider contract/scope/exchange、可停点与 outbound 授权边界 |
| `../docs/production/NATIVE_IM_P0_CONTRACT_EXECUTABLE.md` | E1 生产说明已完成 | 四方法 port、zero-network fake、permit/ledger/ACK-loss、验证、回退和 E2 硬停止边界 |

当前接入前代码基线已经安全备份，Level A 合同可执行已在 `7620200` 完成并通过 Notion 回读。
Level B 的离线 profile/config/verifier/migration/nonce/read-preparation 与整页单事务 admission 已在
运行源码 `9cf1bfe` 完成；adapter/lifecycle 节点在 `2bdaea1` 完成；provider mapper/transport/bundle
TCK、稳定 event-source 与 transient exchange evidence 分离、增强 provenance 和 v6 durable readback
又在 `ee0666f` 完成。下一硬门禁是真实 IM 后端合同、测试 scope/read-only secret reference 与
production exchange；完成前不连接真实 sandbox。对独立 IM 后端已提交 `c623aea` 的源码复核确认：
当前公开 composition 只有 loopback liveness/ping，auth/IM 固定 fake，尚无 authenticated event read、
cursor/snapshot 或 provider readiness；封板前又对已推送 `a18acd6` 做了同口径漂移复核，新增提交
仍未注册这些合同。完整证据和最小交接合同见
`research/27_native_im_backend_contract_audit.md`。
`NEXT_STAGE_PLAN.md` 继续作为 E3 Atomic Result Authority 的最大强度参考，不再作为提前接入前的
串行总清单。其 M1 private stored-event envelope codec 已在 `d889751` 完成：372-byte Golden 在
Python 3.9.6/3.12.12/3.13.9 同 digest，专项 102 tests、Python 3.13 全仓 2,489 tests 与 locked
Ruff/Mypy 全绿；最终 formatter 封板提交为 `39732c1`，受保护的 provider-bundle suite digest 随后
在 `f8cafd4` 刷新为 `9e76f826…1a21ae0`，证据文档推进到 `0e85f80` 后再次完成 2,489 项全量复验。
M2 reserved fence 已在 `dd0ba54` 完成：五个 generic surface 在 `BEGIN` 前拒绝 reserved result
vocabulary，standalone `complete()` 在 clock/DML 前结构拒绝 scoped job；独立复核发现的 stripped
marker 与 type/key drift 旁路均已形成回归并修复。M3 private store adapter 又在 `504824c` 完成：
exact typed snapshot 与 fixed raw `sqlite3.Row` 在 owning transaction 内独立重算并比较 fields、bytes、
digest；trigger replacement/extra-row、storage class、caller mutation 与 exception-graph 旁路全部形成
回归。三版本 209 项组合专项、Python 3.13 全仓 2,578 项及 Ruff/Mypy 全绿。M4 又在代码节点
`aef5f8b` 完成 inactive migration 7 候选、私有 45-object backup topology、Artifact owner handle、
同事务 DML/readback、rollback-only、crash rollback、clean ambiguity classification 与 bounded
streaming history verification；M4 组合 82 项、全仓 2,639 项、Ruff/Mypy/diff-check 全绿，最终独立
reviewer 为 0 blocker。随后本地分支完成 opt-in migration 7、atomic result acceptance、Observed/
Accepted、receipt-bound reconciliation、heartbeat acceptance seam 与 process-bound result-only
business projection 候选；下一步是认证作用域、crash/kill/two-process recovery 和 compatibility/rollback。生产 worker、
真实 IM/outbound 均保持关闭。
完整边界见
`research/29_reserved_result_event_boundary_evidence.md`、
`research/30_stored_event_envelope_store_adapter_evidence.md` 与
`research/31_inactive_result_schema_artifact_transaction_evidence.md`。

## 专题研究

| 文件 | 状态 | 内容 |
|---|---|---|
| `research/00_scope_evidence_and_findings.md` | 已完成首版 | 研究边界、证据分级、群聊发现、核心结论 |
| `research/01_v0_implementation_audit.md` | 已完成首版，持续补充 | v0版 `agent_atore_demo` 源码与生产化差距 |
| `research/02_framework_deepdive.md` | 已完成首版 | DeepSeek Harness、LangGraph、LangChain、Deep Agents |
| `research/03_protocol_landscape.md` | 已完成首版 | A2A、ACP、MCP、ANP、AGNTCY 与内部协议边界 |
| `research/04_competitor_landscape.md` | 已完成首版 | 14 个竞品的编排、协作、通信、上下文与开放性 |
| `research/05_target_product_and_architecture.md` | 已完成首版 | 产品定义、分层架构、状态/上下文/治理与路线图 |
| `research/06_competitor_source_validation.md` | 已完成首版 | 14 个竞品、65 条来源记录、许可证与宣传/实现差异核验 |
| `research/07_current_implementation_status.md` | 历史快照：`e4cbf04` | 固定到旧 clean commit/tree 的 531-test 实现增量，不代表当前主线 |
| `research/08_commit_invariant_test_ledger.md` | 历史快照：`e141912` | commit→模块→不变量→测试证据→残余边界台账；旧基线本地实跑 625 tests |
| `research/09_e141912_release_evidence.md` | 历史快照：`e141912` | 旧 clean-source gates、canonical evidence、双构建、manifest、SBOM/schema 与制品 digest |
| `research/10_authenticated_invocation_transaction_evidence.md` | 当前安全事务证据：`4538159` | lost-ACK 精确 readback、完整 traceback provenance、nonce-bound 重签发、exact-bool state、fork guard、三版本 884-test 门禁与残余风险 |
| `research/11_invocation_receipt_dependency_audit.md` | 设计检查点：`1427dea` | backup v2、native/sparse migration、versioned start/result、trusted receipt 与原子 UoW 的依赖、威胁和 26 提交序列 |
| `research/12_process_inheritance_dependency_audit.md` | 设计检查点：`4944a3e` | fork/process epoch、SQLite connection、授权/密钥/secret、event-loop 继承边界与 20 提交修复序列 |
| `research/13_206acc1_transaction_release_evidence.md` | 发布证据快照：`206acc1` | canonical-parent 安全事务候选的 5/5 source-bound evidence、三版本 884-test 扩展门禁、JSON digest 与集成边界 |
| `research/14_ca02903_attempt_replay_release_evidence.md` | 组合证据快照：`9c24274` | process-identity canonical 上 24 笔安全重放、逐提交专项、三版本 901-test 门禁、预期 fork warning 精确断言与新 evidence digest |
| `research/15_event_store_process_boundary_audit.md` | 接入前设计审计 | `SQLiteEventStore` 的 26 个公开/生命周期入口、exact SQL snapshot、资源创建进程专属 cleanup、clean-error trampoline、iterator fork 缝隙、fresh-connection 测试与 13 提交接入序列；尚未改变 store 行为 |
| `research/16_event_store_process_binding_implementation.md` | 独立复核通过的单组件候选 | `SQLiteEventStore` 全入口 process binding、exact SQL snapshot、完整 inherited graph quarantine、transaction/migration/constructor/context owner cleanup、nested clean error、child GC/finalizer 与 fork/spawn/forkserver fresh CAS；独立 P0–P3 均为 0，三版本 928-test checkpoint，Gate A–E 保持关闭 |
| `research/17_current_stage_integration_evidence.md` | 当前阶段组合证据 | Backup-v2、Event-store、Authorization 与本地产品试用的绿色历史集成；三版本 1106-test full gate、四组 focused gate、静态/依赖/截图/浏览器证据、启动入口与 Gate A–E NO-GO 边界 |
| `research/18_model_backed_custom_instruction_evidence.md` | 当前模型产品证据：`886aedc` | 任意自定义指令、GPT `gpt-5.6-sol`、三 Agent DAG、三段 narration、三个 Markdown Artifact、25-event 浏览器验收、凭据/HTTP/Harness 边界与下一 crash-safe 里程碑 |
| `research/19_six_agent_collaboration_protocols_and_bottom_layer_design.md` | 当前协议选型：2026-08-26 | A2A 1.0（`v1.0.1` release）、stateless MCP 2026-07-28、BeeAI ACP、ANP、AGNTCY、FIPA ACL/Contract Net 六项边界，WanWork canonical envelope 设计、现状差距与落地顺序 |
| `research/20_clawith_competitive_analysis.md` | Clawith 固定源码深研：2026-08-26 | 基于官网、官方文档和 `dataelement/Clawith@45fc701c` 的产品、群聊、长期身份、Aware/Pulse、Experience Library、Skills/MCP、治理与部署审计；明确可借鉴项、不可照搬项和 WanWork 优先级 |
| `research/21_atomic_invocation_start_release_evidence.md` | 当前 atomic start 发布证据：`a1fd355` | first-claim/start 的代码、提交、Python 3.9/3.12/3.13、BEGIN/COMMIT/ROLLBACK ACK-loss、双连接、spawn/fork、backup/token canary 与未关闭 worker/result/action/Gate A–E 边界 |
| `research/22_native_im_e1_contract_executable_evidence.md` | 原生 IM E1 证据：`7620200` | 21 个 V1 wire model、23 vectors、四方法 port、默认拒绝 fake、receiver 双账本、ACK-loss/query、271 项专项矩阵、三版本/full gate 与 E2 未开始边界 |
| `research/23_native_im_e2_offline_inbox_foundation_evidence.md` | 原生 IM E2 离线底座：`4ab745b` | profile/config/verifier、migration 5 六表、durable nonce、read preparation、46 项专项/2,031 项全仓门禁，以及 page admission 单事务 P0 |
| `research/24_native_im_e2_atomic_page_admission_evidence.md` | 原生 IM E2 原子页运行证据：`9cf1bfe` | nonce/page/events/read/checkpoint 单事务、独立 durable readback、88 项专项/2,060 项全仓门禁、下一 adapter/lifecycle P0 |
| `research/25_native_im_e2_adapter_lifecycle_offline_evidence.md` | 原生 IM E2 adapter/lifecycle 离线运行证据：`2bdaea1` | default-off、bounded parser、process-bound lifecycle/kill switch、typed observability、全链 canary、recorded probe、2,114 项全仓门禁与真实接入前硬边界 |
| `research/26_native_im_provider_bundle_offline_evidence.md` | 原生 IM E2 provider bundle 离线闭环：`ee0666f` | Mapper/Transport/Bundle TCK、read-exchange evidence、增强 provenance、migration-v6 持久回读、2,386 项全仓门禁与真实 provider 接入前五项硬输入 |
| `research/27_native_im_backend_contract_audit.md` | 独立原生 IM 后端源码合同复核：`c623aea` | 确认 authority persistence 底座可复用，但运行 composition 仍只有 fake liveness/ping；冻结 Level B readiness/read 最小合同、三类直接阻断与第一轮只读联调顺序 |
| `research/28_stored_event_envelope_codec_evidence.md` | E3 Result Authority M1 代码证据：`d889751` | 私有 canonical envelope、exact raw SQLite row 重算、372-byte Golden、102 项专项、三 Python verifier、2,489 项全仓门禁、对抗修复及 M2/M3/authority 未完成边界 |
| `research/29_reserved_result_event_boundary_evidence.md` | E3 Result Authority M2 代码证据：`dd0ba54` | generic `BEGIN` 前 reserved vocabulary fence、scoped standalone completion durable classifier、25 项三 Python 专项、2,514 项全仓门禁与两条逆向旁路修复 |
| `research/30_stored_event_envelope_store_adapter_evidence.md` | E3 Result Authority M3 代码证据：`504824c` | private typed write-snapshot/raw-row 双验、fresh isolated insert、trigger/exception/API 对抗收口、209 项三 Python 组合专项与 2,578 项全仓门禁 |
| `research/31_inactive_result_schema_artifact_transaction_evidence.md` | E3 Result Authority M4 代码证据：`aef5f8b` | inactive migration 7、private backup topology、Artifact owner transaction、crash/ambiguity/bounded-history 证据、82 项组合专项、2,639 项全仓门禁与最终 0-blocker 复核 |
| `research/32_result_acceptance_worker_seam_evidence.md` | E3 Result Authority M7/worker seam：`7bed2b6` | opt-in store-owned acceptance、fresh-ACK `AcceptedV2`、replay/ACK-loss `ObservedV2`、接受期间 heartbeat fencing 与 exact request 门禁；生产 worker/projection 仍关闭 |
| `research/43_scoped_lease_lifecycle_evidence.md` | E3 continuation：`36cd0b4`/`025b5c7` | 私有 lifecycle admission stop、bounded drain、store relinquish、双连接 heartbeat/expiry 与 relinquish race；生产 gate 仍关闭 |
| `research/44_mainline_full_regression_20260830.md` | 当前封板全量回归 | 2,962 项 pytest、Ruff、strict mypy、compileall、diff-check 全通过及剩余生产边界 |
| `research/50_mainline_web_im_integration_regression_20260830.md` | 当前 Web-first IM 集成封板 | 2,964 项 pytest、Go API、Web 构建、synthetic 验收和影响面回归纪律 |
| `research/51_postgres_runtime_fail_closed_auth_composition_20260830.md` | PostgreSQL runtime 组合收口 | 空 fixture fake verifier、启动/readiness 生命周期、业务请求默认拒绝及 Go 专项证据 |
| `research/52_artifact_store_process_binding_20260830.md` | Artifact Store 进程绑定 | fork inherited SQLite 连接/锁在 public 入口前 fail-closed，28 项专项与静态门禁 |
| `research/53_revocation_guard_process_binding_20260830.md` | Revocation Guard 进程绑定 | fork inherited high-water guard 在 public 入口前 fail-closed，39 项专项与静态门禁 |
| `research/54_projection_offset_process_binding_20260830.md` | Projection Offset Store 进程绑定 | fork inherited projection lock/SQLite 在 public 入口前 fail-closed，72 项专项与静态门禁 |
| `research/55_invocation_recovery_process_binding_20260830.md` | Invocation Recovery Coordinator 进程绑定 | fork inherited recovery lock/store 在 public 入口前 fail-closed，31 项专项与静态门禁 |
| `research/56_authenticated_tenant_context_http_seam_20260830.md` | authenticated tenant context HTTP seam | Bearer → tenant candidate → 同一 UoW identity snapshot → active membership/Actor 的只读闭环；Go test/vet 全通过，真实 Clerk/业务持久化仍关闭 |
| `research/57_regression_gate_scope_fix_20260830.md` | 回归门禁影响面选择器修复 | 模块 README 不再误触发 Go/Web 门禁；7 项 selector 专项通过，未知 Python runtime 仍 fail-closed 升级全量 |
| `../docs/production/RESULT_BUSINESS_PROJECTION.md` | E3 M7.5 result-only business projection 候选 | leased projector、scope/terminal binding、幂等、schema pinning、identity/终态绑定漂移拒绝、fork process binding、dual-connection fencing、SIGKILL recovery 与 fail-closed 安全边界 |

## 已归档截图

完整 SHA-256、尺寸、来源、证据等级和隐私边界见
[`screenshots/README.md`](screenshots/README.md) 与
[`screenshots/manifest.json`](screenshots/manifest.json)。当前共归档 43 张图；前十张是受限、
未脱敏原件，只能进入本项目私有仓库和用户私有知识库，不得公开分发；第 10–13 张是合成本地
产品 UI，第 14 张是真实模型测试输出；第 15–26 张是 Clawith 公开官网、白皮书与官方文档
只读证据；Topic 33～37 的十项 SVG/PNG 是五个 W2 PostgreSQL 检查点/合同图，只作为报告导航图，
不冒充独立运行证据；四张本地 IM 基础/编辑撤回图是零网络本地 IM 运行证据。整套资料
仍按项目内部证据管理。

| 文件 | 内容 | 采集方式 |
|---|---|---|
| `screenshots/00_request_feishu.png` | 用户提供的任务原始截图 | 原始附件副本 |
| `screenshots/01_feishu_current_context.jpeg` | “10亿美金俱乐部”当前任务上下文 | 飞书只读 |
| `screenshots/02_feishu_history_aug16_19.jpeg` | WanWork 与“先业务、后平台”历史上下文 | 飞书只读 |
| `screenshots/03_feishu_dph_direction_aug15.jpeg` | DeepSeek Harness / 一切皆插件方向 | 飞书只读 |
| `screenshots/04_yuque_multi_agent_overview.jpeg` | 语雀多 Agent 产品调研概览 | 语雀只读 |
| `screenshots/05_yuque_products_rows_3_8.jpeg` | YouMind 至 NEAR AI Agent Market | 语雀只读 |
| `screenshots/06_yuque_products_rows_7_11.jpeg` | Todos 至 OpenWorker | 语雀只读 |
| `screenshots/07_yuque_products_rows_12_16.jpeg` | Pi Agent 至 OpenAgents | 语雀只读 |
| `screenshots/08_yuque_im_provider_comparison.jpeg` | IM 厂商能力对比 | 语雀只读 |
| `screenshots/09_yuque_technical_options.jpeg` | 技术组合对比 | 语雀只读 |
| `screenshots/10_local_trial_desktop_idle.png` | 本地产品桌面初始态 | Playwright loopback 运行证据 |
| `screenshots/11_local_trial_desktop_complete.png` | 3 Artifact / 25 event 桌面完成态 | Playwright loopback 运行证据 |
| `screenshots/12_local_trial_mobile_complete.png` | 390×844 移动端完成态 | Playwright loopback 运行证据 |
| `screenshots/13_local_trial_architecture_diagrams.png` | 产品架构、执行时序、平台状态图 | Playwright loopback 运行证据 |
| `screenshots/14_model_backed_custom_instruction_gpt.png` | GPT 自定义指令三 Agent 全页完成态 | Playwright loopback 真实模型运行证据 |
| `screenshots/15_clawith_homepage_positioning.png` | Clawith 的 AI 组织产品定位 | Playwright 官网只读证据 |
| `screenshots/16_clawith_collaboration_network.png` | 专家、超级个体与 Agent 协作网络 | Playwright 官网只读证据 |
| `screenshots/17_clawith_organization_evolution.png` | 个人 Agent 到组织级协作网络演变路径 | Playwright 官网只读证据 |
| `screenshots/18_clawith_six_capabilities.png` | 载体、记忆、协调、执行、治理与学习六类能力 | Playwright 官网只读证据 |
| `screenshots/19_clawith_docs_introduction.png` | 持久身份、记忆、协作与关键概念 | Playwright 官方文档只读证据 |
| `screenshots/20_clawith_pricing_20260827.png` | Free–Scale 月付、credits、public Agent seats 与加购包 | Playwright 价格页元素只读证据 |
| `screenshots/21_clawith_whitepaper_governance_20260827.png` | L1–L4 四级自治权限模型白皮书表述 | Playwright 白皮书 viewport 只读证据 |
| `screenshots/22_clawith_whitepaper_audit_claim_20260827.png` | 全链路审计、追溯、回放与证据声明 | Playwright 白皮书元素只读证据 |
| `screenshots/23_clawith_aware_focus_triggers_20260827.png` | Focus、Trigger、绑定与自适应调度文档 | Playwright 官方文档元素只读证据 |
| `screenshots/24_clawith_pulse_trigger_engine_20260827.png` | Pulse Trigger Engine、类型与生命周期文档 | Playwright 官方文档元素只读证据 |
| `screenshots/25_clawith_plaza_legacy_docs_20260827.png` | 与固定源码 Experience Library 已漂移的 Plaza 旧文档 | Playwright 官方文档元素只读证据 |
| `screenshots/26_clawith_rapid_rnd_claim_20260827.png` | 部门级研发交付样板、阶段指标与 `3 天` / `6d 21h` 同卡片口径冲突 | Playwright 官网元素只读证据 |
| `screenshots/33_postgres_authority_persistence_map.svg` / `.png` | 一级调研 → 当前持久化切片 → PostgreSQL 18.6 证据 → 六项 P0 gate | 仓库内 SVG 与派生 PNG；不是独立运行证据 |
| `screenshots/34_postgres_function_only_writes_and_exact_access_map.svg` / `.png` | 一级调研 → 五函数写面 → exact access → PG18.6 故障证据 → 剩余生产 gate | 仓库内 SVG 与派生 PNG；不是独立运行证据 |
| `screenshots/35_postgres_attested_runtime_composition_map.svg` / `.png` | private config → ambient/default-file/raw-DSN/malformed-query hardening → physical/session attestation → readiness/UoW/API gate → Gate A/Trusted Participant/mention 剩余边界 | 仓库内 SVG 与派生 PNG；不是独立运行证据 |
| `screenshots/36_postgres_production_authority_topology.svg` / `.png` | Gate A0 plan/SecretRef/provision/migrate/runtime/TLS/receipt/No-Go 合同 | 仓库内 SVG 与派生 PNG；不是独立运行证据 |
| `screenshots/37_postgres_approval_policy_control_store_map.svg` / `.png` | 离线 root policy → 独立 control cluster → exact CAS/attestation/readback → mutation-time fence 剩余边界 | 仓库内 SVG 与派生 PNG；不是独立运行证据 |
| `screenshots/35_local_im_acceptance_desktop.png` | 任意自定义指令提交后，父群受限工作卡、独立 Agent 子群和回复 | Playwright loopback 桌面端真实渲染与交互证据 |
| `screenshots/36_local_im_acceptance_mobile.png` | 移动 viewport 下的同一自定义指令验收结果 | Playwright loopback 移动端真实渲染与交互证据 |
| `screenshots/38_local_im_basic_desktop.png` | 普通群创建、普通文本发送和 reload 后消息列表 | Playwright loopback 桌面端真实渲染与交互证据 |
| `screenshots/39_local_im_basic_mobile.png` | 普通 IM 基础会话在移动 viewport 的响应式状态 | Playwright loopback 移动端真实渲染与交互证据 |
| `screenshots/40_local_im_edit_recall_desktop.png` | 桌面端普通群消息编辑后撤回，保留生命周期状态 | Playwright loopback 桌面端真实渲染与交互证据 |
| `screenshots/41_local_im_edit_recall_mobile.png` | 移动端普通群消息编辑后撤回及响应式渲染 | Playwright loopback 移动端真实渲染与交互证据 |

编辑/撤回交互步骤和 API 复核清单：
`screenshots/local_im_edit_recall_acceptance_manifest.json`（`local_pending`）。

## 数据安全

- 飞书与企微始终只读，没有发送、回复、评论、@ 或上传。
- Clawith 官网、官方文档和公开源码仅做只读调研，没有注册、登录、创建 Agent 或触发外部动作。
- 与本课题无关的凭据不会进入报告、截图索引、代码、Git 历史或 Notion。
- `references/` 是本地研究副本，不纳入本仓库提交。
