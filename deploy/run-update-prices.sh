#!/bin/bash
# Nightly `--step update-prices`, wrapped for cron. Run by deploy/crontab.
#
# The step runs in its own container on coin_keeper's docker network, so
# postgres is simply the host `postgres` -- no looking up a container IP
# that docker reassigns on every recreate, no second python on the box,
# no venv to keep installed. The one thing this needs from the host is
# coin_keeper's .env, which is where the stack's credentials already
# live; nothing is copied into this repository.
set -euo pipefail

CHECKOUT="${CHECKOUT:-$HOME/coin-parser}"
ENV_FILE="${ENV_FILE:-$HOME/coinkeeper/.env}"
COMPOSE_FILE="$CHECKOUT/deploy/docker-compose.collector.yml"

# cron's PATH is bare; docker usually lives in one of these.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

mkdir -p "$HOME/logs"

echo "=== $(date -Is) update-prices ==="

if [ ! -r "$ENV_FILE" ]; then
    echo "no $ENV_FILE -- nothing to connect with" >&2
    exit 2
fi

# --rm: one-shot task, no container left behind. --no-TTY: cron has no
# terminal. exec: cron reads THIS script's exit code, and it has to be
# the step's own (0 fine / 1 partial / 2 nothing done).
exec docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" \
    run --rm --no-TTY update-prices
