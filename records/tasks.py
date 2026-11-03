import logging

from celery import shared_task
from django.utils import timezone

security_logger = logging.getLogger("security")


@shared_task
def deactivate_expired_prescriptions():
    """
    Har kuni ishga tushadigan davriy vazifa: boshlanish sanasi + davomiylik
    muddati o'tgan, hali ham 'faol' deb belgilangan retseptlarni avtomatik
    'tugagan' holatiga o'tkazadi.
    """
    from datetime import timedelta

    from django.db import transaction

    from .audit import log_audit
    from .models import Prescription

    today = timezone.now().date()
    expired_count = 0

    for prescription in Prescription.objects.filter(is_active=True).select_related("patient", "medical_record"):
        end_date = prescription.start_date + timedelta(days=prescription.duration_days)
        if end_date < today:
            # Tibbiy yozuv o'zgarishi audit izisiz qolmasin: yopish va audit birga yoziladi (actor=None - tizim)
            with transaction.atomic():
                prescription.is_active = False
                prescription.save(update_fields=["is_active", "updated_at"])
                log_audit(
                    None, "update",
                    patient=prescription.patient, medical_record=prescription.medical_record,
                    detail=f"Retsept #{prescription.pk} muddati tugagani ({end_date}) uchun avtomatik yopildi (davriy vazifa)",
                )
            expired_count += 1

    security_logger.info("Davriy vazifa: %s ta retsept muddati tugagani uchun yopildi", expired_count)
    return expired_count


@shared_task
def verify_audit_chain():
    """Har kuni: audit zanjirini tekshiradi, butun bo'lsa nazorat nuqtasi qo'yadi, aks holda ogohlantiradi."""
    from .audit_integrity import run_check

    result = run_check(make_checkpoint=True)
    return {"ok": result["ok"], "checked": result["checked"], "problems": len(result["problems"])}
