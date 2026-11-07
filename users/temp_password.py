"""
Registraturada ochilgan (yoki parolini unutgan) bemorga vaqtinchalik parol.

Oqim:
  1. Registrator (administrator) bemor kartasida "Kirish ma'lumotlari" ni bosadi -> server tasodifiy
     parol yaratadi, faqat xeshini saqlaydi va ochiq matnni BIR MARTA javobda qaytaradi
     (registrator qog'ozga chop etib bemorga beradi).
  2. Bemor shu parol bilan kiradi -> token o'rniga password_change_required + bir martalik pw_token.
  3. Bemor yangi parol o'rnatadi (Django parol validatorlari) -> shundan keyingina sessiya ochiladi.

Himoya:
  * faqat bemor rolidagi hisoblar uchun - administrator shifokor/admin hisobini shu yo'l bilan egallay olmaydi;
  * parol TEMP_PASSWORD_HOURS soatdan keyin ishlamaydi;
  * yangi parol berilganda bemorning barcha sessiyalari yopiladi;
  * vaqtinchalik parol bilan hech qanday API'ga kirib bo'lmaydi (token berilmaydi);
  * 2FA yoqilgan bemorda 2FA bosqichi baribir so'raladi;
  * har bir berilish auditga yoziladi (parolning o'zi emas).
"""
import secrets
from datetime import timedelta

from django.core import signing
from django.core.cache import cache
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

TEMP_PASSWORD_HOURS = 72
PW_TOKEN_AGE = 10 * 60
_SALT = "users.temp_password.change"
# Qog'ozdan o'qish oson bo'lsin: 0/O, 1/I/L kabi chalkash belgilar yo'q
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate():
    """XXXX-XXXX-XXXX: 12 belgi, ~59 bit entropiya."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def issue(user):
    """Yangi vaqtinchalik parol o'rnatadi; ochiq matn va muddatini qaytaradi."""
    password = generate()
    user.set_password(password)
    user.must_change_password = True
    user.temp_password_expires = timezone.now() + timedelta(hours=TEMP_PASSWORD_HOURS)
    user.save(update_fields=["password", "must_change_password", "temp_password_expires"])
    return password, user.temp_password_expires


def clear(user):
    """Doimiy parol o'rnatilganda (save chaqiruvchida)."""
    user.must_change_password = False
    user.temp_password_expires = None


def is_expired(user):
    return bool(user.temp_password_expires and user.temp_password_expires <= timezone.now())


def _bind(user):
    # Parol xeshiga bog'langan: parol almashgach eski token o'z-o'zidan yaroqsiz
    return salted_hmac(_SALT, user.password).hexdigest()[:24]


def issue_change_token(user):
    return signing.dumps({"u": user.pk, "n": secrets.token_urlsafe(12), "h": _bind(user)}, salt=_SALT)


def read_change_token(token):
    """(user, nonce) yoki None."""
    from .models import User

    try:
        data = signing.loads(token or "", salt=_SALT, max_age=PW_TOKEN_AGE)
    except signing.BadSignature:
        return None
    if cache.get(f"pwchange:done:{data.get('n')}"):
        return None
    user = User.objects.filter(pk=data.get("u"), is_active=True).first()
    if not user or not user.must_change_password or is_expired(user):
        return None
    if not constant_time_compare(data.get("h", ""), _bind(user)):
        return None
    return user, data.get("n")


def consume_change_token(nonce):
    cache.set(f"pwchange:done:{nonce}", 1, PW_TOKEN_AGE)
