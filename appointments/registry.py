"""Registratura: shifoxonaga kelgan odamni bemor kartasi bilan aniqlash.

Chipta bemorga bog'lanmasa, shifokor kimning kartasiga yozuv qo'shishini bilmaydi
va "bugungi navbat" asosidagi kirish ham ishlamaydi. Shuning uchun registratura
chiptani faqat aniqlangan bemorga beradi, kioskdan olingan anonim chiptani esa
bemor kelganda unga bog'laydi. Bog'lashni faqat administrator (registrator)
qiladi: shifokorga bu huquq berilsa, u istalgan bemorni o'z navbatiga
"biriktirib", rozilik tizimini chetlab o'tishi mumkin bo'lardi.
"""
import re
import secrets

from django.db import transaction
from django.db.models import Q
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from records.audit import log_audit
from records.models import Patient
from users.models import Role, User
from users.permissions import IsAdmin

from .models import QueueTicket
from .services import MAX_ACTIVE_TICKETS_PER_PATIENT, today

SEARCH_LIMIT = 10
PHONE_RE = re.compile(r"^\+?[0-9 ()-]{7,20}$")
LINKABLE = (QueueTicket.WAITING, QueueTicket.CALLED, QueueTicket.DONE)


def patient_brief(p):
    u = p.user
    return {
        "id": p.id,
        "full_name": u.get_full_name() or u.username,
        "username": u.username,
        "date_of_birth": p.date_of_birth,
        "gender": p.gender,
        "phone": u.phone,
        # Registraturada ochilgan va hali ilovaga kirmagan bemor
        "has_login": u.has_usable_password(),
    }


def search_patients(q="", dob=None):
    qs = Patient.objects.select_related("user").filter(user__is_active=True)
    words = [w for w in (q or "").split() if w]
    if not words and not dob:
        return Patient.objects.none()
    for w in words[:4]:
        digits = re.sub(r"\D", "", w)
        cond = Q(user__first_name__icontains=w) | Q(user__last_name__icontains=w) | Q(user__username__icontains=w)
        if len(digits) >= 4:
            cond |= Q(user__phone__icontains=digits)
        qs = qs.filter(cond)
    if dob:
        qs = qs.filter(date_of_birth=dob)
    return qs.order_by("user__last_name", "user__first_name")[:SEARCH_LIMIT]


class NewPatientSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    date_of_birth = serializers.DateField()
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    gender = serializers.ChoiceField(choices=["M", "F", ""], required=False, allow_blank=True)
    confirm = serializers.BooleanField(required=False, default=False)

    def validate_first_name(self, v):
        v = " ".join(v.split())
        if not v:
            raise serializers.ValidationError("Ismni kiriting.")
        return v

    validate_last_name = validate_first_name

    def validate_phone(self, v):
        v = v.strip()
        if v and not PHONE_RE.match(v):
            raise serializers.ValidationError("Telefon raqami noto'g'ri.")
        return v

    def validate_date_of_birth(self, v):
        if v > today() or v.year < 1900:
            raise serializers.ValidationError("Tug'ilgan sana noto'g'ri.")
        return v


def _new_username():
    for _ in range(20):
        name = f"b{secrets.randbelow(10 ** 7):07d}"
        if not User.objects.filter(username__iexact=name).exists():
            return name
    raise RuntimeError("username")


class RegistryPatientView(APIView):
    """GET ?q=ism|familiya|telefon&dob=YYYY-MM-DD - bemorni topish.
    POST - bazada yo'q bemorni registraturada ochish (paroli yo'q, keyin o'zi tiklaydi)."""

    permission_classes = [IsAdmin]

    def get(self, request):
        dob = request.query_params.get("dob") or None
        if dob:
            try:
                dob = serializers.DateField().to_internal_value(dob)
            except serializers.ValidationError:
                return Response({"detail": "Sana noto'g'ri."}, status=status.HTTP_400_BAD_REQUEST)
        found = search_patients(request.query_params.get("q", "").strip()[:100], dob)
        return Response([patient_brief(p) for p in found])

    def post(self, request):
        s = NewPatientSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        same = Patient.objects.select_related("user").filter(
            user__first_name__iexact=d["first_name"], user__last_name__iexact=d["last_name"],
            date_of_birth=d["date_of_birth"],
        )
        if same.exists() and not d["confirm"]:
            # Ikki marta ochilgan karta - tarix ikkiga bo'linib ketadi; registrator tasdiqlashi kerak
            return Response({"detail": "Bu ism va tug'ilgan sana bilan bemor bor.", "code": "possible_duplicate",
                             "matches": [patient_brief(p) for p in same[:5]]}, status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            user = User(username=_new_username(), first_name=d["first_name"], last_name=d["last_name"],
                        phone=d.get("phone", ""), role=Role.PATIENT)
            user.set_unusable_password()
            user.save()
            patient = Patient.objects.get(user=user)
            patient.date_of_birth = d["date_of_birth"]
            patient.gender = d.get("gender", "")
            patient.save(update_fields=["date_of_birth", "gender"])
        log_audit(request.user, "create", patient=patient, detail="Registraturada yangi bemor kartasi ochildi")
        return Response(patient_brief(patient), status=status.HTTP_201_CREATED)


class RegistryLinkTicketView(APIView):
    """POST {patient} - bugungi anonim chiptani (kiosk) aniqlangan bemorga bog'lash."""

    permission_classes = [IsAdmin]

    def post(self, request, pk):
        from .views import _ticket_doctor_data

        patient = Patient.objects.select_related("user").filter(pk=request.data.get("patient")).first()
        if not patient:
            return Response({"detail": "Bemor topilmadi."}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            ticket = (QueueTicket.objects.select_for_update()
                      .filter(pk=pk, date=today(), status__in=LINKABLE).first())
            if not ticket:
                return Response({"detail": "Chipta topilmadi."}, status=status.HTTP_404_NOT_FOUND)
            if ticket.patient_id:
                return Response({"detail": "Chipta allaqachon bemorga bog'langan.", "code": "already_linked"},
                                status=status.HTTP_409_CONFLICT)
            active = QueueTicket.objects.filter(patient=patient, date=ticket.date, status__in=QueueTicket.ACTIVE)
            if active.filter(doctor_id=ticket.doctor_id).exists():
                return Response({"detail": "Bu bemorning shu shifokorga bugun boshqa navbati bor.", "code": "duplicate"},
                                status=status.HTTP_409_CONFLICT)
            if ticket.status in QueueTicket.ACTIVE and active.count() >= MAX_ACTIVE_TICKETS_PER_PATIENT:
                return Response({"detail": "Bir kunda ko'pi bilan 3 ta faol navbat olish mumkin.", "code": "limit"},
                                status=status.HTTP_409_CONFLICT)
            ticket.patient = patient
            ticket.visitor_name = ""
            ticket.save(update_fields=["patient", "visitor_name"])
        doctor = ticket.doctor.get_full_name() or ticket.doctor.username
        log_audit(request.user, "update", patient=patient,
                  detail=f"Navbat chiptasi bemorga bog'landi: №{ticket.number}, {doctor}")
        return Response(_ticket_doctor_data(ticket))
