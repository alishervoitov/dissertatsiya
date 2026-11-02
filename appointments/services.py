"""
Navbat mantig'i: raqam berish, taxminiy vaqt (ETA) hisoblash va shifokor amallari.

Taxminiy vaqt "aralash" usulda hisoblanadi: shifokorga belgilangan o'rtacha
qabul davomiyligi bugungi yakunlangan qabullarning haqiqiy davomiyligi bilan
o'rtalashtiriladi. Belgilangan qiymat PRIOR_WEIGHT ta qabulga teng vazn oladi,
shuning uchun kun boshida u ustun, qabullar ko'paygan sari haqiqiy tezlik ustun.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from .models import DoctorProfile, QueueTicket

PRIOR_WEIGHT = 3
MIN_SERVICE_MINUTES = 3
MAX_SERVICE_MINUTES = 120
MAX_ACTIVE_TICKETS_PER_PATIENT = 3


class QueueError(Exception):
    """Foydalanuvchiga ko'rsatiladigan navbat xatosi."""

    def __init__(self, message, code="queue_error"):
        super().__init__(message)
        self.message = message
        self.code = code


def queue_tz():
    return ZoneInfo(getattr(settings, "QUEUE_TIME_ZONE", "Asia/Tashkent"))


def local_now():
    return timezone.now().astimezone(queue_tz())


def today():
    return local_now().date()


def service_minutes(profile, date):
    """Bitta qabulning kutilayotgan davomiyligi (daqiqa), aralash usulda."""
    done = QueueTicket.objects.filter(
        doctor_id=profile.user_id, date=date, status=QueueTicket.DONE,
        called_at__isnull=False, finished_at__isnull=False,
    ).values_list("called_at", "finished_at")
    total, n = 0.0, 0
    for called, finished in done:
        minutes = (finished - called).total_seconds() / 60
        if minutes > 0:
            total += min(minutes, MAX_SERVICE_MINUTES)
            n += 1
    blended = (profile.avg_minutes * PRIOR_WEIGHT + total) / (PRIOR_WEIGHT + n)
    return max(MIN_SERVICE_MINUTES, min(MAX_SERVICE_MINUTES, blended))


@dataclass
class QueueState:
    """Navbat holati. "present" - shifoxonaga kelgan va kutayotganlar (shifokor faqat ularni chaqiradi),
    "pending" - hali kelmagan onlayn bemorlar: ular hech kimni kutdirmaydi, kelganda o'z raqami bo'yicha
    navbatdagi joyiga qo'yiladi."""

    profile: DoctorProfile
    date: object
    minutes: float
    current: QueueTicket | None
    present: list
    pending: list
    first_start: datetime          # kelganlar ichida birinchisining taxminiy qabul vaqti
    work_end: datetime

    @property
    def waiting(self):
        return self.present

    def eta_for_index(self, index):
        return self.first_start + timedelta(minutes=self.minutes * index)

    def ahead_of(self, ticket):
        """Oldinda turgan kelgan bemorlar soni. Kelmagan onlayn bemor uchun: hozir kelsa qancha bo'lardi."""
        return sum(1 for t in self.present if t.number < ticket.number and t.id != ticket.id)

    def eta_for(self, ticket):
        if ticket.status != QueueTicket.WAITING:
            return None
        return self.eta_for_index(self.ahead_of(ticket))

    def position_of(self, ticket):
        if ticket.status != QueueTicket.WAITING:
            return None
        return self.ahead_of(ticket) + 1

    @property
    def next_eta_walkin(self):
        """Hozir shifoxonada chipta olgan bemorning taxminiy vaqti: faqat kelganlar hisobga olinadi."""
        return self.eta_for_index(len(self.present))

    @property
    def next_eta(self):
        """Onlayn navbat olayotgan bemor uchun ehtiyotkor baho: kelmagan onlayn bemorlar ham
        undan oldin ro'yxatga olingan, ular kelsa oldinga o'tadi."""
        return self.eta_for_index(len(self.present) + len(self.pending))

    @property
    def accepting(self):
        return self.profile.accepts_queue and self.next_eta < self.work_end

    @property
    def accepting_walkin(self):
        return self.profile.accepts_queue and self.next_eta_walkin < self.work_end


