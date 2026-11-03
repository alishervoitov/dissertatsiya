"""
Audit jurnali yaxlitligini tekshirish xizmati: API, buyruq va kunlik Celery vazifasi shu yerdan foydalanadi.
"""
import logging

from django.db import transaction
from django.utils import timezone

from . import audit_chain
from .models import AuditCheckpoint, AuditLog

security_logger = logging.getLogger("security")


def run_check(make_checkpoint=False):
    result = audit_chain.verify(AuditLog, AuditCheckpoint)
    result["checked_at"] = timezone.now()
    last_cp = AuditCheckpoint.objects.order_by("-id").first()
    result["last_checkpoint"] = (
        {"id": last_cp.id, "created_at": last_cp.created_at, "last_id": last_cp.last_id, "hash": last_cp.last_hash}
        if last_cp else None
    )

    if result["ok"]:
        # Zanjir oxiri tashqi log faylga ham yoziladi: baza to'liq qayta yozilsa ham iz qoladi
        security_logger.info("AUDIT-CHAIN OK | yozuvlar=%s oxirgi_id=%s xesh=%s",
                             result["checked"], result["last_id"], result["head"])
        if make_checkpoint and result["last_id"]:
            AuditCheckpoint.objects.create(last_id=result["last_id"], last_hash=result["head"], count=result["checked"])
    else:
        security_logger.critical("AUDIT-CHAIN BUZILGAN | muammolar=%s", result["problems"][:10])
        _raise_alert(result)
    return result


def _raise_alert(result):
    from monitoring.models import SecurityAlert

    now = timezone.now()
    ids = ", ".join(f"#{p['id']}" for p in result["problems"][:5])
    detail = f"Audit jurnali zanjiri buzilgan: {len(result['problems'])} ta muammo ({ids})"[:255]
    with transaction.atomic():
        alert = SecurityAlert.objects.filter(rule=SecurityAlert.AUDIT_TAMPER, status=SecurityAlert.OPEN).first()
        if alert:
            alert.count += 1
            alert.last_seen = now
            alert.detail = detail
            alert.save(update_fields=["count", "last_seen", "detail"])
        else:
            SecurityAlert.objects.create(
                rule=SecurityAlert.AUDIT_TAMPER, severity="high", actor=None, detail=detail,
                first_seen=now, last_seen=now,
            )
