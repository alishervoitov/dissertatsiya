import logging
import secrets

from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from records.audit import log_audit
from records.models import Patient
from users.models import Role
from users.permissions import IsAdmin, IsDoctor, IsPatient

from . import checkin
from .models import DoctorProfile, PrintedCheckinCode, QueueTicket
from .permissions import HasKioskKey
from .serializers import (
    DoctorProfileSerializer,
    DoctorQueueSerializer,
    DoctorTicketSerializer,
    TakeTicketSerializer,
    TicketSerializer,
    doctor_name,
)
from .services import QueueError, call_next, finish_current, issue_ticket, mark_arrived, queue_state, today


def _active_profiles():
    return (
        DoctorProfile.objects.select_related("user")
        .filter(user__role=Role.DOCTOR, user__is_active=True)
        .order_by("specialty", "user__first_name", "user__last_name")
    )


def _doctor_list():
    return [DoctorQueueSerializer(queue_state(p)).data for p in _active_profiles()]


def _get_profile(doctor_id):
    return get_object_or_404(_active_profiles(), user_id=doctor_id)


def _error(exc):
    return Response({"detail": exc.message, "code": exc.code}, status=status.HTTP_409_CONFLICT)


def _ticket_data(ticket, serializer=TicketSerializer):
    state = queue_state(ticket.doctor.doctor_profile) if ticket.status in QueueTicket.ACTIVE else None
    return serializer((ticket, state)).data


# ---------- Bemor (onlayn navbat) ----------

class DoctorListView(APIView):
    """Navbat olish uchun shifokorlar: mutaxassislik, xona, navbatdagilar soni, taxminiy vaqt."""

    def get(self, request):
        return Response(_doctor_list())


class MyTicketsView(APIView):
    """Bemorning bugungi navbatlari (oxirgi holati bilan)."""

    permission_classes = [IsPatient]

    def get(self, request):
        tickets = QueueTicket.objects.filter(
            patient__user=request.user, date=today(),
        ).select_related("doctor__doctor_profile").order_by("-created_at")
        return Response([_ticket_data(t) for t in tickets])


class TakeTicketView(APIView):
    """Bemor o'zi tanlagan shifokorga bugungi navbatni oladi."""

    permission_classes = [IsPatient]
    throttle_scope = "queue_take"

    def post(self, request):
        s = TakeTicketSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        profile = _get_profile(s.validated_data["doctor"])
        patient = get_object_or_404(Patient, user=request.user)
        try:
            ticket = issue_ticket(profile, QueueTicket.SOURCE_ONLINE, patient=patient, issued_by=request.user)
        except QueueError as exc:
            return _error(exc)
        log_audit(request.user, "create", patient=patient,
                  detail=f"Onlayn navbat: {doctor_name(profile.user)} №{ticket.number}")
        return Response(_ticket_data(ticket), status=status.HTTP_201_CREATED)


class CancelTicketView(APIView):
    permission_classes = [IsPatient]

    def post(self, request, pk):
        ticket = get_object_or_404(QueueTicket, pk=pk, patient__user=request.user)
        if ticket.status != QueueTicket.WAITING:
            return Response({"detail": "Faqat kutilayotgan navbatni bekor qilish mumkin."},
                            status=status.HTTP_409_CONFLICT)
        ticket.status = QueueTicket.CANCELLED
        ticket.save(update_fields=["status"])
        return Response(_ticket_data(ticket))


