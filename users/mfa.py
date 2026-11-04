"""Ikki bosqichli kirish (2FA): Google Authenticator (TOTP) va zaxira kodlar.

* TOTP RFC 6238 bo'yicha standart kutubxonada yozilgan (HMAC-SHA1, 30 s, 6 raqam) -
  Google Authenticator, Microsoft Authenticator, Aegis va boshqalar bilan mos.
* Kalit bazada Fernet bilan shifrlangan; bir marta ishlatilgan kod qayta qabul qilinmaydi.
* Zaxira kodlar faqat PBKDF2 xeshi ko'rinishida saqlanadi va bir martalik.
* Parol to'g'ri bo'lsa ham tokenlar berilmaydi: 5 daqiqalik imzolangan "mfa_token" qaytadi,
  u bilan ko'pi bilan 5 marta kod kiritish mumkin.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

STEP = 30
DIGITS = 6
WINDOW = 1                   # soat farqi uchun oldingi/keyingi 30 soniya ham qabul qilinadi
BACKUP_COUNT = 10
BACKUP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # adashtiradigan 0/o, 1/l/i yo'q
BACKUP_ITERATIONS = 50_000
MFA_TOKEN_AGE = 300
MFA_MAX_ATTEMPTS = 5
_SALT = "users.mfa.login"


# ---------- TOTP ----------

def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret):
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def totp_at(secret, step):
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10 ** DIGITS).zfill(DIGITS)


def current_step(now=None):
    return int((now if now is not None else time.time()) // STEP)


def otpauth_uri(user, secret):
    issuer = getattr(settings, "MFA_ISSUER", "ShifoSafe")
    label = quote(f"{issuer}:{user.username}", safe=":@")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def _clean(code):
    return "".join(str(code or "").split()).replace("-", "").lower()


def verify_totp(device, code):
    """Kod to'g'ri bo'lsa va shu qadam avval ishlatilmagan bo'lsa True (qadam saqlanadi)."""
    from .models import TwoFactorDevice

    code = _clean(code)
    if len(code) != DIGITS or not code.isdigit():
        return False
    now = current_step()
    for step in range(now - WINDOW, now + WINDOW + 1):
        if hmac.compare_digest(totp_at(device.secret, step), code):
            # Takroriy hujum (replay): ushlab olingan kod ikkinchi marta o'tmasin
            updated = TwoFactorDevice.objects.filter(pk=device.pk, last_step__lt=step).update(last_step=step)
            if updated:
                device.last_step = step
            return bool(updated)
    return False


# ---------- Zaxira kodlar ----------

def _hash_backup(code, salt=None):
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode(), salt.encode(), BACKUP_ITERATIONS).hex()
    return f"{salt}${digest}"


def _check_backup(code, stored):
    salt, _, digest = stored.partition("$")
    return hmac.compare_digest(_hash_backup(code, salt).partition("$")[2], digest)


def new_backup_codes(user):
    """Eski kodlarni o'chirib, 10 ta yangisini yaratadi. Ochiq matn faqat shu javobda ko'rinadi."""
    from .models import BackupCode

    codes = ["".join(secrets.choice(BACKUP_ALPHABET) for _ in range(10)) for _ in range(BACKUP_COUNT)]
    with transaction.atomic():
        BackupCode.objects.filter(user=user).delete()
        BackupCode.objects.bulk_create([BackupCode(user=user, code_hash=_hash_backup(c)) for c in codes])
    return [f"{c[:5]}-{c[5:]}" for c in codes]


def use_backup_code(user, code):
    from .models import BackupCode

    code = _clean(code)
    if len(code) != 10:
        return False
    for bc in BackupCode.objects.filter(user=user, used_at__isnull=True):
        if _check_backup(code, bc.code_hash):
            return BackupCode.objects.filter(pk=bc.pk, used_at__isnull=True).update(used_at=timezone.now()) == 1
    return False


def backup_remaining(user):
    return user.backup_codes.filter(used_at__isnull=True).count()


# ---------- Holat ----------

def required_for(user):
    roles = getattr(settings, "MFA_REQUIRED_ROLES", ())
    return user.role in roles


def enabled_device(user):
    from .models import TwoFactorDevice

    if not user.is_active_2fa:
        return None
    return TwoFactorDevice.objects.filter(user=user, confirmed_at__isnull=False).first()


def verify_any(user, code):
    """Ilova kodi yoki zaxira kod. ('totp' | 'backup' | None)"""
    device = enabled_device(user)
    if device and verify_totp(device, code):
        return "totp"
    if device and use_backup_code(user, code):
        return "backup"
    return None


def disable(user):
    from .models import BackupCode, TwoFactorDevice

    with transaction.atomic():
        TwoFactorDevice.objects.filter(user=user).delete()
        BackupCode.objects.filter(user=user).delete()
        user.is_active_2fa = False
        user.save(update_fields=["is_active_2fa"])


# ---------- Kirishning ikkinchi bosqichi ----------

def issue_login_token(user):
    nonce = secrets.token_urlsafe(16)
    return signing.dumps({"u": user.pk, "n": nonce}, salt=_SALT)


def read_login_token(token):
    """(user_id, nonce) yoki None: muddati o'tgan, soxta, ishlatilgan yoki urinishlar tugagan."""
    try:
        data = signing.loads(token or "", salt=_SALT, max_age=MFA_TOKEN_AGE)
    except signing.BadSignature:
        return None
    nonce = data.get("n")
    if cache.get(f"mfa:done:{nonce}") or (cache.get(f"mfa:tries:{nonce}") or 0) >= MFA_MAX_ATTEMPTS:
        return None
    return data.get("u"), nonce


def register_failed_attempt(nonce):
    key = f"mfa:tries:{nonce}"
    cache.add(key, 0, MFA_TOKEN_AGE)
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, MFA_TOKEN_AGE)
        return 1


def consume_login_token(nonce):
    cache.set(f"mfa:done:{nonce}", 1, MFA_TOKEN_AGE)
