#!/bin/sh
set -eu

verify_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
verify_root=$(CDPATH= cd -- "$verify_script_dir/.." && pwd)
verify_port=${WANWORK_IM_VERIFY_PORT:-18081}
verify_tmp=$(mktemp -d "${TMPDIR:-/tmp}/wanwork-im-verify.XXXXXX")
verify_log=$verify_tmp/im-api.log
verify_pid=''

verify_cleanup() {
    verify_status=$?
    trap - EXIT INT TERM
    if [ -n "$verify_pid" ] && kill -0 "$verify_pid" 2>/dev/null; then
        kill "$verify_pid" 2>/dev/null || true
        wait "$verify_pid" 2>/dev/null || true
    fi
    if [ "$verify_status" -ne 0 ] && [ -s "$verify_log" ]; then
        printf '%s\n' "--- IM API 日志（验证失败）---" >&2
        sed -n '1,160p' "$verify_log" >&2 || true
    fi
    rm -rf "$verify_tmp"
    exit "$verify_status"
}
trap verify_cleanup EXIT INT TERM

case "$verify_port" in
    ''|*[!0-9]*) printf '%s\n' "错误：WANWORK_IM_VERIFY_PORT 必须是数字" >&2; exit 2 ;;
esac
[ "$verify_port" -ge 1 ] && [ "$verify_port" -le 65535 ] || {
    printf '%s\n' "错误：验证端口必须是 1 到 65535" >&2
    exit 2
}

printf '%s\n' "[1/4] 构建 Web 客户端"
(cd "$verify_root/clients/im-web" && npm run build)

printf '%s\n' "[2/4] 启动 synthetic loopback API（端口 ${verify_port}）"
(
    cd "$verify_root"
    env WANWORK_IM_LISTEN_ADDRESS="127.0.0.1:$verify_port" \
        WANWORK_IM_AGENT_RUNTIME=synthetic \
        GOTOOLCHAIN="${GOTOOLCHAIN:-local}" \
        GOTELEMETRY="${GOTELEMETRY:-off}" \
        go run ./apps/im-api/cmd/im-api >"$verify_log" 2>&1
) &
verify_pid=$!

verify_ready=0
verify_wait=0
while [ "$verify_wait" -lt 1800 ]; do
    if curl -fsS --max-time 1 "http://127.0.0.1:$verify_port/health/live" >/dev/null 2>&1; then
        verify_ready=1
        break
    fi
    if ! kill -0 "$verify_pid" 2>/dev/null; then
        break
    fi
    verify_wait=$((verify_wait + 1))
    sleep 0.1
done
[ "$verify_ready" -eq 1 ] || {
    printf '%s\n' "错误：IM API 未在 180 秒内就绪" >&2
    exit 1
}

printf '%s\n' "[3/4] 检查 HTTP 200 envelope、Agent Store 和 synthetic 零网络"
verify_snapshot=$(curl -fsS -H 'Authorization: Bearer demo.local.signature' \
    "http://127.0.0.1:$verify_port/api/v1/demo/im")
VERIFY_SNAPSHOT="$verify_snapshot" python3 -c '
import json, os, sys
payload = json.loads(os.environ["VERIFY_SNAPSHOT"])
assert payload["code"] == 200
data = payload["data"]
assert data["mode"] == "zero-network-fake"
assert data["networkCalls"] == 0
assert data["agentRuntime"]["mode"] == "synthetic"
' 
verify_agents=$(curl -fsS -H 'Authorization: Bearer demo.local.signature' \
    "http://127.0.0.1:$verify_port/api/v1/demo/im/agents")
VERIFY_AGENTS="$verify_agents" python3 -c '
import json, os
payload = json.loads(os.environ["VERIFY_AGENTS"])
assert payload["code"] == 200
agents = payload["data"]["agents"]
assert any(item["installationStatus"] == "active" for item in agents)
assert any(item["definitionId"] == "agd_local_planner" and item["installationStatus"] == "available" and item["canInstall"] for item in agents)
'
verify_install=$(curl -fsS -H 'Authorization: Bearer demo.local.signature' -H 'Content-Type: application/json' \
    --data '{"idempotencyKey":"verify/web-first/agent-install"}' \
    "http://127.0.0.1:$verify_port/api/v1/demo/im/agents/agd_local_planner/install")
VERIFY_INSTALL="$verify_install" python3 -c '
import json, os
payload = json.loads(os.environ["VERIFY_INSTALL"])
assert payload["code"] == 200
assert payload["data"]["agent"]["agentActorId"] == "agt_local_planner"
assert payload["data"]["agent"]["installationStatus"] == "active"
assert payload["data"]["replayed"] is False
'
verify_install_replay=$(curl -fsS -H 'Authorization: Bearer demo.local.signature' -H 'Content-Type: application/json' \
    --data '{"idempotencyKey":"verify/web-first/agent-install"}' \
    "http://127.0.0.1:$verify_port/api/v1/demo/im/agents/agd_local_planner/install")
