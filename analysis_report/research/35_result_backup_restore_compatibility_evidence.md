# E3 Result Authority：备份、恢复与迁移兼容证据

> 证据日期：2026-08-29（Asia/Shanghai）  
> 分支：`mainline_continue_quantum_entanglement`  
> 代码/测试修订：`4af41c7`  
> 远端：`origin/mainline_continue_quantum_entanglement`  
> Notion 状态：`local_pending`；本地阶段完成后批量上传并逐页回读

## 结论

本轮复核了 result authority 的 migration-7 激活、空库回退、非空库拒绝回退、活动拓扑、非空
备份/校验/恢复、clean-process reopen/reconcile，以及备份发布边界的 `SIGKILL` 恢复。39 项
专项测试全部通过。结果备份仍是显式 opt-in 的本地合同，不改变默认 store 的 feature-off 状态，
不打开 worker、publication、真实 IM 或任何外部副作用。

## 覆盖范围

| 专项 | 收集数 | 关键断言 |
| --- | ---: | --- |
| `test_result_migration_activation.py` | 7 | migration-7 激活元数据/依赖、空库受保护回退、非空库 fail-closed |
| `test_result_backup.py` | 8 | 非空 result graph 备份/验证/恢复、clean-process reconcile、快照一致性、发布边界 SIGKILL、manifest/database tamper |
| `test_result_backup_topology.py` | 7 | active catalog 精确派生、stats/rogue object/schema drift 拒绝 |
| `test_inactive_invocation_results_backup_topology.py` | 4 | inactive candidate topology 不被误激活 |
| `test_inactive_invocation_results_migration.py` | 13 | legacy candidate descriptor、依赖、codec、迁移 rollback 和 sidecar 约束 |

`test_result_backup.py` 通过模块属性引用 durable-prerequisite helper，避免 pytest 把被导入的
`unittest.TestCase` 当作本模块的第二套测试收集；行为测试本身未被删除或跳过。

## Clean-process 恢复路径

1. 父进程在 opt-in migration-7 数据库中建立一份合法、含 Artifact 的结果图；
2. 生成带 canonical topology、行计数、页面几何和 SHA-256 的 result backup；
3. 在新解释器中重新打开 restored SQLite，读取 receipt identity，执行 receipt-bound
   `reconcile_scoped_invocation_result()`；
4. 再次读取 `ObservedV2`，断言与 reconcile 返回的 observation 完全相同；
5. 进程只打印无身份/凭据的 `{"reconciliation": "reconciled", "stable": true}`，父进程严格
   比对该 JSON，确保 clean-process 断言不是自比较假阳性。

另有独立连接在未提交写事务持锁期间创建 backup，验证备份读取上一个 committed snapshot；源库
未提交的 timestamp 不会泄漏到备份。发布过程的两个 hard-link 边界分别注入真实 `SIGKILL`，
恢复扫描只清理模块命名空间的临时文件，并把状态准确分类为 `incomplete` 或 `complete`。

## 迁移和回退语义

- 默认 `SQLiteEventStore(...)` 仍只接受 legacy schema；migration-7 必须显式
  `enable_result_acceptance_schema=True`。
- 激活先验证 legacy 1--6、domain sidecar、registry digest 和 dependency edges，再在同一
  transaction 写入 activation metadata。
- 空库可以执行受保护 rollback，保留 sidecar 所需的 legacy/domain 元数据；任何非空 result
  graph 都拒绝 rollback，不删除可恢复数据。
- backup/restore 只接受精确 active result topology；legacy/inactive/rogue schema、manifest
  digest、database bytes、目标已存在或 symbolic source 均 fail closed，不覆盖既有文件。

## 可复现命令与结果

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_result_migration_activation.py \
  tests/test_result_backup.py \
  tests/test_result_backup_topology.py \
  tests/test_inactive_invocation_results_backup_topology.py \
  tests/test_inactive_invocation_results_migration.py
```

结果：**39 tests passed**。

```bash
.venv/bin/ruff check tests/test_result_backup.py
git diff --check
```

以上命令通过。本证据只覆盖本地 SQLite result authority；不宣称跨主机、跨版本生产数据库
升级、PostgreSQL、容量、RPO/RTO、灾备演练或 GA。

## 未关闭门禁

仍待独立完成：全系统每个边界的 kill matrix、双进程中途 kill/lease expiry、restore 后完整
event/attempt/projection replay 对账、版本兼容/回滚矩阵（至少上一 tag 与当前 schema）、
长时 heartbeat/graceful drain、可信认证和全 repository tenant scope、production composition
及 receipt-bound worker promotion。真实 connector、飞书、企微、语雀、Notion outbound 继续
保持关闭。

## 本地优先同步策略

本文先进入本地 `analysis_report/research`、Git commit 和远端分支备份。Notion 保持
`local_pending`；本地阶段全部完成后再批量同步正文、证据和截图，逐页回读并更新
`analysis_report/notion_sync_manifest.json`，回读完成前不声明 Notion 同步闭环。
