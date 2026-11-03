import logging
from datetime import timedelta

from django.db import IntegrityError, transaction

from .models import Notification

logger = logging.getLogger(__name__)

NEAR_THRESHOLD = 2          # oldinda shuncha yoki kamroq kishi qolganda xabar beriladi
RX_WARN_DAYS = 2            # retsept tugashiga shuncha kun qolganda


def notify(user, kind, params=None, link="", dedupe_key=""):
    """Bildirishnoma yaratadi. Xato asosiy amalni (natija saqlash, navbat chaqirish) buzmaydi."""
    if user is None:
        return None
    try:
        with transaction.atomic():
            if dedupe_key and Notification.objects.filter(user=user, dedupe_key=dedupe_key).exists():
                return None
            return Notification.objects.create(user=user, kind=kind, params=params or {}, link=link, dedupe_key=dedupe_key)
    except IntegrityError:
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Bildirishnoma yaratilmadi")
        return None


def notify_lab_ready(result):
    abnormal = sum(1 for v in result.values.all() if v.flag in ("low", "high", "abnormal"))
    return notify(result.patient.user, Notification.LAB_READY,
                  {"title": result.title, "abnormal": abnormal}, link="/me", dedupe_key=f"lab:{result.id}")


def notify_queue(profile):
    """Navbat siljiganda: chaqirilgan bemorga va oldida <= 2 kishi qolganlarga xabar."""
    try:
        _notify_queue(profile)
    except Exception:  # noqa: BLE001
        logger.exception("Navbat bildirishnomalari yuborilmadi")


def _notify_queue(profile):
    from appointments.models import QueueTicket
    from appointments.services import queue_state

    state = queue_state(profile)
    doctor = profile.user.get_full_name() or profile.user.username
    cur = state.current
    if cur and cur.patient_id:
        notify(cur.patient.user, Notification.QUEUE_CALLED,
               {"number": cur.number, "doctor": doctor, "room": profile.room}, link="/queue",
               dedupe_key=f"queue_called:{cur.id}")
    for t in state.present + state.pending:
        if not t.patient_id or t.status != QueueTicket.WAITING:
            continue
        ahead = state.ahead_of(t)
        if ahead <= NEAR_THRESHOLD:
            notify(t.patient.user, Notification.QUEUE_NEAR,
                   {"number": t.number, "doctor": doctor, "ahead": ahead, "arrived": t.arrived}, link="/queue",
                   dedupe_key=f"queue_near:{t.id}")


def ensure_rx_notices(user, today):
    """Tugashiga oz qolgan faol retseptlar uchun bildirishnoma (ro'yxat ochilganda tekshiriladi)."""
    from records.models import Prescription

    qs = Prescription.objects.filter(patient__user=user, is_active=True)
    for rx in qs:
        end = rx.start_date + timedelta(days=rx.duration_days)
        days_left = (end - today).days
        if 0 <= days_left <= RX_WARN_DAYS:
            notify(user, Notification.RX_EXPIRING,
                   {"medication": rx.medication_name, "end_date": end.isoformat(), "days_left": days_left},
                   link="/me", dedupe_key=f"rx:{rx.id}")
