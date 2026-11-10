"""
Emailni tasdiqlash (onlayn ro'yxatdan o'tgan bemorlar uchun).

Nega kerak: email majburiy, lekin tekshirilmasa foydalanuvchi birovning emailini yozishi mumkin -
keyin parolni tiklash xati o'sha odamga ketadi. Shuning uchun parolni tiklash faqat tasdiqlangan
emailga ishlaydi (users/tasks.py).

Kod:
- 6 raqam, 15 daqiqa amal qiladi, bir martalik;
- keshda (Redis) faqat HMAC xeshi saqlanadi va u yuborilgan emailga bog'langan: email o'zgarsa,
  eski kod ishlamaydi;
- bitta kodga ko'pi bilan 5 urinish, keyin kod bekor bo'ladi;
- qayta yuborish: 60 soniyada bir marta va kuniga 5 tadan ko'p emas (pochta qutisini xat bilan
  to'ldirish va SMTP limitini sarflashning oldini olish uchun).
Kodning o'zi jurnalga yozilmaydi.
"""
import hashlib
import hmac
import logging
import secrets
import time

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .security import mask_email

security_logger = logging.getLogger("security")

CODE_TTL = 15 * 60
MAX_ATTEMPTS = 5
RESEND_COOLDOWN = 60
DAILY_LIMIT = 5

# Natija kodlari (frontend shu kalitlar bo'yicha tarjima qiladi)
OK, NO_CODE, INVALID, TOO_MANY, COOLDOWN, DAILY, NO_EMAIL, ALREADY = (
    "ok", "no_code", "invalid", "too_many", "cooldown", "daily_limit", "no_email", "already_verified")


def _key(user_id):
    return f"emailv:{user_id}"


def _digest(user_id, email, code):
    msg = f"{user_id}:{(email or '').lower()}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def send_code(user):
    """Yangi kod yaratib, fon vazifasi orqali yuboradi. Natija kodini qaytaradi."""
    if not user.email:
        return NO_EMAIL
    if user.email_verified:
        return ALREADY
    if not cache.add(f"emailv:cd:{user.pk}", 1, RESEND_COOLDOWN):
        return COOLDOWN
    day_key = f"emailv:day:{user.pk}:{timezone.localdate().isoformat()}"
    cache.add(day_key, 0, 86400)
    try:
        sent_today = cache.incr(day_key)
    except ValueError:
        sent_today = 1
    if sent_today > DAILY_LIMIT:
        security_logger.warning("EMAIL-VERIFY kunlik limit: %s", user.username)
        return DAILY

    code = f"{secrets.randbelow(10 ** 6):06d}"
    cache.set(_key(user.pk), {"h": _digest(user.pk, user.email, code), "n": 0,
                              "exp": time.time() + CODE_TTL}, CODE_TTL)
    from .tasks import send_verification_email

    email, name = user.email, user.get_full_name() or user.username

    def enqueue():
        try:
            send_verification_email.delay(email, name, code)
        except Exception:  # noqa: BLE001 - Celery/Redis ishlamasa so'rov buzilmasin
            security_logger.exception("EMAIL-VERIFY xat navbatga qo'yilmadi")

    transaction.on_commit(enqueue)
    security_logger.info("EMAIL-VERIFY kod yuborildi: %s -> %s", user.username, mask_email(user.email))
    return OK


def verify_code(user, code):
    """Kodni tekshiradi; to'g'ri bo'lsa emailni tasdiqlangan deb belgilaydi."""
    if user.email_verified:
        return ALREADY
    data = cache.get(_key(user.pk))
    if not data or data.get("exp", 0) < time.time():
        return NO_CODE
    if data["n"] >= MAX_ATTEMPTS:
        cache.delete(_key(user.pk))
        return TOO_MANY
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != 6 or not hmac.compare_digest(data["h"], _digest(user.pk, user.email, code)):
        data["n"] += 1
        if data["n"] >= MAX_ATTEMPTS:
            cache.delete(_key(user.pk))
            security_logger.warning("EMAIL-VERIFY urinishlar tugadi: %s", user.username)
            return TOO_MANY
        cache.set(_key(user.pk), data, max(1, int(data["exp"] - time.time())))
        return INVALID
    cache.delete(_key(user.pk))
    user.email_verified = True
    user.save(update_fields=["email_verified"])
    security_logger.info("EMAIL-VERIFY tasdiqlandi: %s", user.username)
    return OK


def reset_pending(user_id):
    """Email o'zgarganda kutilayotgan kod bekor qilinadi (u eski emailga bog'langan)."""
    cache.delete(_key(user_id))
