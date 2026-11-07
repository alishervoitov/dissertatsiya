"""
Testlar uchun sozlamalar: prod Postgres, Redis va media volume'ga tegmaydi.
Ishlatish: python manage.py test users --settings=config.settings_test
"""
import os
import tempfile
from pathlib import Path

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# CI'da (GitHub Actions) testlar haqiqiy PostgreSQL'da ham yuradi: audit jurnali triggeri faqat
# Postgres'da bor, SQLite uni tekshira olmaydi. Mahalliy ishga tushirishda - SQLite (tez).
if os.environ.get("CI_POSTGRES") == "1":
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "shifosafe_ci"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

MEDIA_ROOT = tempfile.mkdtemp(prefix="medkarta-test-media-")

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CELERY_TASK_ALWAYS_EAGER = True

# Xavfsizlik log fayli vaqtinchalik papkaga (prod logs_data volume'iga tegilmaydi)
LOG_DIR = Path(tempfile.mkdtemp(prefix="medkarta-test-logs-"))
LOGGING["handlers"]["security_file"]["filename"] = str(LOG_DIR / "security.log")