VERIFY_INSTALL_REPLAY="$verify_install_replay" python3 -c '
import json, os
payload = json.loads(os.environ["VERIFY_INSTALL_REPLAY"])
assert payload["code"] == 200
assert payload["data"]["replayed"] is True
'

printf '%s\n' "[4/4] 用动态指令验证 Agent 子群隔离和业务错误封装"
verify_result=$(curl -fsS -H 'Authorization: Bearer demo.local.signature' \
    -H 'Content-Type: application/json' \
    --data '{"conversationId":"cnv_local_demo_parent","messageId":"msg_verify_web_first","instruction":"验证 Web-first 动态指令闭环"}' \
    "http://127.0.0.1:$verify_port/api/v1/demo/im/mentions")
VERIFY_RESULT="$verify_result" python3 -c '
import json, os
payload = json.loads(os.environ["VERIFY_RESULT"])
assert payload["code"] == 200
data = payload["data"]
assert data["parentConversationId"] == "cnv_local_demo_parent"
assert data["childConversationId"] != data["parentConversationId"]
assert data["agentReply"]["conversationId"] == data["childConversationId"]
assert data["providerStatus"] == "committed"
assert data["taskId"].startswith("task_local_")
assert data["artifactId"].startswith("artifact_local_")
assert data["needsYouId"].startswith("needs_local_")
'

verify_task_id=$(VERIFY_RESULT="$verify_result" python3 -c 'import json, os; print(json.loads(os.environ["VERIFY_RESULT"])["data"]["taskId"])')
verify_artifact_id=$(VERIFY_RESULT="$verify_result" python3 -c 'import json, os; print(json.loads(os.environ["VERIFY_RESULT"])["data"]["artifactId"])')
verify_needs_id=$(VERIFY_RESULT="$verify_result" python3 -c 'import json, os; print(json.loads(os.environ["VERIFY_RESULT"])["data"]["needsYouId"])')
verify_workboard_tasks=$(curl -fsS -H 'Authorization: Bearer demo.local.signature' "http://127.0.0.1:$verify_port/api/v1/demo/im/tasks")
verify_workboard_artifacts=$(curl -fsS -H 'Authorization: Bearer demo.local.signature' "http://127.0.0.1:$verify_port/api/v1/demo/im/artifacts")
verify_workboard_needs=$(curl -fsS -H 'Authorization: Bearer demo.local.signature' "http://127.0.0.1:$verify_port/api/v1/demo/im/needs-you")
VERIFY_TASKS="$verify_workboard_tasks" VERIFY_ARTIFACTS="$verify_workboard_artifacts" VERIFY_NEEDS="$verify_workboard_needs" \
VERIFY_TASK_ID="$verify_task_id" VERIFY_ARTIFACT_ID="$verify_artifact_id" VERIFY_NEEDS_ID="$verify_needs_id" python3 -c '
import json, os
tasks=json.loads(os.environ["VERIFY_TASKS"]); artifacts=json.loads(os.environ["VERIFY_ARTIFACTS"]); needs=json.loads(os.environ["VERIFY_NEEDS"])
assert tasks["code"] == artifacts["code"] == needs["code"] == 200
assert any(item["id"] == os.environ["VERIFY_TASK_ID"] and item["status"] == "waiting_for_review" for item in tasks["data"]["tasks"])
assert any(item["id"] == os.environ["VERIFY_ARTIFACT_ID"] and item["status"] == "draft" for item in artifacts["data"]["artifacts"])
assert any(item["id"] == os.environ["VERIFY_NEEDS_ID"] and item["status"] == "open" for item in needs["data"]["needsYou"])
'
verify_resolved=$(curl -fsS -H 'Authorization: Bearer demo.local.signature' -H 'Content-Type: application/json' \
    --data '{"decision":"accept"}' "http://127.0.0.1:$verify_port/api/v1/demo/im/needs-you/$verify_needs_id/resolve")
VERIFY_RESOLVED="$verify_resolved" python3 -c '
import json, os
data=json.loads(os.environ["VERIFY_RESOLVED"])
assert data["code"] == 200
assert data["data"]["artifact"]["status"] == "accepted"
assert data["data"]["task"]["status"] == "completed"
assert data["data"]["needsYou"]["status"] == "resolved"
'

printf '%s\n' "Web-first synthetic 验证通过（构建、envelope、Agent Store、子群隔离、Workboard 审阅闭环）"
