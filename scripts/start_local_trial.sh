#!/bin/sh

set -eu

qe_usage() {
  cat <<'EOF'
用法：scripts/start_local_trial.sh [选项]

启动仅绑定 127.0.0.1 的 Quantum Entanglement 本地产品体验。

选项：
  --port PORT       指定本地端口（默认：8765）
  --no-open         启动服务，但不自动打开浏览器
  --cli             不启动网页，直接运行同一套合成 Agent demo
  -h, --help        显示帮助

环境变量：
  QE_TRIAL_PYTHON   指定 Python 可执行文件（要求 Python 3.9 或更高）

边界：只使用合成数据，不连接飞书、企微或任何外部消息平台。
EOF
}

qe_fail() {
  printf '%s\n' "错误：$1" >&2
  printf '%s\n' "运行 scripts/start_local_trial.sh --help 查看用法。" >&2
  exit 2
}

qe_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
qe_repo_dir=$(CDPATH= cd -- "$qe_script_dir/.." && pwd)
qe_python=${QE_TRIAL_PYTHON:-python3}
qe_port=8765
qe_open=1
qe_cli=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --port)
      [ "$#" -ge 2 ] || qe_fail "--port 缺少端口值"
      qe_port=$2
      shift 2
      ;;
    --port=*)
      qe_port=${1#--port=}
      shift
      ;;
    --no-open)
      qe_open=0
      shift
      ;;
    --cli)
      qe_cli=1
      shift
      ;;
    -h|--help)
      qe_usage
      exit 0
      ;;
    --)
      shift
      [ "$#" -eq 0 ] || qe_fail "不接受位置参数"
      ;;
    *)
      qe_fail "未知选项：$1"
      ;;
  esac
done

command -v "$qe_python" >/dev/null 2>&1 || qe_fail "找不到 Python：$qe_python"

if ! "$qe_python" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
  qe_fail "需要 Python 3.9 或更高版本"
fi

qe_pythonpath=$qe_repo_dir/src
if [ -n "${PYTHONPATH:-}" ]; then
  qe_pythonpath=$qe_pythonpath:$PYTHONPATH
fi

cd -- "$qe_repo_dir"

if [ "$qe_cli" -eq 1 ]; then
  printf '%s\n' "正在运行本地合成 Agent demo（不会连接任何聊天平台）…"
  exec env PYTHONPATH="$qe_pythonpath" "$qe_python" -u examples/group_chat_demo.py
fi

case "$qe_port" in
  ''|*[!0-9]*) qe_fail "端口必须是 1 到 65535 的整数" ;;
esac
if [ "$qe_port" -lt 1 ] || [ "$qe_port" -gt 65535 ]; then
  qe_fail "端口必须是 1 到 65535 的整数"
fi

if [ "$qe_open" -eq 1 ]; then
  exec env PYTHONPATH="$qe_pythonpath" "$qe_python" -u examples/product_trial_server.py \
    --port "$qe_port" --open
fi

exec env PYTHONPATH="$qe_pythonpath" "$qe_python" -u examples/product_trial_server.py \
  --port "$qe_port"
