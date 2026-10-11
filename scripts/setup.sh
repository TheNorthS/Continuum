#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
test -f .env || cp .env.example .env
cd backend
uv sync --extra dev
uv run python -m app.cli init
uv run python -m app.cli ingest
cd ../frontend
npm ci
