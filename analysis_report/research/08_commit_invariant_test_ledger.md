# 提交—模块—不变量—测试证据—残余边界台账

更新日期：2026-08-20

实现基线：`86ee4bcc9229c1f5d998b33c818c49e9d761b961`

实现 tree：`9c85d5d43f20ddf645b56c78a72e2c97dc7fd4db`

## 1. 结论与使用方式

本台账回答的不是“提交了多少代码”，而是每组关键提交究竟建立了什么可复核不变量、
哪些测试直接约束它、以及该证据还不能推出什么。当前准确结论仍是：仓库已经形成一组
严格的单节点 SQLite 协作内核 primitives，但尚未形成可承载真实客户和不可逆外部副作用的
端到端生产系统。

阅读规则：

- “提交”列只列建立或显著收紧该不变量的关键提交，不声称覆盖其间每个格式、文档或 CI
  提交；完整历史以 `git log --oneline f7be4e2..86ee4bc` 为准。
- “测试证据”列给出当前树中真实存在的 test file 和代表性 test method。通过这些测试只
  证明对应测试模型、fixture 和故障注入；不自动证明多节点、真实 IM、云环境或 GA。
- “残余边界”是保证的一部分。未被接入同一 durable transaction 的 primitives 不能合并
  描述成端到端 exactly-once。
- `analysis_report/research/07_current_implementation_status.md` 的 531 项测试数字绑定其明确
  标注的旧 commit `e4cbf04`；本台账给出后续实现基线，二者不是互相矛盾的同一时点数据。

## 2. 本次复核基线

| 项目 | 实测或 Git 证据 | 限制 |
|---|---|---|
| 工作位置 | 独立 worktree `/private/tmp/qe-evidence-ledger`；实现从 `86ee4bc` 分出 | 不是 clean clone；本次报告提交只改 `analysis_report` |
| 平台 | Darwin arm64；CPython 3.9.6 | 没有在本次台账工作中重跑 Linux/Python 3.12 matrix |
| 单元/集成测试 | `PYTHONPATH=src python3 -m unittest discover -s tests -q` → `Ran 561 tests ... OK` | 2026-08-20 本地一次运行；没有签名或外部留存的 test attestation |
| 代码身份 | implementation commit `86ee4bc`；tree `9c85d5d...` | 测试执行时只多出已提交的截图文档，不涉及 `src/tests/scripts` |
| 历史规模 | `f7be4e2..86ee4bc` 为 177 commits；118 files；`+50,851/-11` | 行数与提交数是规模证据，不是质量或生产成熟度证据 |
| 外部 connector | 仓库测试使用 fake/fixture；没有真实 Feishu/WeCom write connector | 不能声称已验证消息投递、平台回执或第三方限流 |

## 3. 可追溯不变量台账

### 3.1 协作内核、状态与恢复

