# 调研与证据索引

本目录是本地报告的单一真相源。报告正文会在研究、实现和验证完成后汇总为
`multi_agent_collaboration_report.md`。当前采用阶段批量同步节奏：本地连续研究/实现/验证并频繁提交，
阶段代码和文档全部完成、推送 GitHub 后，再统一同步私人 Notion 并完成一次回读。Notion 未回读前只算
`local_pending`，不得冒充远端已更新；语雀仍只在用户另行明确授权时操作。

## Notion 镜像（最近完成批次：2026-08-28）

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
- PostgreSQL authority 专题：<https://app.notion.com/p/3caead4b996e8183ae54f4d5abc2643d?pvs=204>
- 最近稳定批次同步：2026-08-28；47 个受控事实源页面与 2 个派生索引页共 49 页。项目首页、
  WanWork IM 审阅主页、调研矩阵、实施计划、2output 吸收审计、专题 32 与专题 33 已联合回读。
- 当前最后完成远端回读的 WanWork IM Notion 内容基线为
  `dev_wanwork_quantum_entanglement@7bb324a4a06689a496cbc99b79d23261d031bc19`，其 Topic 34 代码证据
  基线为 `cd92ea56493b43889f5165892b40ec36e958d44a`；分支未合并 `main`。Topic 35 `53dd38b` 以及
  authority specification/cluster probe/cutover plan v4/PreflightReport `d2f1bf0` 代码与文档仍是本地/Git
  `local_pending` 增量，不能写成
  Notion 已更新。`main@f99f176` 仍是提前
  接入历史备份基线，不代表本分支当前 W2 进度。
- 本轮已确认两个当前 Markdown 附件从临时上传转为页面附件；所有七个更新页面均回读到 Topic 33
  反链、两个基线 SHA、两层一级调研根、六项 P0 gate 与禁止性声明。没有记录临时 signed S3 URL。
- 冻结合同正文没有为同步台账而改写；其本地原始文件 SHA-256 为
  `99031ad243112122e987e84658ff93daf33b3285ea1468039f9d59dc8048167a`。
- 机器可读页面映射、文件摘要和回读断言见
  [`notion_sync_manifest.json`](notion_sync_manifest.json)。
- 最近完成批次已完成 Notion 写入和远端回读；Topic 35 与 Gate A0 plan 将在本地代码/测试/文档/Git
  全部收口后批量同步；语雀仍未操作。

## 当前阶段交付

| 文件 | 状态 | 内容 |
|---|---|---|
| `STAGE_ACCEPTANCE_2026-08-27.md` | 等待用户验收 | Worktree/远端分支收口、Result Observation 安全边界、验证命令、产品验收清单 |
| `NEXT_STAGE_PLAN.md` | 已冻结，尚未实施 | 新参考项目复评入口、stored-event codec、reserved fence、atomic writer、Observed/Accepted、迁移与 worker 门禁的提交级计划 |
| `NATIVE_IM_INTEGRATION_PREREQUISITES.md` | 原路线已由提前接入调度修订 | 原生 IM P0–P3 高保证路线、验收清单、NO-GO 条件及接入后 TODO 分界 |
| `PRE_NATIVE_IM_EARLY_INTEGRATION_CHECKPOINT_2026-08-27.md` | 历史备份检查点 | `1d399e5` 状态、backup 分支、annotated tag、离线 bundle、恢复命令与提前接入边界；不是当前 W2 入口 |
| `NATIVE_IM_EARLY_INTEGRATION_PLAN.md` | 历史调度计划 | 提前接入决策时冻结的 E0–E5/Level A–D 路线；保留作决策溯源，不再是当前 W2 执行入口 |
| `docs/wanwork_im/W2_POSTGRES_CUTOVER_PLAN_CHECKPOINT.md` | 当前 Gate A0 plan 入口：`d2f1bf0`，`local_pending` | physical cluster probe、managed/transient 双 authority specification、plan v4、代码派生五阶段 workflow、fixed-SQL short-lived PreflightReport、严格 decoder/approval/file trust、No-Go 与 policy/executor/receipt 后续顺序 |
| `docs/wanwork_im/W2_POSTGRES_RUNTIME_CHECKPOINT.md` | 当前 W2 工程入口：`53dd38b` | canonical strict connection policy、attested runtime pool、受控 UoW、startup/readiness/route barrier、独立 migrator、Go/No-Go 与 Gate A/Trusted tenant 剩余 P0 |
| `docs/wanwork_im/W2_POSTGRES_POLICY_CONTROL_STORE_CHECKPOINT.md` | 当前 policy control-store 工程入口：`16d66b6`，`local_pending` | 独立 control cluster、owner/reader/activator 分权、exact CAS、catalog/ACL attestation、commit-unknown reconcile、PG18.6 实证与 mutation-time fence 后续边界 |
| `research/36_postgres_approval_policy_control_store_checkpoint.md` | 当前 W2 深度证据：`16d66b6`，`local_pending` | immutable policy archive/activation/head、完整历史链、双重 attestation、并发/损坏/取消测试、诚实 NO-GO 与下一阶段顺序 |
| `research/37_postgres_durable_attempt_issuer_v2_checkpoint.md` | 当前 W2 本地增量：`68d4f2b`，`local_pending` | 五角色 control store、post-preflight durable attempt grant、issuance ID 幂等回读、fence `/3` trust boundary 与 SQL smoke test |
| `research/38_local_im_provider_agent_thread_checkpoint.md` | 当前本地验收增量：`local_pending` | provider-neutral IM/auth、Agent Store、`@Agent` 子群 vertical slice、零网络 Fiber API、任意自定义指令页面与 Playwright 桌面/移动端证据 |

