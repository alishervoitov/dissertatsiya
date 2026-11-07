"""
Shubhali faollikni aniqlash qoidalari. log_audit() har bir yozuvdan keyin check() ni
chaqiradi. Qoidalar ataylab sodda va tushunarli: chegaralar sozlamada (SECURITY_ALERTS).

1. mass_access - xodim qisqa vaqtda juda ko'p turli bemor kartasiga murojaat qildi;
2. off_hours   - xodim ish vaqtidan tashqari (tunda) bemor ma'lumotiga murojaat qildi;
3. bulk_export - xodim qisqa vaqtda ko'p PDF/eksport oldi.
"""
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

security_logger = logging.getLogger("security")

DEFAULTS = {
    "MASS_ACCESS_PATIENTS": 20,
    "MASS_ACCESS_MINUTES": 10,
    "EXPORT_COUNT": 5,
    "EXPORT_MINUTES": 30,
    "NIGHT_START_HOUR": 22,
    "NIGHT_END_HOUR": 6,
    # Yuqori darajali shu qoidalarda xodimning sessiyalari darhol yopiladi va hisob to'xtatiladi
    "AUTO_SUSPEND_RULES": ("mass_access", "bulk_export"),
}
STAFF_ROLES = ("doctor", "admin", "lab")
DATA_ACTIONS = ("view", "export", "update", "delete", "create")


def config():
    return {**DEFAULTS, **getattr(settings, "SECURITY_ALERTS", {})}


def local_tz():
    return ZoneInfo(getattr(settings, "QUEUE_TIME_ZONE", "Asia/Tashkent"))


def _raise(rule, severity, actor, now, window, detail, count=1, dedupe_since=None):
    """Bir xil qoida va xodim uchun ochiq ogohlantirish bo'lsa, yangisini yaratmasdan hisoblagichni oshiradi."""
    from .models import SecurityAlert

    since = dedupe_since or (now - window)
    alert = (SecurityAlert.objects.filter(rule=rule, actor=actor, status=SecurityAlert.OPEN, last_seen__gte=since)
             .order_by("-last_seen").first())
    if alert:
        alert.count = max(alert.count + 1, count)
        alert.last_seen = now
        alert.detail = detail
        alert.save(update_fields=["count", "last_seen", "detail"])
        return alert
    from .response import planned_response, apply_response

    response = planned_response(rule, severity, actor)
    alert = SecurityAlert.objects.create(
        rule=rule, severity=severity, actor=actor, actor_role=actor.role, count=count,
        detail=detail, first_seen=now, last_seen=now, auto_response=response,
    )
    security_logger.warning("XAVFSIZLIK OGOHLANTIRISHI | rule=%s actor=%s detail=%s", rule, actor.username, detail)
    apply_response(alert)
    return alert


def check(entry):
    """Audit yozuvi bo'yicha qoidalarni tekshiradi. Faqat xodimlar va bemor ma'lumotiga oid harakatlar."""
    from records.models import AuditLog

    actor = entry.actor
    if actor is None or actor.role not in STAFF_ROLES or entry.patient_id is None or entry.action not in DATA_ACTIONS:
        return []
    cfg = config()
    now = entry.timestamp or timezone.now()
    raised = []

    # 1. Qisqa vaqtda ko'p turli bemor
    window = timedelta(minutes=cfg["MASS_ACCESS_MINUTES"])
    patients = (AuditLog.objects.filter(actor=actor, timestamp__gte=now - window, patient__isnull=False)
                .values("patient").distinct().count())
    if patients >= cfg["MASS_ACCESS_PATIENTS"]:
        raised.append(_raise(
            "mass_access", "high", actor, now, window,
            f"{cfg['MASS_ACCESS_MINUTES']} daqiqada {patients} ta turli bemor kartasi", count=patients,
        ))

    # 2. Tungi murojaat (mahalliy vaqt bo'yicha); bir kecha uchun bitta ogohlantirish
    local = now.astimezone(local_tz())
    start, end = cfg["NIGHT_START_HOUR"], cfg["NIGHT_END_HOUR"]
    if local.hour >= start or local.hour < end:
        night_began = local.replace(hour=start, minute=0, second=0, microsecond=0)
        if local.hour < end:
            night_began -= timedelta(days=1)
        raised.append(_raise(
            "off_hours", "medium", actor, now, timedelta(hours=12),
            f"Tungi murojaat {local:%H:%M} da ({entry.get_action_display()})", dedupe_since=night_began,
        ))

    # 3. Ko'p eksport
    if entry.action == "export":
        window = timedelta(minutes=cfg["EXPORT_MINUTES"])
        exports = AuditLog.objects.filter(actor=actor, action="export", timestamp__gte=now - window).count()
        if exports >= cfg["EXPORT_COUNT"]:
            raised.append(_raise(
                "bulk_export", "high", actor, now, window,
                f"{cfg['EXPORT_MINUTES']} daqiqada {exports} ta eksport", count=exports,
            ))
    return raised


def safe_check(entry):
    """Monitoring xatosi asosiy so'rovni hech qachon buzmasin."""
    from django.db import transaction

    try:
        # savepoint: tekshiruvdagi baza xatosi tashqi tranzaksiyani buzmasin
        with transaction.atomic():
            return check(entry)
    except Exception:  # noqa: BLE001
        security_logger.exception("Monitoring tekshiruvida xato (audit yozuvi saqlandi)")
        return []
