#!/bin/bash
# `--step rates`, wrapped for cron. Run by deploy/crontab. Same shape as
# run-update-prices.sh -- see that file for why each piece is here.
set -euo pipefail

CHECKOUT="${CHECKOUT:-$(cd "$(dirname "$0")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$HOME/coinkeeper/.env}"

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

mkdir -p "$HOME/logs"

# The rates step keeps no staging tree of its own, but the shared
# x-collector anchor mounts ../staging into every service regardless --
# created here, before docker does it as root.
mkdir -p "$CHECKOUT/staging"

echo "=== $(date -Is) update-rates ==="

if [ ! -r "$ENV_FILE" ]; then
    echo "no $ENV_FILE -- nothing to connect with" >&2
    exit 2
fi

ENV_FILES=(--env-file "$ENV_FILE")
if [ -r "$CHECKOUT/deploy/.env" ]; then
    ENV_FILES+=(--env-file "$CHECKOUT/deploy/.env")
fi

exec docker compose "${ENV_FILES[@]}" \
    -f "$CHECKOUT/deploy/docker-compose.collector.yml" \
    run --rm --no-TTY update-rates
