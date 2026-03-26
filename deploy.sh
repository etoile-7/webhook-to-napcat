#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "[error] docker not found"
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose)
else
  echo "[error] neither 'docker compose' nor 'docker-compose' is available"
  exit 1
fi

ENV_FILE="$ROOT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT_DIR/.env.example" "$ENV_FILE"
  echo "[info] created .env from .env.example"
  echo "[info] please edit .env before first production use"
fi

echo "[info] building and starting webhook-to-napcat..."
"${COMPOSE_CMD[@]}" --env-file "$ENV_FILE" up -d --build

echo "[info] current status:"
"${COMPOSE_CMD[@]}" ps

echo "[info] health check hint: curl http://127.0.0.1:8787/health"
