#!/bin/sh
set -eu

ROOT=/home/deploy/coin-parser
mkdir -p "$ROOT/staging"
exec docker compose --env-file /home/deploy/coinkeeper/.env \
  -f "$ROOT/deploy/docker-compose.collector.yml" run --rm update-catalog
