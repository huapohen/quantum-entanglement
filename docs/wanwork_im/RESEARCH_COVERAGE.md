# `2output` 调研组合覆盖清单

> 快照日期：2026-08-27
>
> 覆盖审计：2026-08-28
>
> 适用分支：`dev_wanwork_quantum_entanglement`

## 1. 结论

用户指定的本机调研根目录是：

```text
/Users/lwblx/huapohen/agent/automation/2026/05_08/1/2output/more
```

本清单固定了其中全部 **40 份 Markdown、45,909 行**：31 份产品/协议/安全专题报告，以及 9 份
导航、组合分析、方法和追踪文档。产品仓库不复制 66MB 研究原件，只保存内容摘要、SHA-256、采用/
拒绝/延后决定及验收合同；本地原件仍是证据快照。

原研究 `README.md` 和 `_portfolio/product_inventory.md` 声明 30 份独立报告，但目录当前有 31 份
`research_report.md`。新增的 `agentspace/research_report.md` 生成时间晚于组合总报告，未进入原来的
README、产品目录和 WanWork RQ-001～RQ-038。这一差异已被视为显式 evidence delta，而不是静默
假定“总报告已经覆盖”。

## 2. 使用纪律

- `G`（governing）决定证据标签、产品主线或安全不变量；必须进入需求/架构/验收。
- `D`（direct）提供 WanWork 直接吸收或明确拒绝的产品/技术证据；至少有 RQ 或审计处置。
- `C`（comparative）用于校准产品取舍、风险和后续扩展；不自动升级为 M0 必做。
- `W`（watch）只用于发现和动态跟踪；不能单独证明实现、采用或市场成立。
- `[F]` 可核验事实、`[C]` 厂商主张、`[A]` 分析判断、`[U]` 未知必须保留原等级；研究结论
  不能冒充已交付能力。

## 3. 完整内容清单