| 关键提交 | 模块 | 已提交不变量 | 直接测试证据 | 残余边界 |
|---|---|---|---|---|
| `3e7bf30` | `protocol.py`、`events.py`、`store.py`、`artifacts.py` | Envelope 保留 correlation/causation/authority；event append 有 stream 顺序、幂等键和乐观并发；基础 artifact version append-only | `tests/test_protocol.py::test_envelope_round_trip_preserves_causation_and_authority`；`tests/test_store_artifacts.py::test_append_is_ordered_and_idempotent`、`::test_optimistic_concurrency_rejects_stale_writer` | 内部 envelope 不是正式 A2A/MCP 网络兼容证据；SQLite stream 不是跨服务事务日志 |
| `088348e` | `runtime.py`、`scheduler.py`、`context.py`、`policy.py`、`plugins.py` | DAG 校验、显式 task transition、依赖阻断、受限并发、Needs You 和 context omission 均成为确定性状态 | `tests/test_runtime.py::test_independent_tasks_run_in_parallel_and_initial_ready_is_recorded`、`::test_failed_task_blocks_downstream_without_model_guessing`；`tests/test_scheduler_context.py::test_cycle_and_missing_dependency_are_rejected`、`::test_required_context_is_never_silently_truncated` | 初始 plan/task/event 发布尚未被证明为一次完整原子初始化；模型 worker、artifact terminal 与 durable attempt 仍未统一事务 |
| `661a638`、`10ccfcc`、`c832bd6`、`4363a8f`、`a921fbd` | `runtime.py` workflow recovery | 恢复会校验 canonical plan、精确 task manifest、task transition 合法性、页连续性与最大事件数；失败时不发布部分恢复 projection | `tests/test_recovery.py::test_completed_plan_is_recovered_without_invoking_agents_again`、`::test_session_recovery_uses_bounded_contiguous_pages`、`::test_late_invalid_transition_shape_never_publishes_partial_recovery`、`::test_session_recovery_requires_an_exact_task_creation_manifest` | 当前先物化最多 100 万事件；没有累计 JSON 字节/对象预算和真正流式 replay；plan/graph/approval queue 的最终发布还不是持久化级原子动作；RUNNING attempt 崩溃收敛未端到端接入 |
| `d84e6c0` 及后续 `f9814e1` | `agent_runtime.py`、`adapters/deepseek_harness.py` | runtime port 隔离可选 SDK；同一 session 串行；重复 invocation key 只执行一次；close/drain 和不支持 cancel 具有显式语义 | `tests/test_agent_runtime.py::test_duplicate_concurrent_invocations_execute_harness_once`、`::test_turns_for_same_dsh_session_are_serialized`、`::test_close_rejects_new_work_then_drains_and_closes_harness`、`::test_canceling_waiter_reports_remote_turn_is_still_running` | 未证明进程/容器沙箱、网络隔离、远端 cancel 或远端未知结果 reconciliation；runtime 尚未接 durable attempt store |

### 3.2 事件投递、attempt、artifact 与 durable JSON

| 关键提交 | 模块 | 已提交不变量 | 直接测试证据 | 残余边界 |
|---|---|---|---|---|
| `1c708cf`、`bb6c4c3`、`3ee10b2`、`d0fdde2`、`afb0d64` | `delivery.py`、`publisher.py`、`store.py` | domain event 与 outbox 同事务；inbox 去重与 receipt 同事务；publisher 使用 lease/fencing、bounded admission、retry/DLQ、unknown-outcome quarantine；receipt 拒绝 string coercion | `tests/test_delivery.py::test_event_and_outbox_commit_together_and_retry_idempotently`、`::test_takeover_fences_both_old_publisher_ack_and_nack`、`::test_open_ambiguity_blocks_takeover_until_operator_resolution`；`tests/test_publisher.py` 的 publisher 故障、timeout、admission 与 lease 用例 | fake connector callback 成功不等于接收方业务 acceptance；没有认证 operator resolution 服务；远端无幂等/查询时仍不能给出 exactly-once 保证 |
| `4cf23ab`、`3e2d635`、`1d33e78`、`66feda1`、`680c0bb`、`9082bf4`、`9f84b6d` | `attempts.py` 与 migration 0001 | enqueue/claim/heartbeat/recovery/terminal CAS 均 durable；并发连接/进程只有一个 claim winner；旧 worker 被 epoch/lease fencing；读取与 retry policy 有硬边界 | `tests/test_attempts.py::test_two_processes_have_one_atomic_claim_winner`、`::test_heartbeat_recovery_and_fencing_reject_stale_worker`、`::test_terminal_cas_rejects_completion_at_exact_expiry`、`::test_attempt_pages_are_bounded_ordered_and_cursor_based` | Orchestrator 的实际 Agent run 尚未全部经此 store；没有 runtime heartbeat/cancel/action receipt 的组合崩溃矩阵 |
| `21f30f6`、`2fdc685`、`da9bcd1`、`520ffb5`、`bed301a`、`8e0b529`、`815b028`、`bb79a0c` | `artifact_store.py` 与 migration 0002 | blob 与 version metadata 原子提交；content address、scope、version CAS、lineage、exact decode、bounded history/read 在 SQLite 中可复核 | `tests/test_artifact_store.py::test_content_and_metadata_commit_atomically_with_contiguous_versions`、`::test_two_processes_allocate_unique_versions`、`::test_read_detects_blob_and_metadata_tampering`、`::test_history_enforces_a_content_byte_budget_before_materializing_blobs` | runtime 的 task completion/event 与 durable artifact commit 尚不是一次原子提交；无对象存储、大对象上传、tenant identity 或 durable ledger head watermark |
| `b64fe58`、`bcc7591`、`1202107`、`1b3611c`、`81c7ac3` | in-memory `ArtifactLedger` replay | 所有公开 metadata 返回为隔离 JSON snapshot；replay 校验 ID/URI/version 连续性、lineage digest、分页边界；rebuild 成功时一次替换状态，晚失败保留旧状态 | `tests/test_store_artifacts.py::test_every_public_read_returns_an_independent_plain_json_metadata_snapshot`、`::test_replay_verifies_ref_lineage_digest_and_task_consistency`、`::test_successful_rebuild_replaces_stale_state_in_one_step`、`::test_late_replay_failures_preserve_the_exact_previous_state` | 仍是内存 projection；缺 tenant identity、全链 head digest/watermark 和跨进程持久化 publication |
| `1568690`、`91508f1`、`3d3887d`、`fc05962`、`e7d5ea6`、`ddaff17`、`fb5b511` | `store.py` persisted JSON/read boundary | 写入拒绝 NaN/Infinity、cycle、深度/宽度/编码字节越界；读取不做 scalar coercion；event/outbox/inbox/snapshot/page 都使用有界解析和 SQL LIMIT/keyset cursor | `tests/test_store_json.py::test_structural_limits_reject_cycles_depth_width_and_oversized_scalars`、`::test_persisted_event_json_is_decoded_as_bounded_object`、`::test_persisted_delivery_scalars_are_validated_before_use`；`tests/test_store_read_bounds.py::test_each_page_query_has_a_sql_limit` | 单条/单页约束不等于整个 session/workflow 累计资源预算；SQLite 文件仍在可信本机边界内 |

