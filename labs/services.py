from django.utils import timezone

from .models import LabResult


def local_today():
    from appointments.services import today
    return today()


def group_key(result):
    """Bir xil tahlil guruhi: qon, biokimyo, siydik - tur bo'yicha; "boshqa" tahlillar - nomi bo'yicha."""
    if result.panel == "custom":
        return ("custom", result.title.strip().lower())
    return (result.panel, "")


def latest_valid_results(patient, today=None):
    """Har bir tahlil guruhidan eng oxirgi va muddati o'tmagan natija. Eski natija yangisi
    muddati o'tgan bo'lsa ham o'rniga chiqmaydi: eskirgan ma'lumot berilmaydi."""
    today = today or local_today()
    latest = {}
    qs = (LabResult.objects.filter(patient=patient)
          .select_related("created_by").prefetch_related("values")
          .order_by("-collected_at", "-id"))
    for r in qs:
        latest.setdefault(group_key(r), r)
    return [r for r in latest.values() if not r.is_expired(today)]
