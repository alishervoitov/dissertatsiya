#!/bin/sh
set -e

echo "PostgreSQL kutilmoqda..."
while ! python -c "
import socket, os, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1)
try:
    s.connect((os.environ.get('DB_HOST', 'db'), int(os.environ.get('DB_PORT', 5432))))
    s.close()
except Exception:
    sys.exit(1)
"; do
  sleep 1
done
echo "PostgreSQL tayyor."

python manage.py migrate --noinput
python manage.py collectstatic --noinput --clear

exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3