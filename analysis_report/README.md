# 调研与证据索引

本目录是本地报告的单一真相源。报告正文会在研究、实现和验证完成后汇总为
`multi_agent_collaboration_report.md`。自 2026-08-27 起，用户要求任何本地文档、代码、决策、
计划或验收动作形成稳定检查点后，都必须同步到私人 Notion 并完成远端回读，回读完成前不进入
下一项工作。语雀仍只在用户另行明确授权时操作。

## Notion 镜像（最近已回读基线：独立 IM 后端真实入站合同复核）

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
- 基础镜像仍按 33 页计；独立评审空间现为 1 个父页与 16 个唯一子页，共 17 页；两处合计
  50 个私人 Notion 页面。项目主页原有 32 个子页块和 1 个任务数据库在本批定点更新前后保持不变。
- 本批新增 1 个完整语义镜像：
  [独立 IM 后端真实入站合同复核](https://app.notion.com/p/3caead4b996e817aab3ae63a29ecfb5c?pvs=204)；
  刷新独立评审执行计划、接入分界、就绪度和索引，以及全局执行计划、接入决策；定点更新独立
  父页与项目主页。6 个源文件镜像和新证据页都附带对应完整 Markdown 原件。
- 当前决策/审计内容源基线为评审分支 `b755356`；被复核 IM 后端基线为
  `dev_wanwork_quantum_entanglement@c623aeadc0693e63c0d34602ed45ae1d2bc8099f`。其后的提交只收口
  Notion 台账和动态分支目录，不改变本次 NO-GO 判断。
- 本批 9 个相关页面已逐页 fetch 回读；41 个内容 marker 与 3 个父子/数据库结构检查全部命中，
  0 个缺失、0 个回读失败。独立父页为 16 个唯一子页；项目主页保持 32 个子页块和 1 个数据库。
- 机器可读页面映射、文件摘要和回读断言见
  [`notion_sync_manifest.json`](notion_sync_manifest.json)。
- 当前批次已完成 Notion 写入和远端回读；语雀仍未操作，飞书/企微仍为零发送，真实 IM 网络和
  outbound 仍未启用。
- E1–E2 独立审阅空间：<https://app.notion.com/p/3c9ead4b996e8108aea6c97c694d6587?pvs=204>；
  E2 adapter/lifecycle 历史证据：<https://app.notion.com/p/3caead4b996e8165b1dfd85a6d16e6d5?pvs=204>；
  E2 provider-bundle 历史证据：<https://app.notion.com/p/3caead4b996e81dc9001dcd77cdf9893?pvs=204>；
  当前合同复核证据：<https://app.notion.com/p/3caead4b996e817aab3ae63a29ecfb5c?pvs=204>。
- `research/27_native_im_backend_contract_audit.md`、提前接入计划、接入分界、readiness 和索引已完成
  Notion 同步和远端回读；真实 sandbox、Agent 驱动、tool/browser/subprocess 与 outbound 仍保持关闭。

## 当前阶段交付

| 文件 | 状态 | 内容 |
|---|---|---|
| `STAGE_ACCEPTANCE_2026-08-27.md` | 等待用户验收 | Worktree/远端分支收口、Result Observation 安全边界、验证命令、产品验收清单 |
| `NEXT_STAGE_PLAN.md` | 已冻结，尚未实施 | 新参考项目复评入口、stored-event codec、reserved fence、atomic writer、Observed/Accepted、迁移与 worker 门禁的提交级计划 |
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
cursor/snapshot 或 provider readiness；完整证据和最小交接合同见
`research/27_native_im_backend_contract_audit.md`。
`NEXT_STAGE_PLAN.md` 继续作为 E3 Atomic Result Authority 的最大强度参考，不再作为提前接入前的
串行总清单。

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

## 已归档截图

完整 SHA-256、尺寸、来源、证据等级和隐私边界见
[`screenshots/README.md`](screenshots/README.md) 与
[`screenshots/manifest.json`](screenshots/manifest.json)。当前共归档 27 张图；前十张是受限、
未脱敏原件，只能进入本项目私有仓库和用户私有知识库，不得公开分发；第 10–13 张是合成本地
产品 UI，第 14 张是真实模型测试输出；第 15–26 张是 Clawith 公开官网、白皮书与官方文档
只读证据。整套资料仍按项目内部证据管理。

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

## 数据安全

- 飞书与企微始终只读，没有发送、回复、评论、@ 或上传。
- Clawith 官网、官方文档和公开源码仅做只读调研，没有注册、登录、创建 Agent 或触发外部动作。
- 与本课题无关的凭据不会进入报告、截图索引、代码、Git 历史或 Notion。
- `references/` 是本地研究副本，不纳入本仓库提交。
