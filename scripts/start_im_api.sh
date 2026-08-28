#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)

if [ -n "${WANWORK_IM_POSTGRES_RUNTIME_URL:-}" ] || \
   [ -n "${WANWORK_IM_POSTGRES_AUTHORITY_MANIFEST:-}" ] || \
   [ -n "${WANWORK_IM_POSTGRES_ALLOW_INSECURE_LOCAL_TEST:-}" ]; then
    if [ "${WANWORK_IM_ALLOW_RUNTIME_COMPOSITION:-}" != "1" ]; then
        echo "runtime PostgreSQL variables are present; set WANWORK_IM_ALLOW_RUNTIME_COMPOSITION=1 only after reviewing the exact manifest" >&2
        exit 2
    fi
fi

cd "$project_root"
exec env GOTOOLCHAIN="${GOTOOLCHAIN:-local}" go run ./apps/im-api/cmd/im-api