### 3.3 迁移、备份与 projection

| 关键提交 | 模块 | 已提交不变量 | 直接测试证据 | 残余边界 |
|---|---|---|---|---|
| `a9a015c`、`e504a46`、`2be1af3`、`b590051`、`bea3be3` | `migrations/__init__.py` | packaged SQL checksum、连续 ledger、schema validator 与失败原子性共同约束 migration；初始化异常关闭连接 | `tests/test_migrations.py` checksum/order/failure-atomicity 用例；`tests/test_store_initialization.py::test_base_exception_during_initialization_closes_connection` | 只覆盖已登记 SQLite schema；没有跨版本 rolling deployment、在线大表迁移或所有旧 binary compatibility matrix |
| `c9a890d`、`1332416`、`758c902`、`4d693f8`、`9b48d5a` | `backup.py` backup/verify | WAL 一致快照、canonical bounded manifest、migration/schema 校验、stable descriptor/inode 与不可覆盖 publication 在本地 POSIX 模型中 fail closed | `tests/test_backup.py::test_online_backup_captures_wal_state_and_verifies_manifest`、`::test_backup_path_replacement_during_verification_is_rejected`、`::test_future_migration_is_rejected_before_backup_publication`、`::test_weakened_migration_owned_table_is_rejected_before_backup_publication` | 无加密/KMS、远端保管、调度、保留策略、容量实测和生产 RPO/RTO |
| `c7f6107`、`e60bd3c` | `backup.py` restore | 仅从 verified backup 恢复到新目标；copy 期间重验 source/manifest/parent；exact bytes；失败不覆盖 operator 文件 | `tests/test_backup.py::test_verified_backup_restores_to_new_database`、`::test_restore_rehearses_outbox_ambiguity_artifact_and_attempt_state`、`::test_restore_detects_backup_in_place_change_during_copy`、`::test_restore_destination_race_preserves_operator_file` | rehearsal 是测试数据库；缺定期 retained drill、全服务恢复后 reconciliation、密钥/外部制品恢复与实测 RTO |
| `ad99b9b`、`a27e27c`、`f2e4ac0`、`d26c386`、`6a75e10`、`6fdf030`、`60c24f9` | `domain_migrations.py` | registry/sidecar/bridge state 与 plan 使用 canonical digest；planner 只产生封闭动作；apply 在 write lock 内重验 source 并原子安装 sidecar/bootstrap legacy metadata | `tests/test_domain_migrations.py::test_all_exact_shapes_map_to_the_closed_action_set`、`::test_stale_source_is_rejected_under_lock_without_mutation`、`::test_absent_legacy_plan_installs_and_bootstraps_in_one_transaction`、`::test_two_concurrent_consumers_have_one_winner_and_one_stale_rejection` | 当前只实现 bridge-only 路径；native/sparse/v4 与完整 upgrade/downgrade executor/rehearsal 未实现，因而仍 fail closed |
| `efc0657`、`2299bdf`、`088f077`、`334249e`、`9e25fe3` | `projections.py` schema/lease/upcast | exact SQLite schema 与 catalog collision 校验；lease/epoch/fencing、monotonic offset CAS、sealed upcaster/decoder、bounded source batch | `tests/test_projections.py::test_column_and_table_constraint_drift_fail_closed`、`::test_case_variant_catalog_collisions_fail_before_transaction_or_write`、`::test_concurrent_claims_have_exactly_one_owner`、`::test_projector_upcasts_and_checkpoints_a_bounded_batch` | 仍是单节点 SQLite projection；没有 operator rebuild service、多节点数据库或全部业务 projections |
| `dc0f245`、`dd9c4ac` | `projections.py` handler capability/ambiguous transaction | handler transaction capability 线程绑定且返回/异常后撤销；handler 不能控制框架 transaction 或访问 framework tables；BEGIN/ROLLBACK after-success 和双故障释放 lock | `tests/test_projections.py::test_handler_transaction_capability_is_revoked_after_success`、`::test_active_handler_transaction_rejects_cross_thread_use_before_sql`、`::test_handler_cannot_access_or_change_framework_tables`、`::test_begin_after_success_failures_roll_back_release_lock_and_allow_retry`、`::test_rollback_failures_close_connection_release_lock_and_preserve_primary` | 尚缺正式回归覆盖：`set_authorizer` 已安装后 Python 包装器抛异常；deferred VIEW/TRIGGER/virtual-table 执行期越权边界仍需专门审计和测试 |

