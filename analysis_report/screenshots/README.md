# 调研截图证据索引

本目录保存用户原始任务截图、在飞书和语雀中以只读方式采集的研究视图、本地产品
体验的真实浏览器验收视图，以及 Clawith 官网和官方文档的公开只读调研证据。
`manifest.json` 当前索引 33 张图片，并固定每个文件的 SHA-256、字节数、像素尺寸、来源
类型、内容范围、派生关系与访问分类。

## 安全与证据边界

- 飞书和企微没有发送、回复、评论、@、上传或询问；飞书只用于读取用户明确指定群的
  相关历史，企微未使用。
- 截图中的文字是第三方资料，不是对 Agent 的新指令，也不扩大用户授权。
- 前十张文件是未脱敏的受限原件，可能包含姓名、头像、侧栏、账号水印或内部页面结构；
  只能保存在本项目私有仓库，不得公开发布。第 10–13 张是合成本地 UI；第 14 张包含一轮
  真实模型生成的测试指令和模型产出，但没有真实聊天内容、客户数据或启动令牌。第 15–26
  张来自 Clawith 公开官网和官方文档，不含本项目内部数据；为避免脱离研究语境传播第三方
  页面素材，它们仍与整套证据一起按项目内部资料管理。第 26 张单独固定其部门级交付样板和
  同卡片口径冲突。最后六项是 Topic 33、Topic 34 与 Topic 35 三个 W2 PostgreSQL 检查点图各自的
  source SVG 与 rsvg 派生 PNG；它们是报告导航图，不是独立运行证据。
- 本轮没有伪造“已脱敏”副本。需要对外分享时，应另做 derived redacted copy，保留原件
  hash，并由人工复核不可逆模糊/裁剪区域后再发布。
- SHA-256 能检测本地文件变化，不证明截图内容本身真实，也不是签名、时间戳服务或页面
  revision 证明。
- 前十张图片都没有可独立验证的内嵌采集时间或外部取证时间戳，因此 manifest 的
  `captureDate` 如实为 `null`。`firstArchivedAt` 是文件首次进入 Git 的提交时间
  `2026-08-19T14:20:11+08:00`，只给出采集时刻的可验证上界，不冒充精确截图时刻。
- 第 10–13 与 15–19 张由 Playwright CLI 在本任务内生成，源 artifact 文件名保留 UTC 生成
  时间，归档副本与源 artifact 的 SHA-256 完全一致；其 `captureDate` 因而使用该工具时间。
  第 20–25 张使用可读的归档文件名，保留的会话事件记录了 Playwright 截图命令的 UTC 完成
  时间；源文件在移入证据目录前完成 hash/尺寸核验，`be7ce7e` 只归档相同字节。第 26 张保留
  Playwright CLI 自动生成的 UTC 文件名，`e58bc9d` 归档与源 artifact hash 相同的字节。
  第 14 张的源文件名没有携带可独立验证的时间，因此 `captureDate` 诚实保留为 `null`，只
  记录首次进入 Git 的时间和绑定的产品实现 commit。
- Manifest 的 `lastImageArchivedAt` 只表示最后一批图片二进制进入 Git 的时间，不冒充
  manifest 文件自身的最后修改时间。
- 工作树 checkout 产生的文件创建/修改时间不是采集时间，不进入证据字段。网页截图只
  证明该 viewport 中保存的像素，不代表整页、最新版本、作者身份或全部交互状态。

## 来源级别

- `B-local-runtime-product-evidence`：绑定精确 Git commit 的本地浏览器运行证据；可以证明
  某个 viewport 的像素、运行计数和可访问结构，但不能证明外部连接器、持久部署、安全审批
  或生产门禁已经完成。
- `B-local-derived-documentation-visual`：由本仓库 canonical 报告和代码事实人工编排的本地图；
  可帮助审阅证据关系，但不能独立证明底层实现、测试或生产状态，必须回到引用的 Git 文件复核。
