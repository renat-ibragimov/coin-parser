#!/bin/bash
# Nightly `--step update-prices`, wrapped for cron. Run by deploy/crontab.
#
# The step runs in its own container on coin_keeper's docker network, so
# postgres is simply the host `postgres`: no container IP to look up, no
# python on the host, no venv to keep installed. The only thing taken
# from the host is coin_keeper's .env, which is where the stack's
# credentials already live -- nothing is copied into this repository.
set -euo pipefail

CHECKOUT="${CHECKOUT:-$(cd "$(dirname "$0")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$HOME/coinkeeper/.env}"

# cron's PATH is bare; docker usually lives in one of these.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

mkdir -p "$HOME/logs"

# Before docker does it as root, which would leave the container's own
# user unable to write the night's HTML into it.
mkdir -p "$CHECKOUT/staging"

echo "=== $(date -Is) update-prices ==="

if [ ! -r "$ENV_FILE" ]; then
    echo "no $ENV_FILE -- nothing to connect with" >&2
    exit 2
fi

# Passing --env-file at all turns OFF compose's own reading of ./.env, so
# deploy/.env has to be named explicitly or its overrides are ignored in
# silence. Second wins, which is the way round we want: coin_keeper's
# credentials first, this project's non-secret overrides (network name,
# uid) on top.
ENV_FILES=(--env-file "$ENV_FILE")
if [ -r "$CHECKOUT/deploy/.env" ]; then
    ENV_FILES+=(--env-file "$CHECKOUT/deploy/.env")
fi

# --rm: one-shot, nothing left behind. --no-TTY: cron has no terminal.
# exec: cron reads THIS script's exit code, and it must be the step's own
# (0 fine / 1 partial / 2 nothing done).
exec docker compose "${ENV_FILES[@]}" \
    -f "$CHECKOUT/deploy/docker-compose.collector.yml" \
    run --rm --no-TTY update-prices