接入前代码基线已经安全备份。`NATIVE_IM_EARLY_INTEGRATION_PLAN.md` 中“先 Level A、再 Level B
sandbox inbound-only”是当时的历史调度口径。当前执行源已切换为
`docs/wanwork_im/W2_POSTGRES_RUNTIME_CHECKPOINT.md` 与 Topic 35；`NEXT_STAGE_PLAN.md` 继续作为 E3
Atomic Result Authority 的最大强度参考，不是当前 W2 的串行总清单。

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
| `research/22_2output_research_absorption_audit.md` | 当前调研吸收审计：2026-08-28 | `2output` 的 40/40 Markdown、31/31 独立报告处置、AgentSpace evidence delta、RQ-039～RQ-042、Egress 架构修正及 M0/W1/W2+ 门禁 |
| `research/23_plugin_manifest_admission_implementation.md` | W1 P0-3 历史实现证据：`ed9a709` | host-computed manifest digest、PackageRecord exact admission、Effective v2 与 frozen activation；当前 Secret 增量由专题 24 接续 |
| `research/24_secret_claim_admission_implementation.md` | W1 P0-4 当前实现证据：`211ada7` | `2output` Secret/credential/plugin 证据到 claim admission、Effective v3、anti-replay/revocation/canary/golden 门禁及 action-time JIT lease 未完成边界 |
| `research/25_plugin_registry_freeze_implementation.md` | W1 P1-1 当前实现证据：`e2f82be` | Registry builder→Freeze→runtime 合同、完整 definition graph 重验、不可变快照、late registration 拒绝与 concurrent race 证据 |
| `research/26_plugin_effect_scope_shutdown_implementation.md` | W1 P1-2 当前实现证据：`0f00b47` | effect scope `open→closing→closed`、Drain 前关闭注册、迟到/递归 cleanup 拒绝与失败项精确重试 |
| `research/27_plugin_host_callback_locking_implementation.md` | W1 P1-3 当前实现证据：`3b8e02e` | Host mutex 外 callback、starting/stopping single owner、State 可观察与 reentrant/concurrent lifecycle 快速拒绝 |
| `research/28_plugin_lifecycle_panic_containment_implementation.md` | W1 P1-4 当前实现证据：`2d97f0a` | panic 固定脱敏、startup rollback、shutdown 继续回收与 payload canary |
| `research/29_plugin_lifecycle_cooperative_deadline_contract.md` | W1 P1-5 当前实现证据：`eafd3da` | context deadline 不冒充 callback return/process kill/effect finality，owner/state 直到 callback 返回才收敛 |
| `research/30_third_party_execution_isolation_contract.md` | W1 P1-6 当前合同与 fake 证据：`43e111e`/`fccb64e`/`d32079c` | host-owned refs、Supervisor IPC、generation/fence、cancel→grace→kill→wait/reap/release receipt、operator quarantine 与 `isolation=none` deterministic fake |
| `research/31_volatile_memory_event_store_implementation.md` | W1 P1-7 当前实现证据：`a4ac9bd`…`4118746` | volatile/non-production EventStore fake、ordered exact retry/conflict、store-owned ordering/time、scope/namespace cursor、严格 admission、cooperative context、并发/失败原子性、test-only backfill fixture 与 W2 durability/projection 边界 |
| `research/32_im_identity_conversation_and_provider_metadata_contract.md` | W1 IM identity/conversation/metadata 合同证据：`9f55b33`…`60ebf6a` | `2output` 一级证据到 `ActorRef/Snapshot`、realm-scoped external identity、`ConversationRef/Snapshot`、Agent thread topology、零授权 canonical `ext_info`、848 个非 canonical 排列、forbidden-field canary、race/fuzz 与 W2～W4 未完成边界 |
| [`research/33_postgres_authority_persistence_checkpoint.md`](research/33_postgres_authority_persistence_checkpoint.md) | W2 PostgreSQL authority persistence 历史检查点：`8d662bf` / `4a465d8` | `0001..0004` persistence substrate 的前序证据；保留作溯源，不是当前 W2 入口 |
| [`research/34_postgres_function_only_writes_and_exact_access_checkpoint.md`](research/34_postgres_function_only_writes_and_exact_access_checkpoint.md) | W2 前序检查点：`cd92ea5` | `0005`、五个 fixed function、function-only repository/receipt、exact access 临时测试 validator fixture、真实 migration/runtime login 与 PostgreSQL 18.6 正负向矩阵；由 Topic 35 接续 |
| [`research/35_postgres_attested_runtime_composition_checkpoint.md`](research/35_postgres_attested_runtime_composition_checkpoint.md) | W2 当前深度证据：`53dd38b` | canonical strict connection policy、physical/session attestation、exact readiness、attested-only UoW、API gate、one-shot migrator、PG18 normal/race/vet 与 Gate A/Trusted Participant/mention 后续计划 |
| [`research/36_postgres_approval_policy_control_store_checkpoint.md`](research/36_postgres_approval_policy_control_store_checkpoint.md) | W2 当前深度证据：`16d66b6` | 独立 policy control cluster、三角色、exact CAS、完整历史链、code-owned catalog attestation、PG18.6 并发/故障证据与 mutation-time fence NO-GO |
| [`research/37_postgres_durable_attempt_issuer_v2_checkpoint.md`](research/37_postgres_durable_attempt_issuer_v2_checkpoint.md) | W2 当前本地增量：`68d4f2b`，`local_pending` | 五角色 control store、post-preflight durable attempt issuer、issuance retry、完整向量绑定、fence `/3`、PG18.6 fresh schema/contract smoke test；Notion 延后批量同步 |
| `docs/wanwork_im/W2_POSTGRES_POLICY_CONTROL_STORE_CHECKPOINT.md` | 当前 policy control-store 工程入口与 Go/No-Go：`16d66b6` | 代码/部署入口、验证命令、可信边界，以及 approval consumption/fence/receipt/executor 的后续顺序 |

## 已归档截图

完整 SHA-256、尺寸、来源、证据等级和隐私边界见
[`screenshots/README.md`](screenshots/README.md) 与
[`screenshots/manifest.json`](screenshots/manifest.json)。当前共归档 39 张图；前十张是受限、
未脱敏原件，只能进入本项目私有仓库和用户私有知识库，不得公开分发；第 10–13 张是合成本地
产品 UI，第 14 张是真实模型测试输出；第 15–26 张是 Clawith 公开官网、白皮书与官方文档
只读证据；最后十项是 Topic 33～37 五个 W2 PostgreSQL 检查点/合同图各自的 SVG source 与
PNG rendering，只作为报告导航图，不冒充独立运行证据。整套资料仍按项目内部证据管理。

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

## 数据安全

- 飞书与企微始终只读，没有发送、回复、评论、@ 或上传。
- Clawith 官网、官方文档和公开源码仅做只读调研，没有注册、登录、创建 Agent 或触发外部动作。
- 与本课题无关的凭据不会进入报告、截图索引、代码、Git 历史或 Notion。
- `references/` 是本地研究副本，不纳入本仓库提交。
