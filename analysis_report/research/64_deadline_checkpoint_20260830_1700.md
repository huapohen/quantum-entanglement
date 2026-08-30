# 2026-08-30 17:00 军令截止点交付检查表

## 精确版本

- 主线评审分支：`mainline_continue_quantum_entanglement`
- 截止点起草 HEAD：`450fcf4 docs(im): record latest web-first acceptance`
- 远端：`origin/mainline_continue_quantum_entanglement`
- `main`：未自动合并
- 外部发送：未启动飞书、企微、真实 IM、机器人或 webhook

## 本时段已经形成的可验证增量

1. 回归门禁按变更路径选择最小充分集合，2,975 项仅在阶段封板执行；
2. 修复 Web build 在错误目录寻找 `package.json` 的缺陷，并用 selector test 锁定；
3. 认证 conversation event read 已装配 fail-closed contract；
4. `message.created/edited/recalled` strict reducer 已完成；
5. 认证 message read route 已完成 ACL、scope、revision、cursor/page integrity 检查；
6. PostgreSQL production composition 已注入 durable EventStore；
7. bounded event-replay message reader 已注入 production composition；
8. message cursor 绑定 tenant/workspace/conversation/exact stream version，stream drift 拒绝；
9. 重放最多 4,096 事件，拒绝无界扫描；
10. Web-first 动态指令、Agent 子群、Task/Artifact/Needs You 审阅闭环仍通过。

## 截止点验证

阶段封板基线 `a3889e2`：

```text
pytest full (2,975 inventory)  pass
Ruff                             pass
strict mypy                      pass
compileall                       pass
Go test/vet                      pass
Web build                        pass
Web-first synthetic              pass
regression_gate=passed
```

之后的 Go-only durable replay 增量：

```text
go test ./...                         pass
go vet ./...                          pass
EventReplayMessageReader focused      pass
./scripts/verify_web_first.sh          pass
git diff --check                       pass
```

## 可以验收什么

- 本地启动、Web 工作台和动态自定义指令；
- synthetic 零网络 Agent 回复；
- Agent 子群隔离；
- Task、Artifact、Needs You 生成和接受闭环；
- PostgreSQL runtime 的 health/readiness 与 reject-all auth fail-closed composition；
- 已认证 conversation/event/message HTTP contract（测试 composition）；
- durable EventStore-backed message replay reader 的分页和 stream-drift 行为。

## 不能声称已经完成什么

以下项目没有证据证明完成，因此仍保持关闭：

1. PostgreSQL materialized message heads/snapshots；
2. projection write 与 checkpoint 同事务；
3. authority 与 event/projection 同一数据库快照；
4. real Clerk/JWKS、rotation、revocation 和 production session；
5. Task/Attempt/Artifact/Needs You 的完整 PostgreSQL durable projection；
6. production worker dispatch 和 QE invocation bridge；
7. effect_unknown action receipt/reconcile；
8. 真实 IM provider contract/exchange；
9. 全系统 SIGKILL、双进程、restore、rollback compatibility；
10. Gate A–E 发布批准。

## 下一执行顺序

1. migration：message heads/snapshots + exact RLS/access manifest；
2. projector：event apply、message row CAS、checkpoint 同事务；
3. read cutover：replay bridge 与 materialized reader 双读比对，再切换；
4. Task/Attempt/Artifact/Needs You durable projection；
5. worker bridge、Action receipt/reconcile；
6. Clerk/JWKS 与 native IM provider sandbox；
7. crash/restore/rollback/compatibility；
8. Gate A–E 逐项封板。

该文档是精确截止点，不是生产 GA 证书。后续每个代码节点继续使用 focused gate，跨阶段才再次
运行全量。
