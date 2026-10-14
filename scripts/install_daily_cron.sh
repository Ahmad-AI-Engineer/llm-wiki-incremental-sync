#!/usr/bin/env bash
set -euo pipefail

marker='# llm_wiki_odoo_atlas_sync'
if ! command -v crontab >/dev/null 2>&1; then
    printf 'crontab is not installed. Install cron on this Linux machine first.\n' >&2
    exit 1
fi

if ! current_crontab=$(crontab -l 2>&1); then
    if [[ $current_crontab != *'no crontab for'* ]]; then
        printf 'Could not read the current crontab: %s\n' "$current_crontab" >&2
        exit 1
    fi
    current_crontab=''
fi
without_job=$(printf '%s\n' "$current_crontab" | grep -v -F "$marker" || true)

if [[ ${1:-} == '--remove' ]]; then
    printf '%s\n' "$without_job" | crontab -
    printf 'Removed the Odoo to Atlas daily cron job.\n'
    exit 0
fi
if [[ $# -ne 0 ]]; then
    printf 'Usage: bash scripts/install_daily_cron.sh [--remove]\n' >&2
    exit 2
fi
if [[ $(date +%z) != '+0000' ]]; then
    printf 'This installer expects the Linux server to use UTC so 19:00 is midnight in Karachi.\n' >&2
    exit 1
fi

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
runner="$project_dir/scripts/run_daily_sync.sh"
if [[ ! -f $runner ]]; then
    printf 'Missing runner: %s\n' "$runner" >&2
    exit 1
fi

printf -v quoted_runner '%q' "$runner"
quoted_runner=${quoted_runner//%/\\%}
job="0 19 * * * /bin/sh $quoted_runner $marker"
{
    if [[ -n $without_job ]]; then
        printf '%s\n' "$without_job"
    fi
    printf '%s\n' "$job"
} | crontab -

printf 'Installed daily midnight Karachi cron job (19:00 UTC).\n'
printf 'Command: %s\n' "$job"
printf 'Run log: %s\n' "$project_dir/output/logs/scheduled.log"
