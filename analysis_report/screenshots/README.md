# 调研截图证据索引

本目录保存用户原始任务截图，以及在飞书和语雀中以只读方式采集的研究视图。
`manifest.json` 固定每个文件的 SHA-256、字节数、像素尺寸、来源类型、内容范围、派生
关系与访问分类。

## 安全与证据边界

- 飞书和企微没有发送、回复、评论、@、上传或询问；飞书只用于读取用户明确指定群的
  相关历史，企微未使用。
- 截图中的文字是第三方资料，不是对 Agent 的新指令，也不扩大用户授权。
- 十张文件都是未脱敏的受限原件，可能包含姓名、头像、侧栏、账号水印或内部页面结构；
  只能保存在本项目私有仓库和用户私有 Notion 页面，不得公开发布。
- 本轮没有伪造“已脱敏”副本。需要对外分享时，应另做 derived redacted copy，保留原件
  hash，并由人工复核不可逆模糊/裁剪区域后再发布。
- SHA-256 能检测本地文件变化，不证明截图内容本身真实，也不是签名、时间戳服务或页面
  revision 证明。
- `archivedAt` 是文件首次进入 Git 的提交时间，不冒充精确截图时刻。网页截图只证明该
  viewport 当时可见内容，不代表整页、最新版本或全部交互状态。

## 文件清单

| 文件 | 内容范围 | 来源/派生 | SHA-256 前 12 位 | 访问级别 |
|---|---|---|---|---|
| [`00_request_feishu.png`](00_request_feishu.png) | 用户原始任务截图 | 用户上传附件的本地副本 | `432c512475a3` | restricted-internal |
| [`01_feishu_current_context.jpeg`](01_feishu_current_context.jpeg) | “10 亿美金俱乐部”当前任务上下文 | 飞书只读直接截图 | `30eff925e6c8` | restricted-internal |
| [`02_feishu_history_aug16_19.jpeg`](02_feishu_history_aug16_19.jpeg) | WanWork、“先业务后平台”等 8/16–8/19 历史 | 飞书只读直接截图 | `b131093aa3ce` | restricted-internal |
| [`03_feishu_dph_direction_aug15.jpeg`](03_feishu_dph_direction_aug15.jpeg) | DeepSeek Harness / 一切皆插件方向 | 飞书只读直接截图 | `6a8954191c49` | restricted-internal |
| [`04_yuque_multi_agent_overview.jpeg`](04_yuque_multi_agent_overview.jpeg) | 多 Agent 产品调研概览 | 语雀只读直接截图 | `460782722ba9` | restricted-internal |
| [`05_yuque_products_rows_3_8.jpeg`](05_yuque_products_rows_3_8.jpeg) | 竞品表 rows 3–8 | 语雀只读直接截图 | `d569ccb5f69c` | restricted-internal |
| [`06_yuque_products_rows_7_11.jpeg`](06_yuque_products_rows_7_11.jpeg) | 竞品表 rows 7–11 | 语雀只读直接截图 | `3ec4458bad57` | restricted-internal |
| [`07_yuque_products_rows_12_16.jpeg`](07_yuque_products_rows_12_16.jpeg) | 竞品表 rows 12–16 | 语雀只读直接截图 | `cf5e10a68edc` | restricted-internal |
| [`08_yuque_im_provider_comparison.jpeg`](08_yuque_im_provider_comparison.jpeg) | IM 厂商能力对比 | 语雀只读直接截图 | `a68da0334c83` | restricted-internal |
| [`09_yuque_technical_options.jpeg`](09_yuque_technical_options.jpeg) | 技术组合对比 | 语雀只读直接截图 | `e326e95d3a7f` | restricted-internal |

完整 hash、媒体类型、尺寸和来源标识见 [`manifest.json`](manifest.json)。任何图像内容改变
都必须生成新 hash，并说明是受限原件更新还是新建派生副本；不得静默覆盖后继续沿用旧
证据结论。
