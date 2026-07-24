#!/bin/sh
set -eu

echo "Waiting for database..."
until alembic current >/dev/null 2>&1; do
  sleep 1
done

echo "Applying migrations..."
alembic upgrade head

echo "Starting API..."
if [ "${APP_ENV:-local}" = "production" ] || [ "${APP_ENV:-local}" = "staging" ]; then
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${API_WORKERS:-1}"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
