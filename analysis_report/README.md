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
- 认证事务证据：<https://app.notion.com/p/3c2ead4b996e8105b0cad304ef28dd38?pvs=204>
- 最近增量同步：2026-08-20；认证事务证据页、当前实现页的事务增量和项目导航均已
  fetch 回读；`206acc1`、`8d82dd7`、5/5 gates 与 evidence digest 断言全部存在。综合报告、
  竞品页和 10 张受限截图延续上一次已验证状态。
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

## 已归档截图

完整 SHA-256、尺寸、来源、证据等级和隐私边界见
[`screenshots/README.md`](screenshots/README.md) 与
[`screenshots/manifest.json`](screenshots/manifest.json)。十张图均为受限、未脱敏原件，只能
进入本项目私有仓库和用户私有 Notion，不得公开分发。

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

## 数据安全

- 飞书与企微始终只读，没有发送、回复、评论、@ 或上传。
- 与本课题无关的凭据不会进入报告、截图索引、代码、Git 历史或 Notion。
- `references/` 是本地研究副本，不纳入本仓库提交。
