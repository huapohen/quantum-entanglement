#!/bin/sh
set -eu

web_usage() {
    cat <<'EOF'
用法：./scripts/start_web_client.sh [选项]

启动 Quantum Entanglement v0版 Web IM 本地验收台：Go fake API + React/Vite。
服务只监听 127.0.0.1，不连接飞书、企微或任何真实消息平台，也不会发送消息。

选项：
  --im-port PORT    IM API 端口，默认 18080
  --web-port PORT   Web 页面端口，默认 5173
  --no-install      不自动安装 clients/im-web 的 npm 依赖
  --no-open         不自动打开浏览器
  -h, --help        显示帮助
EOF
}

web_fail() {
    printf '%s\n' "错误：$1" >&2
    exit 2
}

web_validate_port() {
    web_port_name=$1
    web_port_value=$2
    case "$web_port_value" in
        ''|*[!0-9]*) web_fail "$web_port_name 必须是 1 到 65535 的整数" ;;
    esac
    [ "$web_port_value" -ge 1 ] && [ "$web_port_value" -le 65535 ] || \
        web_fail "$web_port_name 必须是 1 到 65535 的整数"
}

web_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
web_project_root=$(CDPATH= cd -- "$web_script_dir/.." && pwd)
web_im_port=18080
web_page_port=5173
web_no_install=0
web_no_open=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --im-port)
            [ "$#" -ge 2 ] || web_fail "--im-port 缺少端口值"
            web_im_port=$2
            shift 2
            ;;
        --im-port=*)
            web_im_port=${1#--im-port=}
            shift
            ;;
        --web-port)
            [ "$#" -ge 2 ] || web_fail "--web-port 缺少端口值"
            web_page_port=$2
            shift 2
            ;;
        --web-port=*)
            web_page_port=${1#--web-port=}
            shift
            ;;
        --no-install)
            web_no_install=1
            shift
            ;;
        --no-open)
            web_no_open=1
            shift
            ;;
        -h|--help)
            web_usage
            exit 0
            ;;
        *)
            web_fail "未知选项：$1"
            ;;
    esac
done

web_validate_port "--im-port" "$web_im_port"
web_validate_port "--web-port" "$web_page_port"

command -v go >/dev/null 2>&1 || web_fail "找不到 Go（需要 Go 运行 IM API）"
command -v npm >/dev/null 2>&1 || web_fail "找不到 npm（需要 Node.js 运行 Web 客户端）"
command -v curl >/dev/null 2>&1 || web_fail "找不到 curl（需要它等待本地 API 就绪）"

web_client_dir=$web_project_root/clients/im-web
[ -f "$web_client_dir/package-lock.json" ] || web_fail "缺少 clients/im-web/package-lock.json"
if [ ! -d "$web_client_dir/node_modules" ]; then
    if [ "$web_no_install" -eq 1 ]; then
        web_fail "未找到 node_modules；去掉 --no-install 让脚本执行 npm ci，或先手动 npm install"
    fi
    printf '%s\n' "首次运行：正在安装 Web 依赖（npm ci --ignore-scripts）..."
    (cd "$web_client_dir" && npm ci --ignore-scripts --no-audit --no-fund)
fi

web_tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/wanwork-im-web.XXXXXX")
web_api_log=$web_tmp_dir/im-api.log
web_api_pid=''

web_cleanup() {
    web_status=$?
    trap - EXIT INT TERM
    if [ -n "$web_api_pid" ] && kill -0 "$web_api_pid" 2>/dev/null; then
        kill "$web_api_pid" 2>/dev/null || true
        wait "$web_api_pid" 2>/dev/null || true
    fi
    if [ "$web_status" -ne 0 ] && [ -s "$web_api_log" ]; then
        printf '%s\n' "--- IM API 日志（启动失败）---" >&2
        sed -n '1,160p' "$web_api_log" >&2 || true
    fi
    rm -rf "$web_tmp_dir"
    exit "$web_status"
}
trap web_cleanup EXIT INT TERM

printf '%s\n' "正在启动零网络 IM API：http://127.0.0.1:$web_im_port"
(
    cd "$web_project_root"
    env WANWORK_IM_LISTEN_ADDRESS="127.0.0.1:$web_im_port" \
        GOTOOLCHAIN="${GOTOOLCHAIN:-local}" \
        GOTELEMETRY="${GOTELEMETRY:-off}" \
        go run ./apps/im-api/cmd/im-api >"$web_api_log" 2>&1
) &
web_api_pid=$!

web_api_ready=0
web_wait=0
while [ "$web_wait" -lt 1800 ]; do
    if curl -fsS --max-time 1 "http://127.0.0.1:$web_im_port/health/live" >/dev/null 2>&1; then
        web_api_ready=1
        break
    fi
    if ! kill -0 "$web_api_pid" 2>/dev/null; then
        break
    fi
    web_wait=$((web_wait + 1))
    if [ "$web_wait" -eq 100 ]; then
        printf '%s\n' "IM API 首次编译可能需要几分钟，仍在等待本机服务就绪..."
    fi
    sleep 0.1
done

if [ "$web_api_ready" -ne 1 ]; then
    web_fail "IM API 未在 180 秒内就绪（端口 ${web_im_port}）；请检查上方日志"
fi

web_url="http://127.0.0.1:$web_page_port"
printf '%s\n' "Web IM 已准备启动：$web_url"
printf '%s\n' "API 代理：/api -> http://127.0.0.1:$web_im_port"
printf '%s\n' "停止全部服务：回到本终端按 Ctrl-C"

if [ "$web_no_open" -eq 0 ] && command -v open >/dev/null 2>&1; then
    open "$web_url" >/dev/null 2>&1 || true
fi

cd "$web_client_dir"
WANWORK_IM_WEB_API_PORT="$web_im_port" \
    npm run dev -- --host 127.0.0.1 --port "$web_page_port"
