#!/bin/sh
set -eu

case "${APP_ENV:-local}" in
  production|staging) default_mode=check ;;
  *) default_mode=apply ;;
esac
mode=${MIGRATIONS_MODE:-$default_mode}
case "$mode" in
  check|apply) ;;
  *) echo 'MIGRATIONS_MODE must be check or apply' >&2; exit 1 ;;
esac
attempts=${DATABASE_WAIT_ATTEMPTS:-30}
case "$attempts" in
  ''|*[!0-9]*|0*) echo 'DATABASE_WAIT_ATTEMPTS must be a positive integer' >&2; exit 1 ;;
esac
if [ "${#attempts}" -gt 3 ] || [ "$attempts" -gt 300 ]; then
  echo 'DATABASE_WAIT_ATTEMPTS must be at most 300' >&2
  exit 1
fi

trusted_proxy_ips=${TRUSTED_PROXY_IPS:-127.0.0.1}
export TRUSTED_PROXY_IPS="$trusted_proxy_ips"
python -m app.core.proxy_trust

echo 'Checking database schema...'
while :; do
  if [ "$mode" = check ]; then
    if python -m app.core.migration_readiness; then break; fi
  else
    if PGCONNECT_TIMEOUT=5 PGOPTIONS="-c statement_timeout=5000 -c lock_timeout=5000" alembic current >/dev/null 2>&1; then break; fi
  fi
  attempts=$((attempts - 1))
  if [ "$attempts" -le 0 ]; then
    echo 'Database readiness failed; API will not start' >&2
    exit 1
  fi
  sleep 1
done

if [ "$mode" = apply ]; then
  echo 'Applying migrations (this process is the migration owner)...'
  alembic upgrade head
fi

echo 'Starting API...'
case "${APP_ENV:-local}" in
  production|staging)
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "$trusted_proxy_ips" --workers "${API_WORKERS:-1}" ;;
esac
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "$trusted_proxy_ips" --reload