class ArriveView(APIView):
    """Onlayn bemor shifoxonaga kelganini tasdiqlaydi. Bemorning o'zi, uning shifokori
    yoki registratura (administrator) belgilashi mumkin; boshqalar uchun chipta ko'rinmaydi."""

    def post(self, request, pk):
        user = request.user
        qs = QueueTicket.objects.select_related("doctor__doctor_profile")
        if user.role == Role.PATIENT:
            qs = qs.filter(patient__user=user)
        elif user.role == Role.DOCTOR:
            qs = qs.filter(doctor=user)
        elif user.role != Role.ADMIN:
            qs = qs.none()
        ticket = get_object_or_404(qs, pk=pk)
        if user.role == Role.PATIENT and getattr(settings, "QUEUE_CHECKIN_QR_REQUIRED", True):
            return Response(
                {"detail": "Kelganingizni shifoxonadagi QR kodni skanerlab tasdiqlang.", "code": "qr_required"},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            mark_arrived(ticket)
        except QueueError as exc:
            return _error(exc)
        if user.role == Role.PATIENT:
            return Response(_ticket_data(ticket))
        return Response(_ticket_data(ticket, DoctorTicketSerializer))


# ---------- Shifokor ----------

class DoctorQueueView(APIView):
    """Shifokorning bugungi navbati: joriy bemor, kutayotganlar va o'tganlar."""

    permission_classes = [IsDoctor]

    def get(self, request):
        profile = DoctorProfile.objects.get_or_create(user=request.user)[0]
        state = queue_state(profile)
        tickets = QueueTicket.objects.filter(doctor=request.user, date=state.date).select_related("patient__user")
        return Response({
            "doctor": DoctorQueueSerializer(state).data,
            "settings": DoctorProfileSerializer(profile).data,
            "tickets": [DoctorTicketSerializer((t, state)).data for t in tickets],
        })


class DoctorCallNextView(APIView):
    permission_classes = [IsDoctor]

    def post(self, request):
        profile = DoctorProfile.objects.get_or_create(user=request.user)[0]
        ticket = call_next(profile)
        from notifications.services import notify_queue
        notify_queue(profile)
        # Chaqirilgan bemordan oldingi raqamli, lekin hali kelmagan onlayn bemorlar
        skipped = []
        if ticket:
            skipped = list(QueueTicket.objects.filter(
                doctor=request.user, date=ticket.date, status=QueueTicket.WAITING,
                arrived_at__isnull=True, number__lt=ticket.number,
            ).order_by("number").values_list("number", flat=True))
        return Response({
            "called": DoctorTicketSerializer((ticket, None)).data if ticket else None,
            "skipped": skipped,
        })


class DoctorQueueCountView(APIView):
    """Menyudagi raqam uchun: shifoxonada kutayotganlar va hali kelmagan onlayn bemorlar."""

    permission_classes = [IsDoctor]

    def get(self, request):
        qs = QueueTicket.objects.filter(doctor=request.user, date=today(), status=QueueTicket.WAITING)
        return Response({
            "present": qs.filter(arrived_at__isnull=False).count(),
            "pending": qs.filter(arrived_at__isnull=True).count(),
        })


class DoctorFinishView(APIView):
    permission_classes = [IsDoctor]

    def post(self, request):
        profile = DoctorProfile.objects.get_or_create(user=request.user)[0]
        return Response({"finished": finish_current(profile)})


class DoctorNoShowView(APIView):
    permission_classes = [IsDoctor]

    def post(self, request, pk):
        ticket = get_object_or_404(QueueTicket, pk=pk, doctor=request.user, date=today(),
                                   status__in=QueueTicket.ACTIVE)
        ticket.status = QueueTicket.NO_SHOW
        ticket.save(update_fields=["status"])
        from notifications.services import notify_queue
        notify_queue(ticket.doctor.doctor_profile)
        return Response({"id": ticket.id, "status": ticket.status})


class DoctorSettingsView(APIView):
    """Shifokor o'z qabul sozlamalarini o'zgartiradi."""

    permission_classes = [IsDoctor]

    def patch(self, request):
        profile = DoctorProfile.objects.get_or_create(user=request.user)[0]
        s = DoctorProfileSerializer(profile, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        s.save()
        return Response(s.data)


class AdminDoctorProfileView(APIView):
    """Administrator shifokorning mutaxassisligi, xonasi va ish vaqtini o'zgartiradi."""

    permission_classes = [IsAdmin]

    def patch(self, request, doctor_id):
        profile = get_object_or_404(DoctorProfile, user_id=doctor_id, user__role=Role.DOCTOR)
        s = DoctorProfileSerializer(profile, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        s.save()
        return Response(s.data)


# ---------- Registratura (administrator) ----------

class RegistryTicketView(APIView):
    """Registratura shifoxonaga kelgan bemorga navbat chiptasini chiqaradi.
    Ish vaqti chegarasi tekshirilmaydi: qaror registratorda."""

    permission_classes = [IsAdmin]

    def get(self, request):
        result = []
        for profile in _active_profiles():
            state = queue_state(profile)
            tickets = QueueTicket.objects.filter(doctor_id=profile.user_id, date=state.date).select_related("patient__user")
            result.append({
                "doctor": DoctorQueueSerializer(state).data,
                "tickets": [DoctorTicketSerializer((t, state)).data for t in tickets],
            })
        return Response(result)

    def post(self, request):
        s = TakeTicketSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        profile = _get_profile(s.validated_data["doctor"])
        try:
            ticket = issue_ticket(
                profile, QueueTicket.SOURCE_REGISTRY,
                visitor_name=s.validated_data.get("visitor_name", "").strip(),
                issued_by=request.user, enforce_hours=False,
            )
        except QueueError as exc:
            return _error(exc)
        return Response(_ticket_data(ticket), status=status.HTTP_201_CREATED)


# ---------- Kiosk (shifoxonadagi ekran, hisobsiz) ----------

class KioskDoctorListView(APIView):
    authentication_classes = []
    permission_classes = [HasKioskKey]
    throttle_scope = "kiosk"

    def get(self, request):
        return Response(_doctor_list())


class KioskTicketView(APIView):
    authentication_classes = []
    permission_classes = [HasKioskKey]
    throttle_scope = "kiosk"

    def post(self, request):
        s = TakeTicketSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        profile = _get_profile(s.validated_data["doctor"])
        try:
            ticket = issue_ticket(profile, QueueTicket.SOURCE_KIOSK)
        except QueueError as exc:
            return _error(exc)
        return Response(_ticket_data(ticket), status=status.HTTP_201_CREATED)


# ---------- QR orqali kelganlikni tasdiqlash ----------

security_logger = logging.getLogger("security")


def _qr_payload(request, code, expires_in=None):
    url = checkin.checkin_url(checkin.safe_base(request, request.query_params.get("base", "")), code)
    data = {"url": url, "svg": checkin.qr_svg(url)}
    if expires_in is not None:
        data["expires_in"] = expires_in
    return data


class KioskCheckinCodeView(APIView):
    """Kiosk ekrani uchun aylanuvchi QR kod (har ~45 soniyada yangilanadi)."""

    authentication_classes = []
    permission_classes = [HasKioskKey]
    throttle_scope = "kiosk"

    def get(self, request):
        code, expires_in = checkin.rotating_code()
        return Response(_qr_payload(request, code, expires_in))


class AdminPrintedCheckinView(APIView):
    """Zaxira QR: administrator bugungi kod yaratadi (chop etish uchun) yoki bekor qiladi."""

    permission_classes = [IsAdmin]

    def _response(self, request, obj):
        if not obj or not obj.active:
            return Response({"active": False, "date": str(today())})
        return Response({"active": True, "date": str(obj.date), **_qr_payload(request, checkin.PRINTED_PREFIX + obj.code)})

    def get(self, request):
        return self._response(request, PrintedCheckinCode.objects.filter(date=today()).first())

    def post(self, request):
        # Har safar yangi kod: eski chop etilgan varaq darhol yaroqsiz bo'ladi
        obj, _ = PrintedCheckinCode.objects.update_or_create(
            date=today(), defaults={"code": secrets.token_urlsafe(16), "active": True, "created_by": request.user},
        )
        log_audit(request.user, "create", detail="Chop etiladigan QR kod (kelganlikni tasdiqlash) yaratildi")
        return self._response(request, obj)

    def delete(self, request):
        PrintedCheckinCode.objects.filter(date=today()).update(active=False)
        log_audit(request.user, "update", detail="Chop etiladigan QR kod bekor qilindi")
        return Response({"active": False, "date": str(today())})


def _code_is_valid(code):
    if code.startswith(checkin.ROTATING_PREFIX):
        return checkin.verify_rotating(code), "kiosk"
    if code.startswith(checkin.PRINTED_PREFIX):
        ok = PrintedCheckinCode.objects.filter(
            date=today(), active=True, code=code[len(checkin.PRINTED_PREFIX):],
        ).exists()
        return ok, "printed"
    return False, ""


class CheckinView(APIView):
    """Bemor shifoxonadagi QR kodni skanerlaydi: bugungi barcha kutilayotgan onlayn
    navbatlari "keldi" deb belgilanadi."""

    permission_classes = [IsPatient]
    throttle_scope = "checkin"

    def post(self, request):
        code = str(request.data.get("code", "")).strip()[:100]
        ok, kind = _code_is_valid(code)
        if not ok:
            security_logger.warning("Yaroqsiz QR kod bilan kelganlikni tasdiqlash: user=%s", request.user.pk)
            return Response(
                {"detail": "QR kod eskirgan yoki noto'g'ri. Shifoxonadagi ekrandan qayta skanerlang.", "code": "bad_code"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        patient = get_object_or_404(Patient, user=request.user)
        tickets = list(QueueTicket.objects.filter(
            patient=patient, date=today(), status__in=QueueTicket.ACTIVE,
        ).select_related("doctor__doctor_profile").order_by("number"))
        if not tickets:
            return Response(
                {"detail": "Bugun sizda faol navbat yo'q.", "code": "no_ticket"},
                status=status.HTTP_404_NOT_FOUND,
            )
        from notifications.services import notify_queue
        newly = []
        for t in tickets:
            if t.status == QueueTicket.WAITING and not t.arrived_at:
                mark_arrived(t)
                newly.append(t)
                notify_queue(t.doctor.doctor_profile)
        if newly:
            log_audit(request.user, "update", patient=patient,
                      detail=f"Shifoxonaga kelish QR orqali tasdiqlandi ({kind}): "
                             + ", ".join(f"№{t.number}" for t in newly))
        return Response({"tickets": [_ticket_data(t) for t in tickets], "confirmed": len(newly)})


class AdminQueueStatsView(APIView):
    """Administrator: navbat statistikasi (?days=7|30|90)."""

    permission_classes = [IsAdmin]

    def get(self, request):
        from .stats import queue_stats
        try:
            days = int(request.query_params.get("days", 30))
        except (TypeError, ValueError):
            days = 30
        return Response(queue_stats(days))
