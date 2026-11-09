"""
Login bloklash va register orqali username aniqlashni cheklash (pentest 2026-10-19, 5-band).

Bloklash holati bazada emas, cache'da (Redis) username+IP kaliti bilan saqlanadi:
- mavjud bo'lmagan username ham xuddi mavjud hisob kabi bloklanadi, shuning uchun
  bloklash xabari hisob borligini oshkor qilmaydi;
- boshqa IP'dan kelgan urinishlar hisobni bloklay olmaydi (hisobni bloklash DoS'i).
"""
import hashlib
import logging
import re
import unicodedata
from datetime import timedelta

from django.core.cache import cache
from rest_framework.exceptions import Throttled
from rest_framework.throttling import BaseThrottle
from rest_framework.views import exception_handler as drf_exception_handler

security_logger = logging.getLogger("security")

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)

# NAT ortidagi (universitet, ofis, mobil operator) foydalanuvchilar bloklanmasligi uchun yumshoqroq chegara.
REGISTER_TAKEN_LIMIT = 20
REGISTER_TAKEN_WINDOW = timedelta(hours=1)

# Ommaviy ro'yxatdan o'tishda taqiqlangan nomlar (pentest 2026-10-19, 6-band).
# Administrator panel orqali yaratish cheklanmaydi (xizmat hisoblari uchun).
RESERVED_USERNAMES = frozenset({
    "admin", "administrator", "root", "support", "system", "superuser", "sysadmin", "staff",
    "moderator", "security", "webmaster", "postmaster", "hostmaster", "noreply",
    "medkarta", "medsecure", "doctor", "shifokor",
    "админ", "администратор", "поддержка",
})
# Lotin harfiga o'xshash kirill/yunon harflari - faqat solishtirish uchun, username o'zgartirilmaydi.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "і": "i", "ј": "j", "х": "x", "у": "y",
    "ѕ": "s", "ԁ": "d", "ο": "o", "α": "a", "ι": "i", "ν": "v", "ρ": "p",
})


def is_reserved_username(username):
    """
    'admin', 'ADMIN', 'ａｄｍｉｎ' (to'liq kenglik), 'аdmin' (kirill 'а'), 'ad_min', 'admin1' - hammasi zaxira.
    Prefiks emas: 'adminka', 'staffan', 'rooted' kabi oddiy nomlar ruxsat etiladi.
    """
    base = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", username or "").casefold())
    for candidate in (base, base.translate(_CONFUSABLES)):
        if candidate in RESERVED_USERNAMES or re.sub(r"\d+$", "", candidate) in RESERVED_USERNAMES:
            return True
    return False


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
    from monitoring.stats import bump  # kunlik Telegram langari statistikasi

    fail_key, lock_key = _login_keys(username, ident)
    bump("login_failed")
    if _increment(fail_key, LOCKOUT_DURATION) >= MAX_FAILED_ATTEMPTS:
        cache.set(lock_key, 1, int(LOCKOUT_DURATION.total_seconds()))
        cache.delete(fail_key)
        bump("lockout")
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


def revoke_refresh_tokens(user):
    """Foydalanuvchining amaldagi barcha refresh tokenlarini blacklist qiladi (7-band); sonini qaytaradi."""
    from django.utils import timezone
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    active = OutstandingToken.objects.filter(
        user=user, expires_at__gt=timezone.now(), blacklistedtoken__isnull=True
    )
    active = list(active)
    created = BlacklistedToken.objects.bulk_create(
        [BlacklistedToken(token=token) for token in active], ignore_conflicts=True
    )
    from .sessions import mark_revoked_jtis
    mark_revoked_jtis(t.jti for t in active)   # qo'ldagi access tokenlar ham darhol yaroqsiz
    return len(created)


def mask_email(email):
    """'alisher@example.com' -> 'a***@example.com': logda shaxsiy ma'lumot ochiq turmasin (log bandi)."""
    local, sep, domain = str(email or "").strip().rpartition("@")
    if not sep or not local or not domain:
        return "***"
    # Domen ham foydalanuvchi kiritgan qiymat: yangi qator va boshqa belgilar bilan soxta log yozuvi yasalmasin
    return f"{local[0] if local[0].isalnum() else '?'}***@{re.sub(r'[^A-Za-z0-9.-]', '?', domain)[:100]}"


def logging_exception_handler(exc, context):
    """DRF exception handler: 429 hodisalarini (brute-force belgisi) xavfsizlik logiga yozadi."""
    if isinstance(exc, Throttled):
        request = context.get("request")
        view = context.get("view")
        security_logger.warning(
            "Throttled: scope=%s %s %s (IP: %s, kutish=%ss)",
            getattr(view, "throttle_scope", None),
            getattr(request, "method", "?"),
            getattr(request, "path", "?"),
            client_ident(request) if request is not None else "?",
            int(exc.wait) if exc.wait is not None else "?",
        )
    return drf_exception_handler(exc, context)
