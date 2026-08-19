# 调研与证据索引

本目录是本地报告的单一真相源。报告正文会在研究、实现和验证完成后汇总为
`multi_agent_collaboration_report.md`，并使用相同章节结构同步到 Notion。

## 专题研究

| 文件 | 状态 | 内容 |
|---|---|---|
| `research/00_scope_evidence_and_findings.md` | 已完成首版 | 研究边界、证据分级、群聊发现、核心结论 |
| `research/01_jingran_implementation_audit.md` | 持续补充 | 静然 `agent_atore_demo` 源码与生产化差距 |
| `research/02_framework_deepdive.md` | 调研中 | DeepSeek Harness、LangGraph、LangChain、Deep Agents |
| `research/03_protocol_landscape.md` | 调研中 | A2A、ACP、MCP、ANP、AGNTCY 与内部协议边界 |
| `research/04_competitor_landscape.md` | 已完成首版 | 14 个竞品的编排、协作、通信、上下文与开放性 |
| `research/05_target_product_and_architecture.md` | 已完成首版 | 产品定义、分层架构、状态/上下文/治理与路线图 |
| `research/06_competitor_source_validation.md` | 调研中 | 竞品官方来源、许可证与宣传/实现差异核验 |

## 已归档截图

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
