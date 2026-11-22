#!/bin/sh
set -eu

if [ "${APP_MODE:-local}" != "demo" ]; then
  echo "The hosted startup script requires APP_MODE=demo." >&2
  exit 1
fi

export FLASK_SKIP_DOTENV=1
python -m flask --app pocketview db-migrate
python -m flask --app pocketview seed-demo
exec gunicorn 'pocketview:create_app()' \
  --bind "0.0.0.0:${PORT:-10000}" \
  --workers 1 --threads 4 --timeout 120 \
  --no-control-socket \
  --access-logfile /dev/null --error-logfile -
