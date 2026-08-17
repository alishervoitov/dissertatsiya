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

    from .models import Prescription

    today = timezone.now().date()
    expired_count = 0

    for prescription in Prescription.objects.filter(is_active=True):
        end_date = prescription.start_date + timedelta(days=prescription.duration_days)
        if end_date < today:
            prescription.is_active = False
            prescription.save(update_fields=["is_active"])
            expired_count += 1

    security_logger.info("Davriy vazifa: %s ta retsept muddati tugagani uchun yopildi", expired_count)
    return expired_count