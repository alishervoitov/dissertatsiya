"""
Kunlik Telegram langari uchun oxirgi 24 soat statistikasi (records/audit_anchor.py).

Ikki manba:
  * baza (o'zgarmas tarix): muvaffaqiyatli kirishlar (LoginSession), ogohlantirishlar (SecurityAlert),
    audit yozuvlari va favqulodda kirishlar (AuditLog);
  * Redis hisoblagichlari - bazaga yozilmaydigan hodisalar: noto'g'ri parol, login bloklanishi,
    2FA xatolari, zaxira kod bilan kirish. Soatlik "savat"lar (26 soat yashaydi), oxirgi 24 tasi qo'shiladi.
    Redis qayta ishga tushsa shu hisoblagichlar nolga tushadi - xabarda shunday belgilanadi.
Hech qanday shaxsiy ma'lumot (login, IP, bemor) xabarga chiqmaydi - faqat sonlar.
"""
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count
from django.utils import timezone

WINDOW = timedelta(hours=24)
BUCKET_TTL = int(timedelta(hours=26).total_seconds())
EVENTS = ("login_failed", "lockout", "mfa_failed", "mfa_exhausted", "backup_code")


def _bucket(event, when):
    return f"stats:{event}:{when:%Y%m%d%H}"


def bump(event):
    """Hodisani joriy soat savatiga qo'shadi. Redis ishlamasa jim o'tadi (asosiy oqim buzilmaydi)."""
    key = _bucket(event, timezone.now())
    try:
        if not cache.add(key, 1, BUCKET_TTL):
            cache.incr(key)
    except Exception:  # noqa: BLE001
        pass


def counters(now=None):
    now = now or timezone.now()
    hours = [now - timedelta(hours=h) for h in range(24)]
    out = {}
    for event in EVENTS:
        try:
            values = cache.get_many([_bucket(event, h) for h in hours])
        except Exception:  # noqa: BLE001
            values = {}
        out[event] = sum(int(v or 0) for v in values.values())
    return out


def collect(now=None):
    from records.models import AuditLog
    from users.models import LoginSession

    from .models import SecurityAlert

    now = now or timezone.now()
    since = now - WINDOW
    sessions = LoginSession.objects.filter(created_at__gte=since)
    by_role = dict(sessions.values_list("user__role").annotate(n=Count("id")))
    alerts = SecurityAlert.objects.filter(first_seen__gte=since)
    sev = dict(alerts.values_list("severity").annotate(n=Count("id")))
    audit = AuditLog.objects.filter(timestamp__gte=since)
    actions = dict(audit.values_list("action").annotate(n=Count("id")))
    return {
        "logins": sum(by_role.values()),
        "logins_staff": sum(n for r, n in by_role.items() if r != "patient"),
        "logins_patient": by_role.get("patient", 0),
        "login_users": sessions.values("user").distinct().count(),
        "alerts": sum(sev.values()),
        "alerts_high": sev.get("high", 0),
        "alerts_medium": sev.get("medium", 0),
        "alerts_open": SecurityAlert.objects.filter(status=SecurityAlert.OPEN).count(),
        "suspended": SecurityAlert.objects.filter(status=SecurityAlert.OPEN, auto_response=SecurityAlert.LOCKED)
                                          .values("actor").distinct().count(),
        "emergency": alerts.filter(rule=SecurityAlert.EMERGENCY_ACCESS).count(),
        "audit": sum(actions.values()),
        "audit_view": actions.get("view", 0),
        "audit_export": actions.get("export", 0),
        "audit_change": sum(actions.get(a, 0) for a in ("create", "update", "delete")),
        **counters(now),
    }


def render(s):
    """Langar xabari oxiriga qo'shiladigan blok (HTML; faqat sonlar)."""
    warn = " ⚠️" if s["lockout"] or s["mfa_exhausted"] or s["alerts_high"] or s["suspended"] else ""
    lines = [
        f"\n\n📊 <b>Oxirgi 24 soat</b>{warn}",
        f"Kirishlar: {s['logins']} ({s['logins_staff']} xodim, {s['logins_patient']} bemor; "
        f"{s['login_users']} foydalanuvchi)",
        f"Noto'g'ri parol: {s['login_failed']} · bloklangan: {s['lockout']}",
        f"2FA xatosi: {s['mfa_failed']} · 5 martalik: {s['mfa_exhausted']} · zaxira kod: {s['backup_code']}",
        f"Ogohlantirishlar: {s['alerts']} (🔴 {s['alerts_high']}, 🟠 {s['alerts_medium']}) · "
        f"ochiq: {s['alerts_open']} · to'xtatilgan hisob: {s['suspended']}",
        f"Favqulodda kirish: {s['emergency']}",
        f"Audit yozuvlari: {s['audit']} (ko'rish {s['audit_view']}, o'zgartirish {s['audit_change']}, "
        f"eksport {s['audit_export']})",
    ]
    return "\n".join(lines)