### 3.4 Tenant authorization 与审批

| 关键提交 | 模块 | 已提交不变量 | 直接测试证据 | 残余边界 |
|---|---|---|---|---|
| `49f3858`、`345be30` | `tenancy.py` typed authority | tenant/workspace/member/role 与 capability chain 强类型；默认拒绝；delegation 只能收窄；audience/time/nonce/revocation 有界且可审计 | `tests/test_tenancy.py::test_scope_attenuation_matrix`、`::test_delegation_rejects_every_privilege_amplification_axis`、`::test_cross_tenant_inactive_and_subject_mismatch_denials`、`::test_collection_admission_limits_fail_closed` | 这是 security slice，不是 public admission service；没有 OIDC/service principal、KMS-backed root、membership sync，且未覆盖每个 repository/tool/connector effect |
| `bd29665` 与 tenancy 后续 hardening | package API、key rotation、SQLite revision guard | public API 固定；trusted root/key usage/algorithm/edge proof 必须匹配；rotation/revocation rollback fail closed；revision guard 跨连接持久化 | `tests/test_tenancy.py::test_tenancy_public_api_is_exported_from_package_root`、`::test_key_rotation_rejects_status_rollback_identity_swap_and_kid_reuse`、`::test_sqlite_revision_guard_serializes_independent_connections`、`::test_authorizer_rejects_rollback_after_sqlite_guard_restart` | 本地签名与 guard 测试不证明生产密钥托管、吊销分发 freshness、break-glass 或跨区域一致性 |
| `620cc4c` | `policy.py` Needs You queue | `ApprovalRequest` 及 authority/intent 的 caller input、返回值和 restore snapshot 与内部状态隔离 | `tests/test_policy.py::test_caller_and_returned_snapshots_cannot_mutate_internal_authority`、`::test_restore_also_detaches_input_and_output_snapshots`；`tests/test_approval_atomicity.py::test_exposed_snapshot_cannot_retarget_live_authority` | queue 仍在进程内，由 event recovery 重建；无独立 approval service、approver 当前权限复验或 UI |
| `2ca54aa` | `runtime.py` approval request path | READY→RUNNING→WAITING_APPROVAL 与 `approval.requested` 以同一 `append_many` batch durable；append 失败前不发布内存 waiting/request | `tests/test_runtime.py::test_failed_approval_request_batch_leaves_no_partial_authority`；`tests/test_recovery.py::test_session_recovery_rejects_incomplete_approval_tail_writes` | `append_many` 若数据库 commit 后调用包装器才抛错，当前同实例没有 exact committed-batch reconciliation，仍可能 durable/in-memory 分叉 |
| `24eb061`、`95e1100`、`9a85e0f` | `runtime.py` approval decision/recovery | decision + WAITING_APPROVAL transition 同 batch；durable append 后才发布 queue/graph/grant；恢复严格绑定 payload、actor、correlation、causation、idempotency、邻接和 task causal chain | `tests/test_approval_atomicity.py::test_failed_decision_batch_never_grants_in_memory_authority`、`::test_concurrent_decision_batches_remain_contiguous`；`tests/test_recovery.py::test_session_recovery_enforces_the_approval_causal_chain`、`::test_session_recovery_rejects_unbound_legacy_decision_correlation` | **仍有生产阻断**：post-commit `EVENT_APPENDED` hook 抛错会把已提交命令表现为调用失败，并阻断 batch 后续 hook；commit-after-wrapper-error 未 reconcile；同 session 并发 batch 的 hook 可不按 durable sequence 到达。现有 561 tests 没有锁住这三种语义 |

