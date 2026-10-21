"""
Login bloklash va register orqali username aniqlashni cheklash (pentest 2026-10-19, 5-band).

Bloklash holati bazada emas, cache'da (Redis) username+IP kaliti bilan saqlanadi:
- mavjud bo'lmagan username ham xuddi mavjud hisob kabi bloklanadi, shuning uchun
  bloklash xabari hisob borligini oshkor qilmaydi;
- boshqa IP'dan kelgan urinishlar hisobni bloklay olmaydi (hisobni bloklash DoS'i).
"""
import hashlib
from datetime import timedelta

from django.core.cache import cache
from rest_framework.throttling import BaseThrottle

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)

# NAT ortidagi (universitet, ofis, mobil operator) foydalanuvchilar bloklanmasligi uchun yumshoqroq chegara.
REGISTER_TAKEN_LIMIT = 20
REGISTER_TAKEN_WINDOW = timedelta(hours=1)


def client_ident(request):
    """Mijoz IP'si - DRF throttle bilan bir xil manba (nginx o'rnatgan X-Forwarded-For)."""
    return str(BaseThrottle().get_ident(request) or "")


def _digest(*parts):
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _increment(key, window):
    """Hisoblagichni oshiradi; Redis ishlamasa (IGNORE_EXCEPTIONS) 0 qaytaradi."""
    timeout = int(window.total_seconds())
    if cache.add(key, 1, timeout):
        return 1
    try:
        return cache.incr(key) or 0
    except ValueError:  # kalit add va incr orasida muddati tugagan
        cache.set(key, 1, timeout)
        return 1


def _login_keys(username, ident):
    digest = _digest((username or "").lower(), ident)
    return f"login:fail:{digest}", f"login:lock:{digest}"


def is_login_locked(username, ident):
    return cache.get(_login_keys(username, ident)[1]) is not None


def register_login_failure(username, ident):
    """Muvaffaqiyatsiz urinishni hisoblaydi; chegaraga yetganda username+IP bloklanadi."""
    fail_key, lock_key = _login_keys(username, ident)
    if _increment(fail_key, LOCKOUT_DURATION) >= MAX_FAILED_ATTEMPTS:
        cache.set(lock_key, 1, int(LOCKOUT_DURATION.total_seconds()))
        cache.delete(fail_key)
        return True
    return False


def clear_login_failures(username, ident):
    cache.delete_many(list(_login_keys(username, ident)))


def _register_taken_key(ident):
    return f"register:taken:{_digest(ident)}"


def register_taken_exceeded(ident):
    return (cache.get(_register_taken_key(ident)) or 0) >= REGISTER_TAKEN_LIMIT


def record_register_taken(ident):
    return _increment(_register_taken_key(ident), REGISTER_TAKEN_WINDOW)
