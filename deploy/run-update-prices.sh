#!/bin/bash
# Nightly `--step update-prices`, wrapped for cron. Run by deploy/crontab.
#
# Everything unusual here is about postgres not being reachable the
# ordinary way: its port is published nowhere -- not on the host, not on
# loopback -- so the only address it has is its IP inside the docker
# network, and docker reassigns that whenever the network is recreated
# (`docker compose down` and back up). Hence: look the IP up at RUN time,
# every time. A remembered one is wrong the first morning after a
# redeploy. See docs/03_series_playbook.md.
set -euo pipefail

CHECKOUT="${CHECKOUT:-$HOME/coin-parser}"
COMPOSE="${COMPOSE:-$HOME/coinkeeper/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-$HOME/coinkeeper/.env}"

mkdir -p "$HOME/logs"

echo "=== $(date -Is) update-prices ==="

PGIP=$(docker inspect -f \
    '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
    "$(docker compose -f "$COMPOSE" ps -q postgres)")
if [ -z "$PGIP" ]; then
    echo "postgres container has no IP -- is the stack up? (docker compose ps)" >&2
    exit 2
fi

export PGHOST="$PGIP"
export PGPORT=5432
export PGUSER="${PGUSER:-coinkeeper}"
# The password stays out of DATABASE_URL, out of the process list and out
# of this log: libpq picks it up from PGPASSWORD, and the connection
# string names only the database.
if ! PGPASSWORD=$(grep -m1 '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2-) \
   || [ -z "$PGPASSWORD" ]; then
    echo "no POSTGRES_PASSWORD in $ENV_FILE -- nothing to connect with" >&2
    exit 2
fi
export PGPASSWORD
export DATABASE_URL="postgresql:///coinkeeper"

cd "$CHECKOUT"
# shellcheck disable=SC1091
source .venv/bin/activate
# exec: cron reads the exit code of this script, and it must be the
# step's own (0 fine / 1 partial / 2 nothing done), not the shell's.
exec python -m collector ua --step update-prices