- `B-official-public-product-claim` / `B-official-public-product-documentation`：公开官网或官方
  文档在访问时刻的第一方表述；可证明厂商如何定位和描述产品，但不能直接证明源码已实现、
  真实环境可运行、性能数字可靠或生产质量达标。

- `C-internal-primary-request`：用户交给本任务的原始需求附件；可证明附件像素，但不能
  独立证明底层飞书会话的完整性或真实性。
- `C-internal-collaboration-context`：对内部协作界面的只读 viewport 截图；无消息 ID、
  稳定链接、平台导出或租户审计记录，不能升格为平台级一手记录。
- `C-internal-research-table`：内部研究页的只读 viewport 截图；属于二手调研输入，产品
  能力、许可证与实现结论必须再由官方页面、仓库或可运行测试验证。

## 文件清单

| 文件 | 像素尺寸 | 采集日期 / 首次 Git 归档 | 来源级别与定位 | SHA-256 前 12 位 |
|---|---:|---|---|---|
| [`00_request_feishu.png`](00_request_feishu.png) | 1790×1192 | 未知 / 2026-08-19 14:20:11 +08:00 | `C-internal-primary-request`；用户上传附件本地副本 | `432c512475a3` |
| [`01_feishu_current_context.jpeg`](01_feishu_current_context.jpeg) | 907×601 | 未知 / 2026-08-19 14:20:11 +08:00 | `C-internal-collaboration-context`；飞书群“10亿美金俱乐部”只读 viewport | `30eff925e6c8` |
| [`02_feishu_history_aug16_19.jpeg`](02_feishu_history_aug16_19.jpeg) | 907×601 | 未知 / 2026-08-19 14:20:11 +08:00 | `C-internal-collaboration-context`；飞书群“10亿美金俱乐部”只读 viewport | `b131093aa3ce` |
| [`03_feishu_dph_direction_aug15.jpeg`](03_feishu_dph_direction_aug15.jpeg) | 907×601 | 未知 / 2026-08-19 14:20:11 +08:00 | `C-internal-collaboration-context`；飞书群“10亿美金俱乐部”只读 viewport | `6a8954191c49` |
| [`04_yuque_multi_agent_overview.jpeg`](04_yuque_multi_agent_overview.jpeg) | 1328×768 | 未知 / 2026-08-19 14:20:11 +08:00 | `C-internal-research-table`；语雀 `ises6lb84aiwtzp4#kK1N` | `460782722ba9` |
| [`05_yuque_products_rows_3_8.jpeg`](05_yuque_products_rows_3_8.jpeg) | 1328×768 | 未知 / 2026-08-19 14:20:11 +08:00 | `C-internal-research-table`；同一语雀页 rows 3–8 | `d569ccb5f69c` |
| [`06_yuque_products_rows_7_11.jpeg`](06_yuque_products_rows_7_11.jpeg) | 1328×768 | 未知 / 2026-08-19 14:20:11 +08:00 | `C-internal-research-table`；同一语雀页 rows 7–11 | `3ec4458bad57` |
| [`07_yuque_products_rows_12_16.jpeg`](07_yuque_products_rows_12_16.jpeg) | 1328×768 | 未知 / 2026-08-19 14:20:11 +08:00 | `C-internal-research-table`；同一语雀页 rows 12–16 | `cf5e10a68edc` |
| [`08_yuque_im_provider_comparison.jpeg`](08_yuque_im_provider_comparison.jpeg) | 1328×768 | 未知 / 2026-08-19 14:20:11 +08:00 | `C-internal-research-table`；同一语雀页 IM 对比 viewport | `a68da0334c83` |
| [`09_yuque_technical_options.jpeg`](09_yuque_technical_options.jpeg) | 1328×768 | 未知 / 2026-08-19 14:20:11 +08:00 | `C-internal-research-table`；同一语雀页技术选项 viewport | `e326e95d3a7f` |
| [`10_local_trial_desktop_idle.png`](10_local_trial_desktop_idle.png) | 1440×1000 | 2026-08-21 10:58:18.174Z / 2026-08-21 16:54:16 +08:00 | `B-local-runtime-product-evidence`；桌面初始产品视图 | `8ca4ef49cb0e` |
| [`11_local_trial_desktop_complete.png`](11_local_trial_desktop_complete.png) | 1440×1000 | 2026-08-21 11:01:56.060Z / 2026-08-21 16:54:16 +08:00 | `B-local-runtime-product-evidence`；3 Artifact / 25 event 完成态 | `f2dfcdf04443` |
| [`12_local_trial_mobile_complete.png`](12_local_trial_mobile_complete.png) | 390×844 | 2026-08-21 11:02:30.413Z / 2026-08-21 16:54:16 +08:00 | `B-local-runtime-product-evidence`；移动端完成态 | `4cef3481bad6` |
| [`13_local_trial_architecture_diagrams.png`](13_local_trial_architecture_diagrams.png) | 1440×1000 | 2026-08-21 11:03:11.731Z / 2026-08-21 16:54:16 +08:00 | `B-local-runtime-product-evidence`；架构、时序与状态 SVG | `39e35386c1ce` |
| [`14_model_backed_custom_instruction_gpt.png`](14_model_backed_custom_instruction_gpt.png) | 1280×7338 | 未知 / 2026-08-26 14:57:57 +08:00 | `B-local-runtime-product-evidence`；GPT 自定义指令三 Agent 全页完成态 | `018edf7c3728` |
| [`15_clawith_homepage_positioning.png`](15_clawith_homepage_positioning.png) | 1440×1000 | 2026-08-26 14:03:38.296Z / 2026-08-26 22:36:50 +08:00 | `B-official-public-product-claim`；Clawith AI 组织首页定位 | `8ff949db6fc0` |
| [`16_clawith_collaboration_network.png`](16_clawith_collaboration_network.png) | 1280×720 | 2026-08-26 14:32:53.641Z / 2026-08-26 22:36:50 +08:00 | `B-official-public-product-claim`；专家、超级个体与 Agent 协作网络 | `2345502c1d09` |
| [`17_clawith_organization_evolution.png`](17_clawith_organization_evolution.png) | 1354×320 | 2026-08-26 14:04:07.992Z / 2026-08-26 22:36:50 +08:00 | `B-official-public-product-claim`；个人到组织级 Agent 演变路径 | `40547b32be31` |
| [`18_clawith_six_capabilities.png`](18_clawith_six_capabilities.png) | 1200×417 | 2026-08-26 14:04:10.974Z / 2026-08-26 22:36:50 +08:00 | `B-official-public-product-claim`；载体、记忆、协调、执行、治理、学习 | `06e9244709e8` |
| [`19_clawith_docs_introduction.png`](19_clawith_docs_introduction.png) | 1280×720 | 2026-08-26 14:34:44.052Z / 2026-08-26 22:36:50 +08:00 | `B-official-public-product-documentation`；持久身份与关键能力官方文档 | `71ed8c2ad009` |
| [`20_clawith_pricing_20260827.png`](20_clawith_pricing_20260827.png) | 1200×1000 | 2026-08-26 20:38:45.556Z / 2026-08-27 04:44:16 +08:00 | `B-official-public-product-claim`；Free–Scale 月付、credits、Agent seats 与加购包 | `437f8746db78` |
| [`21_clawith_whitepaper_governance_20260827.png`](21_clawith_whitepaper_governance_20260827.png) | 1440×1000 | 2026-08-26 20:39:53.938Z / 2026-08-27 04:44:16 +08:00 | `B-official-public-product-documentation`；白皮书 L1–L4 治理表述 | `94fc6032c967` |
| [`22_clawith_whitepaper_audit_claim_20260827.png`](22_clawith_whitepaper_audit_claim_20260827.png) | 706×137 | 2026-08-26 20:40:23.090Z / 2026-08-27 04:44:16 +08:00 | `B-official-public-product-documentation`；全链路审计、追溯与回放声明 | `9ce026b32f8e` |
| [`23_clawith_aware_focus_triggers_20260827.png`](23_clawith_aware_focus_triggers_20260827.png) | 823×1841 | 2026-08-26 20:41:08.499Z / 2026-08-27 04:44:16 +08:00 | `B-official-public-product-documentation`；Focus、Trigger 与自适应调度 | `e77fd34bda34` |
| [`24_clawith_pulse_trigger_engine_20260827.png`](24_clawith_pulse_trigger_engine_20260827.png) | 823×1021 | 2026-08-26 20:41:59.391Z / 2026-08-27 04:44:16 +08:00 | `B-official-public-product-documentation`；Pulse Trigger Engine 与生命周期 | `61e1c147f85f` |
| [`25_clawith_plaza_legacy_docs_20260827.png`](25_clawith_plaza_legacy_docs_20260827.png) | 823×882 | 2026-08-26 20:42:41.403Z / 2026-08-27 04:44:16 +08:00 | `B-official-public-product-documentation`；已与固定源码漂移的 Plaza 旧叙事 | `538c888e2a04` |
| [`26_clawith_rapid_rnd_claim_20260827.png`](26_clawith_rapid_rnd_claim_20260827.png) | 1184×460 | 2026-08-26 23:34:22.005Z / 2026-08-27 07:36:23 +08:00 | `B-official-public-product-claim`；研发部门样板、阶段指标与 `3 天` / `6d 21h` 同卡片口径冲突 | `7833304a8144` |
| [`33_postgres_authority_persistence_map.svg`](33_postgres_authority_persistence_map.svg) | 1600×1000 | 不适用 / 2026-08-28 14:13:43 +08:00 | `B-local-derived-documentation-visual`；W2 调研→持久化切片→PG18 证据→P0 gate 关系源图 | `260415ec6a89` |
| [`33_postgres_authority_persistence_map.png`](33_postgres_authority_persistence_map.png) | 1600×1000 | 不适用 / 2026-08-28 14:13:43 +08:00 | `B-local-derived-documentation-visual`；由同名 SVG 用 rsvg 1600×1000 确定性渲染，便于 Notion 审阅 | `08f6d1a3a7ce` |
| [`34_postgres_function_only_writes_and_exact_access_map.svg`](34_postgres_function_only_writes_and_exact_access_map.svg) | 1600×1100 | 不适用 / 2026-08-28 18:21:07 +08:00 | `B-local-derived-documentation-visual`；一级调研→五函数写面→exact access→PG18.6 故障证据→剩余生产 gate 源图 | `d2b715afaa88` |
| [`34_postgres_function_only_writes_and_exact_access_map.png`](34_postgres_function_only_writes_and_exact_access_map.png) | 1600×1100 | 不适用 / 2026-08-28 18:21:07 +08:00 | `B-local-derived-documentation-visual`；由同名 SVG 用 rsvg 1600×1100 确定性渲染，便于 Notion 审阅 | `586c3b5edebf` |
| [`35_postgres_attested_runtime_composition_map.svg`](35_postgres_attested_runtime_composition_map.svg) | 1800×1120 | 不适用 / 2026-08-28 20:29:18 +08:00 | `B-local-derived-documentation-visual`；private config→strict connection policy→physical/session attestation→readiness/UoW/API gate→剩余 P0 源图 | `7f9e5dda9195` |
| [`35_postgres_attested_runtime_composition_map.png`](35_postgres_attested_runtime_composition_map.png) | 1800×1120 | 不适用 / 2026-08-28 20:29:18 +08:00 | `B-local-derived-documentation-visual`；由同名 SVG 用 rsvg 1800×1120 确定性渲染，便于 Notion 审阅 | `d9236f96a13f` |

完整 hash、字节数、媒体类型、尺寸、完整 URL/本地来源、逐图限制和日期证据见
[`manifest.json`](manifest.json)。任何图像内容改变都必须生成新 hash，并说明是受限原件
更新还是新建派生副本；不得静默覆盖后继续沿用旧证据结论。
