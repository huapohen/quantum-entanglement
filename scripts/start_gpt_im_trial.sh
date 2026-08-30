#!/bin/sh
set -eu

# Read the first HTTPS/key pair from a local operator-provided input file and
# keep the secret in the child process environment only. Never print it, write
# it to the repository, or include it in evidence. The supplied 2026 input
# file puts the GPT gateway pair first; DeepSeek entries are ignored.
usage() {
  cat <<'EOF'
用法：./scripts/start_gpt_im_trial.sh --input-file FILE [Web 启动参数]

从本机文件读取第一组 HTTPS endpoint 和第一条 sk- Key，启动显式 GPT Web IM runtime。
Key 只存在于子进程环境，不会写入仓库、日志、截图或 Notion。
EOF
}

fail() { printf '%s\n' "错误：$1" >&2; exit 2; }
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
input_file=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --input-file)
      [ "$#" -ge 2 ] || fail "--input-file 缺少路径"
      input_file=$2
      shift 2
      ;;
    --input-file=*) input_file=${1#--input-file=}; shift ;;
    -h|--help) usage; exit 0 ;;
    *) break ;;
  esac
done
[ -n "$input_file" ] || fail "必须提供 --input-file（不会自动猜测凭据文件）"
[ -f "$input_file" ] || fail "凭据输入文件不存在"

base_url=$(awk '/^[[:space:]]*https:\/\// { gsub(/[[:space:]]+/, ""); print; exit }' "$input_file")
api_key=$(awk '/^[[:space:]]*sk-[A-Za-z0-9_-]+[[:space:]]*$/ { gsub(/[[:space:]]+/, ""); print; exit }' "$input_file")
[ -n "$base_url" ] || fail "输入文件没有 HTTPS endpoint"
[ -n "$api_key" ] || fail "输入文件没有 sk- Key"

exec env WANWORK_IM_AGENT_RUNTIME=openai-compatible \
  WANWORK_IM_MODEL_API_KEY="$api_key" \
  WANWORK_IM_MODEL_BASE_URL="$base_url" \
  WANWORK_IM_MODEL=gpt-5.6-sol \
  "$script_dir/start_web_client.sh" --model-runtime openai-compatible "$@"