def queue_state(profile, now=None):
    tz = queue_tz()
    now = (now or timezone.now()).astimezone(tz)
    date = now.date()
    minutes = service_minutes(profile, date)
    tickets = list(
        QueueTicket.objects.filter(
            doctor_id=profile.user_id, date=date, status__in=QueueTicket.ACTIVE,
        ).select_related("patient__user").order_by("number")
    )
    current = next((t for t in tickets if t.status == QueueTicket.CALLED), None)
    waiting = [t for t in tickets if t.status == QueueTicket.WAITING]
    present = [t for t in waiting if t.arrived_at]
    pending = [t for t in waiting if not t.arrived_at]

    start = max(now, datetime.combine(date, profile.work_start, tzinfo=tz))
    if current and current.called_at:
        elapsed = (now - current.called_at).total_seconds() / 60
        remaining = max(minutes - elapsed, 1)
        start = max(start, now + timedelta(minutes=remaining))
    work_end = datetime.combine(date, profile.work_end, tzinfo=tz)
    return QueueState(profile, date, minutes, current, present, pending, start, work_end)


def issue_ticket(profile, source, patient=None, visitor_name="", issued_by=None, enforce_hours=True):
    """Yangi navbat raqamini beradi. Bir vaqtdagi so'rovlarda raqam takrorlanmasligi
    uchun shifokor profili qatori bloklanadi, unique constraint esa oxirgi himoya."""
    date = today()
    if patient is not None:
        active = QueueTicket.objects.filter(patient=patient, date=date, status__in=QueueTicket.ACTIVE)
        if active.filter(doctor_id=profile.user_id).exists():
            raise QueueError("Bu shifokorga bugun navbatingiz bor.", "duplicate")
        if active.count() >= MAX_ACTIVE_TICKETS_PER_PATIENT:
            raise QueueError("Bir kunda ko'pi bilan 3 ta faol navbat olish mumkin.", "limit")

    for _ in range(3):
        try:
            with transaction.atomic():
                locked = DoctorProfile.objects.select_for_update().get(pk=profile.pk)
                if not locked.accepts_queue:
                    raise QueueError("Shifokor bugun navbat qabul qilmayapti.", "closed")
                state = queue_state(locked)
                open_now = state.accepting if source == QueueTicket.SOURCE_ONLINE else state.accepting_walkin
                if enforce_hours and not open_now:
                    raise QueueError("Bugungi ish vaqti tugaguncha bo'sh joy qolmadi.", "full")
                last = QueueTicket.objects.filter(doctor_id=locked.user_id, date=date).aggregate(m=Max("number"))["m"]
                return QueueTicket.objects.create(
                    doctor_id=locked.user_id, date=date, number=(last or 0) + 1, source=source,
                    patient=patient, visitor_name=visitor_name, issued_by=issued_by,
                    # Jonli navbat oluvchi shifoxonada turibdi; onlayn bemor kelganini keyin tasdiqlaydi
                    arrived_at=None if source == QueueTicket.SOURCE_ONLINE else timezone.now(),
                )
        except IntegrityError:
            continue
    raise QueueError("Navbat berishda xatolik, qayta urinib ko'ring.", "retry")


def finish_current(profile):
    now = timezone.now()
    return QueueTicket.objects.filter(
        doctor_id=profile.user_id, date=today(), status=QueueTicket.CALLED,
    ).update(status=QueueTicket.DONE, finished_at=now)


def call_next(profile):
    """Joriy bemorni yakunlaydi va navbatdagi birinchi bemorni chaqiradi."""
    with transaction.atomic():
        DoctorProfile.objects.select_for_update().get(pk=profile.pk)
        finish_current(profile)
        # Faqat shifoxonaga kelganlar chaqiriladi; kelmagan onlayn bemor navbatni to'xtatib qo'ymaydi
        nxt = QueueTicket.objects.filter(
            doctor_id=profile.user_id, date=today(), status=QueueTicket.WAITING, arrived_at__isnull=False,
        ).order_by("number").first()
        if nxt:
            nxt.status = QueueTicket.CALLED
            nxt.called_at = timezone.now()
            nxt.save(update_fields=["status", "called_at"])
        return nxt


def mark_arrived(ticket):
    if ticket.status != QueueTicket.WAITING:
        raise QueueError("Faqat kutilayotgan navbat uchun kelganlikni belgilash mumkin.", "not_waiting")
    if ticket.date != today():
        raise QueueError("Navbat bugungi kun uchun emas.", "not_today")
    if not ticket.arrived_at:
        ticket.arrived_at = timezone.now()
        ticket.save(update_fields=["arrived_at"])
    return ticket
