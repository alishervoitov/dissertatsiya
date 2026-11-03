"""
Navbat statistikasi (administrator uchun): kutish vaqti, kelmaganlar ulushi, eng band shifokorlar.

Ta'riflar:
  * kutish vaqti   = chaqirilgan vaqt - shifoxonaga kelgan vaqt (faqat ikkalasi ma'lum bo'lsa);
  * qabul davomiyligi = yakunlangan vaqt - chaqirilgan vaqt;
  * kelmaganlar ulushi = kelmadi / (yakunlandi + kelmadi). Bekor qilingan va hali ochiq
    navbatlar maxrajga kirmaydi - ular "kelmadi" ham, "qabul qilindi" ham emas.
"""
from collections import defaultdict
from datetime import timedelta
from statistics import mean, median

from .models import QueueTicket
from .services import queue_tz, today

MAX_DAYS = 365


def _minutes(a, b):
    if not a or not b or b < a:
        return None
    return (b - a).total_seconds() / 60


def _avg(values):
    return round(mean(values), 1) if values else None


def _median(values):
    return round(median(values), 1) if values else None


def _rate(no_show, done):
    total = no_show + done
    return round(no_show * 100 / total, 1) if total else None


def queue_stats(days=30):
    days = max(1, min(int(days), MAX_DAYS))
    end = today()
    start = end - timedelta(days=days - 1)
    tz = queue_tz()
    tickets = list(
        QueueTicket.objects.filter(date__gte=start, date__lte=end)
        .select_related("doctor", "doctor__doctor_profile")
        .only("id", "date", "source", "status", "arrived_at", "called_at", "finished_at", "created_at",
              "doctor__id", "doctor__first_name", "doctor__last_name", "doctor__username",
              "doctor__doctor_profile__specialty")
    )

    counts = defaultdict(int)
    waits, services = [], []
    by_day = {start + timedelta(days=i): defaultdict(int) for i in range(days)}
    by_hour = defaultdict(int)
    by_source = defaultdict(lambda: defaultdict(int))
    docs = {}

    for t in tickets:
        counts[t.status] += 1
        by_day[t.date][t.status] += 1
        by_source[t.source]["tickets"] += 1
        by_source[t.source][t.status] += 1
        if t.arrived_at:
            by_hour[t.arrived_at.astimezone(tz).hour] += 1

        d = docs.get(t.doctor_id)
        if d is None:
            u = t.doctor
            profile = getattr(u, "doctor_profile", None)
            d = docs[t.doctor_id] = {
                "id": u.id, "name": u.get_full_name() or u.username,
                "specialty": getattr(profile, "specialty", "") or "",
                "counts": defaultdict(int), "waits": [], "services": [],
            }
        d["counts"][t.status] += 1

        w = _minutes(t.arrived_at, t.called_at)
        if w is not None and t.status in (QueueTicket.CALLED, QueueTicket.DONE):
            waits.append(w)
            d["waits"].append(w)
        s = _minutes(t.called_at, t.finished_at)
        if s is not None and t.status == QueueTicket.DONE:
            services.append(s)
            d["services"].append(s)

    done, no_show = counts[QueueTicket.DONE], counts[QueueTicket.NO_SHOW]
    doctors = []
    for d in docs.values():
        c = d["counts"]
        doctors.append({
            "id": d["id"], "name": d["name"], "specialty": d["specialty"],
            "tickets": sum(c.values()), "done": c[QueueTicket.DONE], "no_show": c[QueueTicket.NO_SHOW],
            "no_show_rate": _rate(c[QueueTicket.NO_SHOW], c[QueueTicket.DONE]),
            "avg_wait_min": _avg(d["waits"]), "avg_service_min": _avg(d["services"]),
        })
    doctors.sort(key=lambda x: (-x["done"], -x["tickets"], x["name"]))

    return {
        "period": {"from": str(start), "to": str(end), "days": days},
        "totals": {
            "tickets": len(tickets), "done": done, "no_show": no_show,
            "cancelled": counts[QueueTicket.CANCELLED],
            "open": counts[QueueTicket.WAITING] + counts[QueueTicket.CALLED],
            "no_show_rate": _rate(no_show, done),
            "avg_wait_min": _avg(waits), "median_wait_min": _median(waits),
            "avg_service_min": _avg(services),
        },
        "by_day": [
            {"date": str(day), "done": c[QueueTicket.DONE], "no_show": c[QueueTicket.NO_SHOW],
             "cancelled": c[QueueTicket.CANCELLED]}
            for day, c in sorted(by_day.items())
        ],
        "by_hour": [{"hour": h, "count": by_hour[h]} for h in sorted(by_hour)],
        "by_source": [
            {"source": src, "tickets": c["tickets"], "done": c[QueueTicket.DONE], "no_show": c[QueueTicket.NO_SHOW],
             "no_show_rate": _rate(c[QueueTicket.NO_SHOW], c[QueueTicket.DONE])}
            for src, c in sorted(by_source.items(), key=lambda kv: -kv[1]["tickets"])
        ],
        "doctors": doctors,
    }
