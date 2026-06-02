#!/bin/sh
set -e

echo "[api] Starting ChiroFlow API container…"

if [ -z "${DJANGO_SECRET_KEY:-}" ]; then
  echo "[api] ERROR: DJANGO_SECRET_KEY is not set. Add it to apps/api/.env (required when DEBUG=false)."
  exit 1
fi

if [ -n "${DATABASE_URL:-}" ]; then
  case "$DATABASE_URL" in
    *localhost*|*127.0.0.1*)
      echo "[api] ERROR: DATABASE_URL points at localhost. Inside Docker the database host must be 'db'."
      echo "[api] Remove DATABASE_URL from apps/api/.env and let docker-compose.prod.yml set it,"
      echo "[api] or set: postgresql://USER:PASSWORD@db:5432/chiroflow"
      exit 1
      ;;
  esac
fi

echo "[api] Running migrations…"
if ! python manage.py migrate --noinput 2>&1; then
  echo "[api] ERROR: migrate failed (see traceback above). Common fixes:"
  echo "[api]   - Duplicate index: redeploy latest code (0044 uses IF NOT EXISTS) or run: migrate clinic 0044_add_db_indexes --fake"
  echo "[api]   - POSTGRES_PASSWORD in .env must match the password used when the database volume was first created."
  echo "[api]   - POSTGRES_USER in .env must match (compose default: chiroflow_user)."
  echo "[api]   - Remove DATABASE_URL from .env if it points at localhost; compose sets host db."
  exit 1
fi

echo "[api] Collecting static files…"
python manage.py collectstatic --noinput

if [ -n "${DJANGO_ADMIN_USERNAME:-}" ] && [ -n "${DJANGO_ADMIN_PASSWORD:-}" ]; then
  echo "[api] Syncing admin user from env…"
  python manage.py create_admin_from_env
fi

echo "[api] Starting Gunicorn on :8000…"
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120