### 3.5 Release evidence、distribution 与依赖供应链

| 关键提交 | 模块 | 已提交不变量 | 直接测试证据 | 残余边界 |
|---|---|---|---|---|
| `f8646e6`、`4fd9d8f`、`74d5ae3` | release evidence generator/verifier | dirty、HEAD/tree 漂移、gate 失败/超时/缺工具/零 gate 全部 fail closed；canonical evidence 绑定 expected commit；checkout 内 evidence 被拒绝 | `tests/test_release_evidence.py::test_clean_source_and_passing_gates_are_releasable`、`::test_gate_created_commit_fails_closed_when_worktree_returns_clean`；`tests/test_verify_release_evidence.py::test_source_dirty_unstable_or_mismatched_identity_is_rejected`、`::test_cli_rejects_even_ignored_evidence_inside_repository` | canonical JSON 不是签名 attestation；仍依赖 CI/操作员正确保留和关联 evidence |
| `d0bb6e9`、`c7b4424`、`55f1c5f` | distribution manifest/sdist normalization/reproducibility verifier | wheel/sdist inventory、source bytes、RECORD、metadata、entry point 与 safe archive bounds 可核验；canonical sdist；两个目录的精确制品集合逐字节比较 | `tests/test_distribution_manifest.py::test_valid_wheel_and_sdist_generate_and_verify_source_bound_manifest`、`::test_archive_traversal_and_sdist_links_are_rejected`；`tests/test_normalize_sdist.py::test_different_source_metadata_normalizes_to_identical_bytes`；`tests/test_verify_reproducible_distributions.py::test_content_and_filename_mismatches_fail_closed` | 同 toolchain 可复现不等于跨 runner/编译器/zlib 可复现；没有签名 artifact 或 SLSA provenance |
| `d872df9`、`37e72d1`、`611d7f4` | lock inventory、build/dev/release lock 与 verifier | lock inventory/source digest、root version、binary/hash policy、path/matrix、pyproject root 一致性 fail closed；installer/backend/build/dev/release inputs 均进入锁定集合 | `tests/test_dependency_locks.py::test_repository_inventory_is_valid_and_source_aligned`、`::test_input_and_lock_digest_drift_are_rejected`、`::test_root_version_must_match_the_resolved_lock`、`::test_pyproject_build_and_dev_roots_cannot_drift` | lock 证明输入固定，不证明依赖无漏洞、许可证可接受或下载源长期可用；当前基线没有已提交 SBOM、漏洞/license policy gate |
| `990bb91`、`8508b45`、`86ee4bc` | `.github/workflows/ci.yml`、`package.yml` | 外部 Actions 固定到完整 commit；测试/evidence/build 安装 hash-locked toolchain；distribution 双构建禁用依赖解析后再比较 | `tests/test_ci_action_pins.py::test_every_external_action_is_pinned_to_a_full_commit`；`tests/test_ci_dependency_locks.py::test_test_job_verifies_then_installs_hash_locked_tools`、`::test_both_distribution_builds_disable_dependency_resolution`；`tests/test_ci_package_manifest.py::test_reproducibility_is_verified_from_an_independent_checkout` | workflow 结构测试不证明 GitHub-hosted runner/Action 发布者/基础镜像未被攻破；还缺 SBOM、漏洞/license enforcement、签名 provenance 与 artifact signature |