| # | 来源 | 行数 | SHA-256 | 角色/处置 |
|---:|---|---:|---|---|
| 1 | `README.md` | 137 | `beb61f61912d3d8c7d90a89bdaa828db0f0ad9e6c0cd9125d36f9f31f9f7c3b0` | G：阅读顺序、组合边界与审计口径 |
| 2 | `_portfolio/corrections_and_uncertainties.md` | 160 | `8da895208f8e326459ceae5fa1327e49ac256bf4d17c65c2f26700d2470b8f59` | G：防止把营销、模块存在或协议原语写成生产事实 |
| 3 | `_portfolio/frontier_signal_ledger.md` | 244 | `4502d0cf12122f480e9f000782606e41e01ef32e5bf92080296c46d6a65ee911` | W：发现信号；不作为能力或采用证明 |
| 4 | `_portfolio/master_research_report.md` | 1,791 | `02366d7b7dcfdda96309b22142376217caff2b752e770e71a8a8e2d8cb8c2787` | G：Task、Trust、Artifact、Attention、durability 产品主线 |
| 5 | `_portfolio/opportunity_map.md` | 657 | `a028ee9002ba9815c673d187033fb5c8104ba51075f7144769f1af5466dacd4c` | G/C：平台扩张与 kill criteria；机会不自动进入当前范围 |
| 6 | `_portfolio/product_inventory.md` | 114 | `c47d70ff371eff0454d7ba5f046444d268294a0fafaed8831e5bf9376ded550a` | G：原 30 份报告边界；不含后生成 AgentSpace |
| 7 | `_shared/clawith_quality_reference.md` | 579 | `1a83d7ac52014aecc22377af18aeb20a993876a65e638910019631f234c1f0bd` | G：报告深度、证据和视觉质量基准 |
| 8 | `_shared/research_methodology.md` | 191 | `9249873bcba62bca2b275923bdebffb888ae08438bef06aca3f4ab2cf31bf9e4` | G：F/C/A/U、来源优先级和验收方法 |
| 9 | `_watchlist/frontier_accounts_and_channels.md` | 563 | `bcd7e68a6b423e0f34c06a2f7caa1e7e01e01d66690e9076952aee07e81c5dd9` | W：长期雷达；动态信息上线前重验 |
| 10 | `agentspace/research_report.md` | 3,083 | `9698be0f74d81c2078e208a3231f3e6498965fedeb3a3aba164764279bd8f0b7` | D：新增 delta；控制/执行面、输出 promotion、queue/approval/runtime 反例 |
| 11 | `agentteams/research_report.md` | 1,141 | `b9ace0fc4a8e0c8be7cf49e3a530428e097f026040fc19a6d314995e38d67a9c` | D：透明 room、HITL、submission/acceptance；拒绝 Docker/socket 泛权 |
| 12 | `clawith/research_report.md` | 1,588 | `9894dbfbf6f8b1a5eca987c01d4556888160ca1a0721a4e6503ebe1d7a188bc7` | D：Participant/Handoff/Needs You/Skill/Tool/Experience 与 Action unknown |
| 13 | `codexloom/research_report.md` | 912 | `cd150e18b58fcc1760c1154b647de44f9a71fcc891211216e50b2bc0bb637a1f` | D：责任消息、Artifact、Needs You、会话级 Agent 身份/承诺边界 |
| 14 | `coze-coze-studio/research_report.md` | 906 | `02bab73903e0ac0c3b894a547c59829ab7c94deab35f5af43dddf0b4f427d429` | C：托管/开源双边界、Skill 复用与治理；不照搬大而全 builder |
| 15 | `deepseek-harness/research_report.md` | 1,281 | `8f6cfb194a114d1b3a324db17e650a637e0962fdb1b6aefa81b84597ea0330b4` | D：插件 capability seam、effective composition、event spine；拒绝同 UID 信任 |
| 16 | `floatim-floatboat/research_report.md` | 1,018 | `fe239ede133ca4cc0168a20cac8e43bf0673fafb7fd1d895b69584ba82e432ac` | D：Agent-native IM、mention/presence/data route；拒绝头像式 Agent 身份 |
| 17 | `gottao-pi-agent/research_report.md` | 995 | `f9a79cb722e5606608a3fad5359ad00115022d11baa06411ae138a6a1f90974e` | C：主体/上游漂移、租户与退出可移植性、Outcome Ledger |
| 18 | `holaos/research_report.md` | 1,883 | `b5582eaea50c22732bd7a66ce562735e47ec80597da3038f4d595b5419601572` | D：Environment、run projection、data route；拒绝 local=private 与 legacy fail-open |
| 19 | `internet-court-skill/research_report.md` | 1,582 | `1cb93a023982504ffc17aab459b7eb69ecb77aabf1afc32dbeeb694f9416361f` | C/D：mandate→evidence→remedy、整树供应链；交易/escrow 后置 |
| 20 | `kirocrew/research_report.md` | 1,234 | `922e3203e072e06d65e5f9f2d5b01be65bb326fb5c176acb613b3aa822ee8b40` | D：持续 workspace 与安全门；明确 App 代码不受 Tool gate 隔离 |
| 21 | `mindra/research_report.md` | 1,399 | `df99fe74156613a660480b205c10a7cc0f75fd96a8d98e38efce0766a06dc3ed` | C：AI Department、连接器/审批/企业采购证据；徽章不等于控制有效 |
| 22 | `multica/research_report.md` | 1,055 | `250960ba2611be6ae44dddf0143591bb69c047bbe095fcf5b11c4f5d30cca3b9` | C/D：coding fleet 与 host/worktree 权限；许可证与默认权限分开 |
| 23 | `near-ai-agent-market/research_report.md` | 837 | `c4472df454f26b4a41c37cdedff37c43d80f148ea07b1aa2507bdd5d0a4cc3c5` | C：市场必须有履约/争议/可用性；公共 marketplace 延后 |
| 24 | `omnigent/research_report.md` | 1,575 | `8ce6200baa616a475d3926a961b14755fcbb700c8e54ee86202cd8d9d25897cc` | D：跨 harness adapter/capability matrix；拒绝“能启动=语义无损” |
| 25 | `open-connector/research_report.md` | 1,197 | `d43ffe461bc8362eba05bfe92f192a1218c70980d13a0350378a80ea60c41974` | D：OAuth/secret broker、Action Gateway、scope 执行；目录数量不是 E2E |
| 26 | `openagents/research_report.md` | 1,543 | `05f3feb8889430236e9e7211beb2004613145d8634ba5afb5840069a20a5916c` | D：workspace/Task/Routine/Inbox/protocol adapter；拒绝 owner token/fail-open |
| 27 | `openbot/research_report.md` | 2,015 | `264acec43a07c78c45715722b4aaef2e5a9b088ccaedc0fdacb11cd3a6bd994e` | D：独立电脑、action policy、audit-before-act、attention inbox |
| 28 | `openworker/research_report.md` | 1,196 | `9fdd7b1d1b54321145dc14012ee672d6feba0d3a9e08756667e4f1016c56bd15` | C/D：local-first coworker/runtime/Skill/MCP；默认宿主权限不等于 sandbox |
| 29 | `orca/research_report.md` | 1,157 | `2d32de3d2db2eec3c9da5bec9b2ae72611aff3ceef32cb81dc4d8d80b5e5fa8f` | D：fleet/worktree、移动结构化审批、unverifiable liveness |
| 30 | `pi-agent/research_report.md` | 2,313 | `368ed858edb6b5860056edeef0320cf91302c58d57ecf9e0131e4c7dd10f2060` | D：小内核、session/provider/extension seam；同权 package 必须外部隔离 |
| 31 | `protocol-a2a/research_report.md` | 1,024 | `21e29a0e340e74b177e975a5912c12247d71263a1e0e355d44839af27ffb8ff5` | G/D：Agent 互操作 adapter；Card/Task/Artifact 不是信任/验收 |
| 32 | `protocol-acp-dual/research_report.md` | 949 | `9541e8137812a59b8c99720fec5ccede16a36c4a2c5e2edf25642c45c4287899` | G/D：Client ACP 与旧 Communication ACP 分名、分版本 |
| 33 | `protocol-mcp/research_report.md` | 1,199 | `6aa6bc551458c817a81dc52dbe30b5bb2589f1184f27f4831a067817d3ffd462` | G/D：Tool/context seam 与 current spec；Registry 不提供代码信任 |
| 34 | `qianwen-ai-platform/research_report.md` | 1,442 | `7be176e4288ea9694f01e1ae0bd98adc234d4748cdceaef2f46d9ab06efe7b98` | C/D：多模型数据政策、Skill/cloud action、成本与兼容回归 |
| 35 | `raft-slock/research_report.md` | 1,086 | `5f2d64dd62c9cc5243074439e42733b2fae7f07614c371d5088d9468a189214a` | D：持续身份、跨组织 room、外部 Agent；activity 不冒充 audit |
| 36 | `sandbase-harness/research_report.md` | 1,702 | `03255d84e4e14694ac0018fee94db99cfac7369dd168c1fe9a4915abf145d922` | D：sandbox/credential/memory/replay 名词与真实 enforcement 分开 |
| 37 | `tech-agent-security-governance/research_report.md` | 1,014 | `a0c97009a04aaaaa47c5437b2d66bc8c09ef2fb7107f0af51e1dab05e64e2bb9` | G/D：身份、能力、隔离、供应链、egress、evidence/recovery 控制栈 |
| 38 | `todos-dev/research_report.md` | 833 | `cb9ec24e7e49f3ee7eb98c2c4bf017461fb40a3e108495b2fbbde3c75ccc4b62` | D：Plan/Diff、Attention、Universal Artifact、长任务故障矩阵 |
| 39 | `tutti-vm/research_report.md` | 1,436 | `51af72a312f8b3fc3bf86e96e3fd8a8f5c5d2a4ee0eb62944f35fc9f52bda19d` | D：共享 workstate、能力 lease、冲突语义；云 Room/本地边界分开 |
| 40 | `youmind/research_report.md` | 878 | `ed224545e95cd21a7db00f99b7bc7fd24b9c4f28ca4f745e5feb983b7c3fe020` | C/D：Artifact OS、Skill SOP、上下文/隐私路由；渠道声明不可外推 |

## 4. 覆盖与变更门禁

1. `RESEARCH_TRACEABILITY.md` 只承载会改变产品/API/安全/验收的硬映射；本文件证明所有研究源都
   已被盘点，不要求把 40 份材料各制造一个功能。
2. 比较型和 watch 型证据必须写清 `adopt / reject / defer / watch`，不得因为“值得关注”就塞入 M0。
3. 任一文件行数或 SHA-256 变化，先产生 evidence delta，再修改 RQ、PRD、架构和实施计划。
4. 新增 `research_report.md` 时必须同时更新本清单、组合报告差异说明和追踪矩阵；不能只依赖旧
   README 的报告数量。
5. 上线或采购时重新验证动态事实、合同、协议版本、provider 行为和安全配置；本清单只固定
   2026-08-27 的研究快照。
