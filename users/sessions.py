"""
Faol sessiyalar: har bir login refresh tokeni (jti) bilan bog'langan LoginSession.

Sessiya yopilganda:
  1) refresh token blacklist qilinadi - yangi access token olib bo'lmaydi;
  2) sid kesh orqali "yopilgan" deb belgilanadi - qo'ldagi access token ham darhol
     rad etiladi (15 daqiqa kutib o'tirmaydi), qarang users/authentication.py.
"""
import re
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import LoginSession

SID_CLAIM = "sid"


def _revoked_key(sid):
    return f"session:revoked:{sid}"


def _access_lifetime_seconds():
    return int(settings.SIMPLE_JWT.get("ACCESS_TOKEN_LIFETIME").total_seconds()) + 60


def is_revoked(sid):
    return bool(sid) and cache.get(_revoked_key(sid)) is not None


def _exp_to_dt(exp):
    return datetime.fromtimestamp(int(exp), tz=dt_timezone.utc)


def _client(request):
    from .security import client_ident
    ip = client_ident(request) or None
    ua = (request.META.get("HTTP_USER_AGENT") or "")[:300]
    return ip, ua


def start_session(request, user, refresh):
    """Login muvaffaqiyatli bo'lganda chaqiriladi (refresh - RefreshToken obyekti)."""
    ip, ua = _client(request)
    now = timezone.now()
    return LoginSession.objects.create(
        user=user, jti=refresh["jti"], ip=ip, user_agent=ua,
        last_seen=now, expires_at=_exp_to_dt(refresh["exp"]),
    )


def touch_session(request, refresh):
    """Access token yangilanganda: oxirgi faollik vaqti va IP yangilanadi.
    Joylashtirishdan oldin berilgan tokenlar uchun sessiya shu yerda yaratiladi."""
    ip, ua = _client(request)
    now = timezone.now()
    updated = LoginSession.objects.filter(jti=refresh["jti"], revoked_at__isnull=True).update(last_seen=now, ip=ip)
    if not updated and not LoginSession.objects.filter(jti=refresh["jti"]).exists():
        LoginSession.objects.create(
            user_id=refresh["user_id"], jti=refresh["jti"], ip=ip, user_agent=ua,
            last_seen=now, expires_at=_exp_to_dt(refresh["exp"]),
        )


def active_sessions(user):
    return LoginSession.objects.filter(user=user, revoked_at__isnull=True, expires_at__gt=timezone.now())


def revoke(sessions):
    """Berilgan sessiyalarni yopadi; yopilganlar sonini qaytaradi."""
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    sessions = list(sessions)
    if not sessions:
        return 0
    jtis = [s.jti for s in sessions]
    BlacklistedToken.objects.bulk_create(
        [BlacklistedToken(token=t) for t in OutstandingToken.objects.filter(jti__in=jtis)],
        ignore_conflicts=True,
    )
    LoginSession.objects.filter(jti__in=jtis, revoked_at__isnull=True).update(revoked_at=timezone.now())
    cache.set_many({_revoked_key(j): 1 for j in jtis}, _access_lifetime_seconds())
    return len(jtis)


def mark_revoked_jtis(jtis):
    """Boshqa joyda (masalan parol tiklanganda) blacklist qilingan tokenlar uchun."""
    jtis = list(jtis)
    if jtis:
        LoginSession.objects.filter(jti__in=jtis, revoked_at__isnull=True).update(revoked_at=timezone.now())
        cache.set_many({_revoked_key(j): 1 for j in jtis}, _access_lifetime_seconds())


# ---- Qurilma nomi (User-Agent dan, taxminiy) ----

_BROWSERS = [
    ("Edge", r"Edg(?:e|A|iOS)?/"), ("Opera", r"OPR/|Opera"), ("Yandex", r"YaBrowser/"),
    ("Samsung Internet", r"SamsungBrowser/"), ("Firefox", r"Firefox/|FxiOS/"),
    ("Chrome", r"Chrome/|CriOS/"), ("Safari", r"Safari/"),
]
_OSES = [
    ("iPhone", r"iPhone"), ("iPad", r"iPad"), ("Android", r"Android"),
    ("Windows", r"Windows"), ("macOS", r"Mac OS X|Macintosh"), ("Linux", r"Linux"),
]


def describe_device(ua):
    ua = ua or ""
    browser = next((name for name, rx in _BROWSERS if re.search(rx, ua)), "")
    os_name = next((name for name, rx in _OSES if re.search(rx, ua)), "")
    if os_name == "iPad" or (os_name == "Android" and "Mobile" not in ua):
        kind = "tablet"
    elif os_name in ("iPhone", "Android"):
        kind = "mobile"
    elif os_name:
        kind = "desktop"
    else:
        kind = "unknown"
    return {"browser": browser, "os": os_name, "kind": kind}
