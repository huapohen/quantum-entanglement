#!/bin/sh
set -eu

im_usage() {
    cat <<'EOF'
用法：./scripts/start_im_demo.sh [--port PORT]

启动 Quantum Entanglement 原生 IM 的零网络本地验收台。
强制使用 synthetic runtime，不读取模型 Key，不连接飞书/企微，也不会向任何人发消息。

选项：
  --port PORT   监听端口，默认 18080
  -h, --help    显示帮助
EOF
}

im_fail() {
    printf '%s\n' "错误：$1" >&2
    exit 2
}

im_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
im_port=18080

while [ "$#" -gt 0 ]; do
    case "$1" in
        --port)
            [ "$#" -ge 2 ] || im_fail "--port 缺少端口值"
            im_port=$2
            shift 2
            ;;
        --port=*)
            im_port=${1#--port=}
            shift
            ;;
        -h|--help)
            im_usage
            exit 0
            ;;
        *)
            im_fail "未知选项：$1"
            ;;
    esac
done

case "$im_port" in
    ''|*[!0-9]*) im_fail "端口必须是 1 到 65535 的整数" ;;
esac
[ "$im_port" -ge 1 ] && [ "$im_port" -le 65535 ] || im_fail "端口必须是 1 到 65535 的整数"

printf '%s\n' "正在启动零网络 IM 验收台：http://127.0.0.1:$im_port/demo/im"
printf '%s\n' "停止服务：回到本终端按 Ctrl-C"

exec env \
    WANWORK_IM_LISTEN_ADDRESS="127.0.0.1:$im_port" \
    WANWORK_IM_AGENT_RUNTIME=synthetic \
    "$im_script_dir/start_im_api.sh"