## 4. 当前阻断项与下一笔证据要求

### P0：不得在修复前宣称 approval command 语义稳定

需要新增并保留正式 regression tests，至少覆盖：

1. decision event 的 hook 抛错后，命令不得把 durable success 伪装成可安全重试的失败；同
   batch 后续 transition hook 仍须有明确投递语义；
2. `append_many` 真实 commit 后包装器抛错时，按 `expected_version` 读取并逐字段、canonical
   JSON 比对 exact batch；只在完整连续匹配时 reconcile 为 committed；
3. 同一 stream 的两个并发 decision batch，其 `EVENT_APPENDED` 可观察顺序必须与 durable
   sequence 一致；
4. retry 与 restart 均不得重复授权、漏授权或漏 projection。

### P0：端到端 effect transaction

下一阶段证据必须把 authenticated admission、action-time authorization、inbox、durable
attempt、runtime/tool effect、receiver acceptance/UNKNOWN、action receipt、artifact 与 task
terminal、outbox 串成可恢复状态机，并在每个边界做 crash injection。缺少 receiver
acceptance 或可查询幂等键时，UNKNOWN 不得自动重试。

### P0：系统级 tenant 与不可信执行边界

在 OIDC/service principal、KMS-backed key registry、membership freshness、每个 repository
和 effect 的授权、进程/容器级文件网络进程隔离、URL/DNS/redirect 防护、secret handle 与
全链 canary scan 落地前，typed tenancy 与 runtime adapter 不能被描述为 SaaS tenant
isolation 或安全沙箱。

### P1：服务、互操作、运维与灾备

需要 versioned service API、readiness/liveness、cursor stream、正式 A2A SDK/TCK、MCP
adapter、OpenTelemetry/SLO、容量与故障测试、加密远端备份、retained restore drill。当前
A2A JSON mapping 单测、本地 backup fixture 和设计文档不能代替这些证据。

### P1：完整供应链

锁文件之后还需提交并验证 SBOM、vulnerability/license policy、签名 provenance/
attestation 与 artifact signature，并保留 clean-host/cross-runner reproducibility matrix。
任何进行中的未提交脚本或测试都不属于本基线能力。

## 5. 复核命令

以下命令可以在实现基线或其仅文档后继上重复核对本台账；测试数量只能引用实际输出：

```bash
git rev-parse 86ee4bc
git rev-parse 86ee4bc^{tree}
git rev-list --count f7be4e2..86ee4bc
git diff --shortstat f7be4e2..86ee4bc
PYTHONPATH=src python3 -m unittest discover -s tests -q
git diff --check
```

完整发布结论仍必须走 `docs/production/LOCAL_RELEASE_EVIDENCE.md` 定义的 clean-source
canonical gates 和严格 verifier；本台账本身不是 release evidence，也不授权真实
Feishu/WeCom 消息发送。
