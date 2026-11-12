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

# 3 jarayon x 4 oqim = bir vaqtda 12 ta so'rov. Avval 3 ta edi: bitta sekin so'rov (PDF, parol xeshi,
# fayl yuklash) butun jarayonni band qilib, qolganlar navbatda kutardi. Oqimlar xotirani deyarli oshirmaydi.
# Sonlarni .env.docker orqali o'zgartirish mumkin (GUNICORN_WORKERS, GUNICORN_THREADS).
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 \
    --worker-class gthread --workers "${GUNICORN_WORKERS:-3}" --threads "${GUNICORN_THREADS:-4}"