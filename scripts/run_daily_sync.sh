#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$project_dir/output/logs"
exec >> "$project_dir/output/logs/scheduled.log" 2>&1
printf '\n[%s] Starting scheduled Odoo to Atlas sync\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"

if command -v flock >/dev/null 2>&1; then
    exec 9> "$project_dir/output/scheduled.lock"
    if ! flock -n 9; then
        printf 'A scheduled sync is already running; skipping this run.\n'
        exit 0
    fi
fi

if [ -x "$project_dir/.venv/bin/python" ]; then
    python_bin="$project_dir/.venv/bin/python"
elif [ -x "$project_dir/venv/bin/python" ]; then
    python_bin="$project_dir/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    python_bin=$(command -v python3)
else
    printf 'Python 3 was not found. Create a virtual environment or install python3.\n' >&2
    exit 1
fi

cd "$project_dir"
exec "$python_bin" main.py --unattended
