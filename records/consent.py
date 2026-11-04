"""
Bemor roziligi asosidagi kirish nazorati (shifokor uchun).

Shifokor bemor kartasini (profil, kasallik tarixi, retseptlar, tahlillar, fayllar, PDF) faqat
quyidagi hollarda ko'radi:
  1. "grant"     - bemor shu shifokorga ruxsat bergan (muddati o'tmagan, bekor qilinmagan);
  2. "visit"     - bemor bugun shu shifokorning navbatida (qabulga yozilgan bemor - avtomatik rozilik);
  3. "emergency" - favqulodda kirish: shifokor sabab yozgan, 1 soat amal qiladi, admin ogohlantiriladi.
Boshqa hollarda kirish rad etiladi (403, code=consent_required).

PATIENT_CONSENT_REQUIRED=False bo'lsa eski tartib (shifokor hamma bemorni ko'radi) ishlaydi.
"""
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

EMERGENCY_MINUTES = 60
EMERGENCY_MIN_REASON = 20
VISIT_STATUSES = ("waiting", "called", "done")


def consent_required():
    return getattr(settings, "PATIENT_CONSENT_REQUIRED", True)


def _today():
    from appointments.services import today
    return today()


def _active_grants(doctor, now=None):
    from .models import CareGrant
    now = now or timezone.now()
    return CareGrant.objects.filter(doctor=doctor, revoked_at__isnull=True).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now))


def _active_emergency(doctor, now=None):
    from .models import EmergencyAccess
    return EmergencyAccess.objects.filter(doctor=doctor, expires_at__gt=now or timezone.now())


def _visit_patient_ids(doctor):
    from appointments.models import QueueTicket
    return QueueTicket.objects.filter(doctor=doctor, date=_today(), status__in=VISIT_STATUSES,
                                      patient__isnull=False).values_list("patient_id", flat=True)


def access_basis(doctor, patient_id):
    """Kirish asosi: 'grant' | 'visit' | 'emergency' | None."""
    if _active_grants(doctor).filter(patient_id=patient_id).exists():
        return "grant"
    if patient_id in set(_visit_patient_ids(doctor)):
        return "visit"
    if _active_emergency(doctor).filter(patient_id=patient_id).exists():
        return "emergency"
    return None


def doctor_has_access(doctor, patient):
    if not consent_required():
        return True
    pid = patient if isinstance(patient, int) else patient.pk
    return access_basis(doctor, pid) is not None


def accessible_patient_ids(doctor):
    """Shifokor hozir ko'ra oladigan bemorlar (queryset filtrlash uchun)."""
    ids = set(_active_grants(doctor).values_list("patient_id", flat=True))
    ids |= set(_visit_patient_ids(doctor))
    ids |= set(_active_emergency(doctor).values_list("patient_id", flat=True))
    return ids


def access_map(doctor, patient_ids):
    """{patient_id: (asos, tugash_vaqti)} - ro'yxat sahifasi uchun bitta so'rovda."""
    now = timezone.now()
    result = {}
    for g in _active_grants(doctor, now).filter(patient_id__in=patient_ids):
        prev = result.get(g.patient_id)
        if prev is None or g.expires_at is None or (prev[1] and g.expires_at > prev[1]):
            result[g.patient_id] = ("grant", g.expires_at)
    for pid in set(_visit_patient_ids(doctor)) & set(patient_ids):
        result.setdefault(pid, ("visit", None))
    for e in _active_emergency(doctor, now).filter(patient_id__in=patient_ids):
        result.setdefault(e.patient_id, ("emergency", e.expires_at))
    return result


def deny():
    raise PermissionDenied({
        "detail": "Bemor sizga kartasini ko'rishga ruxsat bermagan. Zarur bo'lsa favqulodda kirishdan foydalaning.",
        "code": "consent_required",
    })


def require_access(user, patient):
    """Shifokor uchun tekshiruv; boshqa rollarga ta'sir qilmaydi."""
    from users.models import Role
    if user.role == Role.DOCTOR and not doctor_has_access(user, patient):
        deny()


def open_emergency(doctor, patient, reason):
    """Favqulodda kirishni ochadi: audit, admin ogohlantirishi, bemorga bildirishnoma."""
    from .audit import log_audit
    from .models import EmergencyAccess

    now = timezone.now()
    access = EmergencyAccess.objects.create(
        doctor=doctor, patient=patient, reason=reason, expires_at=now + timedelta(minutes=EMERGENCY_MINUTES))
    log_audit(doctor, "view", patient=patient, detail=f"FAVQULODDA KIRISH: {reason}"[:255])

    from monitoring.models import SecurityAlert
    SecurityAlert.objects.create(
        rule=SecurityAlert.EMERGENCY_ACCESS, severity="high", actor=doctor, actor_role=doctor.role,
        detail=f"Bemor #{patient.pk}: {reason}"[:255], first_seen=now, last_seen=now,
    )
    from notifications.services import notify
    notify(patient.user, "emergency_access",
           {"doctor": doctor.get_full_name() or doctor.username, "reason": reason[:200]},
           link="/my-consent", dedupe_key=f"emergency:{access.pk}")
    return access
