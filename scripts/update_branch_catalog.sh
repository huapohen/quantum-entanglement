#!/bin/sh
set -eu

qe_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$qe_script_dir/branch_catalog.py" "$@"
