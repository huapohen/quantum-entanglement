# 调研与证据索引

本目录是本地报告的单一真相源。报告正文会在研究、实现和验证完成后汇总为
`multi_agent_collaboration_report.md`，并使用相同章节结构同步到 Notion。

## Notion 镜像

- 私有 GitHub 仓库：<https://github.com/huapohen/quantum-entanglement>
- 项目主页：<https://app.notion.com/p/3c1ead4b996e81e289c7dde1d597f630?pvs=204>
- 综合报告：<https://app.notion.com/p/3c1ead4b996e819897daff4941dcbd44?pvs=204>
- 截图证据库：<https://app.notion.com/p/3c1ead4b996e8101997fecd0302714ba?pvs=204>
- 竞品信源核验：<https://app.notion.com/p/3c1ead4b996e81f9b5eddebebc96d30a?pvs=204>
- 当前实现证据：<https://app.notion.com/p/3c1ead4b996e81669cefcf330b894853?pvs=204>
- 最近完整同步：2026-08-20；综合报告、竞品全景、当前实现证据、项目导航和
  10 张受限截图均已回读验证。其余 6 个专题页自 2026-08-19 已验证同步后未发生本地变化。
- 机器可读页面映射、文件摘要和回读断言见
  [`notion_sync_manifest.json`](notion_sync_manifest.json)。

## 专题研究

| 文件 | 状态 | 内容 |
|---|---|---|
| `research/00_scope_evidence_and_findings.md` | 已完成首版 | 研究边界、证据分级、群聊发现、核心结论 |
| `research/01_jingran_implementation_audit.md` | 已完成首版，持续补充 | 静然 `agent_atore_demo` 源码与生产化差距 |
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

## 已归档截图

完整 SHA-256、尺寸、来源、证据等级和隐私边界见
[`screenshots/README.md`](screenshots/README.md) 与
[`screenshots/manifest.json`](screenshots/manifest.json)。当前共归档 26 张图；前十张是受限、
未脱敏原件，只能进入本项目私有仓库和用户私有知识库，不得公开分发；第 10–13 张是合成本地
产品 UI，第 14 张是真实模型测试输出；第 15–25 张是 Clawith 公开官网、白皮书与官方文档
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

## 数据安全

- 飞书与企微始终只读，没有发送、回复、评论、@ 或上传。
- Clawith 官网、官方文档和公开源码仅做只读调研，没有注册、登录、创建 Agent 或触发外部动作。
- 与本课题无关的凭据不会进入报告、截图索引、代码、Git 历史或 Notion。
- `references/` 是本地研究副本，不纳入本仓库提交。
