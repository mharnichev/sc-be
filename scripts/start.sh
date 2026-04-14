#!/bin/sh
set -eu

echo "Waiting for database..."
until alembic current >/dev/null 2>&1; do
  sleep 1
done

echo "Applying migrations..."
alembic upgrade head

echo "Starting API..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
