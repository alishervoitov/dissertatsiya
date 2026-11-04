import logging

from rest_framework import generics, status
from rest_framework.response import Response

from users.models import Role
from users.permissions import IsAdmin, IsAdminOrDoctor, IsPatient

from .audit import log_audit
from .models import AuditLog, MedicalRecord, Patient
from .permissions import MedicalRecordObjectPermission, PatientObjectPermission, can_read_patient
from .serializers import (
    AuditLogSerializer,
    MedicalRecordSerializer,
    PatientListSerializer,
    PatientSerializer,
)
import csv
from django.http import HttpResponse
from rest_framework.views import APIView
from django.http import FileResponse
from .pdf_export import build_patient_history_pdf
from .anonymization import anonymization_report

from django.http import FileResponse
from rest_framework.parsers import MultiPartParser

from .models import RecordAttachment
from .serializers import RecordAttachmentSerializer

from .models import Prescription
from .permissions import PrescriptionObjectPermission
from .serializers import PrescriptionSerializer

security_logger = logging.getLogger("security")


class PatientListView(generics.ListAPIView):
    """Shifokor/administrator uchun barcha bemorlar ro'yxati (qidiruv bilan)."""

    serializer_class = PatientListSerializer
    permission_classes = [IsAdminOrDoctor]

    def get_queryset(self):
        qs = Patient.objects.select_related("user").all()
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(user__first_name__icontains=search) | qs.filter(
                user__last_name__icontains=search
            ) | qs.filter(user__username__icontains=search)
        return qs.order_by("user__first_name")

    def list(self, request, *args, **kwargs):
        from . import consent
        if request.user.role != Role.DOCTOR or not consent.consent_required():
            return super().list(request, *args, **kwargs)
        # Shifokor: qidiruvsiz - faqat ruxsati bor bemorlar; qidiruvda - topilganlar (ko'pi bilan 20 ta),
        # ruxsati yo'qlari uchun faqat tanib olishga yetadigan minimum (ism, login, tug'ilgan sana)
        search = request.query_params.get("search")
        qs = self.get_queryset()
        if not search:
            qs = qs.filter(id__in=consent.accessible_patient_ids(request.user))
        patients = list(qs[:20] if search else qs)
        access = consent.access_map(request.user, [p.id for p in patients])
        data = []
        for p in patients:
            basis = access.get(p.id)
            if basis:
                row = PatientListSerializer(p).data
                row.update(has_access=True, access_via=basis[0], access_until=basis[1])
            else:
                row = {"id": p.id, "full_name": p.user.get_full_name(), "username": p.user.username,
                       "date_of_birth": p.date_of_birth, "has_access": False}
            data.append(row)
        return Response(data)

class MyPatientProfileView(APIView):
    """Bemorning o'zi tizimga kirganda o'z profilini ko'rishi uchun qisqa yo'l."""

    def get(self, request):
        try:
            patient = Patient.objects.get(user=request.user)
        except Patient.DoesNotExist:
            return Response({"detail": "Bemor profili topilmadi."}, status=status.HTTP_404_NOT_FOUND)
        log_audit(request.user, "view", patient=patient, detail="O'z profilini ko'rdi")
        return Response(PatientSerializer(patient).data)


class PatientDetailView(generics.RetrieveUpdateAPIView):
    """
    Bitta bemor profili. Har bir ko'rish AuditLog ga yoziladi -
    bu shifokor/administratorning har bir kirishini kuzatib borish imkonini beradi.
    """

    queryset = Patient.objects.select_related("user", "primary_doctor").all()
    serializer_class = PatientSerializer
    permission_classes = [PatientObjectPermission]

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        actor = request.user
        detail = "O'z profili" if actor.role == Role.PATIENT else "Bemor kartasi ko'rildi"
        log_audit(actor, "view", patient=instance, detail=detail)
        data = self.get_serializer(instance).data
        if actor.role == Role.DOCTOR:
            # Shifokor qaysi asosda ko'rayotganini biladi (favqulodda kirish muddati tugashini ham)
            from . import consent
            basis = consent.access_map(actor, [instance.id]).get(instance.id) if consent.consent_required() else None
            data["access"] = {"via": basis[0], "until": basis[1]} if basis else None
        return Response(data)

    def perform_update(self, serializer):
        instance = serializer.save()
        log_audit(self.request.user, "update", patient=instance, detail="Bemor profili tahrirlandi")


class MedicalRecordListCreateView(generics.ListCreateAPIView):
    serializer_class = MedicalRecordSerializer
    permission_classes = [MedicalRecordObjectPermission]

    def get_queryset(self):
        user = self.request.user
        qs = MedicalRecord.objects.select_related("patient__user", "created_by")

        if user.role == Role.PATIENT:
            qs = qs.filter(patient__user=user)
        elif user.role == Role.DOCTOR:
            # Shifokor faqat roziligi bor (ruxsat, bugungi navbat yoki favqulodda) bemorlar yozuvlarini ko'radi
            from . import consent
            if consent.consent_required():
                pid = self.request.query_params.get("patient")
                if pid and str(pid).isdigit() and not consent.doctor_has_access(user, int(pid)):
                    consent.deny()
                qs = qs.filter(patient_id__in=consent.accessible_patient_ids(user))
        elif user.role == Role.ADMIN:
            pass  # administrator nazorat uchun ko'ra oladi (faqat o'qish)
        else:
            qs = qs.none()  # boshqa rollar (masalan, laborant) klinik yozuvlarni ko'rmaydi

        patient_id = self.request.query_params.get("patient")
        if patient_id:
            qs = qs.filter(patient_id=patient_id)
        return qs.order_by("-visit_date")

    def perform_create(self, serializer):
        from .consent import require_access
        require_access(self.request.user, serializer.validated_data["patient"])
        record = serializer.save(created_by=self.request.user)
        log_audit(
            self.request.user, "create", patient=record.patient, medical_record=record,
            detail=f"Yangi yozuv: {record.title}",
        )

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        patient_id = request.query_params.get("patient")
        if patient_id:
            patient = Patient.objects.filter(id=patient_id).first()
            if patient:
                log_audit(request.user, "view", patient=patient, detail="Yozuvlar ro'yxati ko'rildi")
        return response


class MedicalRecordDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = MedicalRecord.objects.select_related("patient__user", "created_by").all()
    serializer_class = MedicalRecordSerializer
    permission_classes = [MedicalRecordObjectPermission]

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        log_audit(
            request.user, "view", patient=instance.patient, medical_record=instance,
            detail=f"Yozuv ko'rildi: {instance.title}",
        )
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def perform_update(self, serializer):
        record = serializer.save()
        log_audit(
            self.request.user, "update", patient=record.patient, medical_record=record,
            detail=f"Yozuv tahrirlandi: {record.title}",
        )

    def perform_destroy(self, instance):
        log_audit(
            self.request.user, "delete", patient=instance.patient, medical_record=instance,
            detail=f"Yozuv o'chirildi: {instance.title}",
        )
        instance.delete()

class AuditLogListView(generics.ListAPIView):
    """Faqat administrator uchun: tizimdagi barcha kirish/harakatlar tarixi."""

    serializer_class = AuditLogSerializer
    permission_classes = [IsAdmin]

    def get_queryset(self):
        qs = AuditLog.objects.select_related("actor", "patient__user").all()
        patient_id = self.request.query_params.get("patient")
        if patient_id:
            qs = qs.filter(patient_id=patient_id)
        return qs[:500]





class AuditIntegrityView(APIView):
    """Administrator: audit jurnali xesh zanjirini hozir tekshirish."""

    permission_classes = [IsAdmin]

    def get(self, request):
        from .audit_integrity import run_check

        result = run_check(make_checkpoint=False)
        log_audit(request.user, "view",
                  detail=f"Audit jurnali yaxlitligi tekshirildi: {'butun' if result['ok'] else 'BUZILGAN'}")
        return Response(result)


class AnonymizedExportView(APIView):
    permission_classes = [IsAdmin]

    ALL_QI = ("age_group", "gender", "blood_type")

    def get(self, request):
        try:
            k = int(request.query_params.get("k", 5))
        except ValueError:
            k = 5
        k = max(2, min(k, 50))

        qi_param = request.query_params.get("qi")
        if qi_param:
            quasi_identifiers = tuple(q for q in qi_param.split(",") if q in self.ALL_QI)
            if not quasi_identifiers:
                quasi_identifiers = self.ALL_QI
        else:
            quasi_identifiers = self.ALL_QI

        report = anonymization_report(Patient.objects.all(), k=k, quasi_identifiers=quasi_identifiers)

        log_audit(
            request.user, "export",
            detail=f"Anonimlashtirilgan dataset eksport qilindi (k={k}, qi={quasi_identifiers})",
        )

        if request.query_params.get("format") == "csv":
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = f'attachment; filename="anon_dataset_k{k}.csv"'
            writer = csv.writer(response)
            writer.writerow(["yosh_oralig'i", "jinsi", "qon_guruhi", "yozuvlar_soni", "tibbiy_toifalar"])
            for row in report["dataset"]:
                writer.writerow([
                    row["age_group"], row["gender"], row["blood_type"],
                    row["records_count"], "; ".join(row["record_types"]),
                ])
            return response

        return Response(report)



class PatientHistoryPDFView(APIView):
    """
    Bemor o'z tarixini, yoki shifokor/admin istalgan bemor tarixini
    PDF hujjat sifatida yuklab olishi uchun. `?lang=uz|ru|en` parametri
    orqali hujjat tilini tanlash mumkin (standart: uz).
    """

    def get(self, request, patient_id):
        try:
            patient = Patient.objects.select_related("user").get(id=patient_id)
        except Patient.DoesNotExist:
            return Response({"detail": "Bemor topilmadi."}, status=status.HTTP_404_NOT_FOUND)

        # Ruxsat tekshiruvi: bemor faqat o'zinikini, shifokor/admin istalganini, boshqa rollar hech birini
        user = request.user
        from .consent import require_access
        require_access(user, patient)
        if not can_read_patient(user, patient):
            return Response({"detail": "Ruxsat berilmagan."}, status=status.HTTP_403_FORBIDDEN)

        # Eski "laboratoriya natijasi" turidagi matnli yozuvlar o'rniga laborant kiritgan haqiqiy tahlillar beriladi
        records = (MedicalRecord.objects.filter(patient=patient).exclude(record_type="lab_result")
                   .select_related("created_by").order_by("-visit_date"))
        from labs.services import latest_valid_results
        lab_results = latest_valid_results(patient)

        lang = request.query_params.get("lang", "uz")
        if lang not in ("uz", "ru", "en"):
            lang = "uz"

        pdf_buffer = build_patient_history_pdf(patient, records, lang=lang, lab_results=lab_results)

        log_audit(
            user, "export", patient=patient,
            detail=f"Kasallik tarixi PDF sifatida yuklab olindi (til: {lang})",
        )

        filename = f"kasallik_tarixi_{patient.user.username}.pdf"
        return FileResponse(pdf_buffer, as_attachment=True, filename=filename, content_type="application/pdf")





class RecordAttachmentListCreateView(generics.ListCreateAPIView):
    """
    Bitta tibbiy yozuvga biriktirilgan fayllar ro'yxati va yangi fayl
    yuklash. Faqat shifokor fayl yuklay oladi; ko'rish - yozuvga
    kirish huquqi bo'lgan har kim uchun.
    """

    serializer_class = RecordAttachmentSerializer
    parser_classes = [MultiPartParser]

    def get_record(self):
        record = MedicalRecord.objects.select_related("patient__user").get(id=self.kwargs["record_id"])
        # Yozuvga kirish ruxsati bormi - mavjud permission mantig'ini qayta ishlatamiz
        self.check_object_permissions(self.request, record)
        return record

    def get_permissions(self):
        return [MedicalRecordObjectPermission()]

    def get_queryset(self):
        record = self.get_record()
        return RecordAttachment.objects.filter(record=record).select_related("uploaded_by")

    def create(self, request, *args, **kwargs):
        record = self.get_record()
        if request.user.role != Role.DOCTOR:
            return Response({"detail": "Faqat shifokor fayl biriktira oladi."}, status=status.HTTP_403_FORBIDDEN)

        uploaded_file = request.FILES.get("file")
        if not uploaded_file:
            return Response({"detail": "Fayl topilmadi."}, status=status.HTTP_400_BAD_REQUEST)

        if uploaded_file.size > RecordAttachment.MAX_SIZE_BYTES:
            return Response({"detail": "Fayl hajmi 10 MB dan oshmasligi kerak."}, status=status.HTTP_400_BAD_REQUEST)

        # Brauzer aytgan turga (Content-Type) emas, faylning imzosiga ishonamiz
        from .filetypes import detect_type

        real_type = detect_type(uploaded_file)
        if real_type not in RecordAttachment.ALLOWED_CONTENT_TYPES:
            security_logger.warning(
                "Fayl rad etildi: e'lon qilingan tur %s, imzo mos emas (%s, foydalanuvchi %s)",
                uploaded_file.content_type, uploaded_file.name[:100], request.user.username,
            )
            return Response(
                {"detail": "Faqat JPEG, PNG, WEBP rasm yoki PDF fayllar qabul qilinadi."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        attachment = RecordAttachment.objects.create(
            record=record, file=uploaded_file, original_filename=uploaded_file.name[:255],
            content_type=real_type, file_size=uploaded_file.size,
            uploaded_by=request.user,
        )
        log_audit(
            request.user, "create", patient=record.patient, medical_record=record,
            detail=f"Fayl biriktirildi: {attachment.original_filename}",
        )
        return Response(RecordAttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED)


class AttachmentDownloadView(APIView):
    """
    Biriktirilgan faylni yuklab olish - faqat tegishli yozuvga kirish
    huquqi bo'lgan foydalanuvchiga (bemorning o'zi, shifokor, admin).
    """

    def get(self, request, attachment_id):
        try:
            attachment = RecordAttachment.objects.select_related(
                "record__patient__user"
            ).get(id=attachment_id)
        except RecordAttachment.DoesNotExist:
            return Response({"detail": "Fayl topilmadi."}, status=status.HTTP_404_NOT_FOUND)

        user = request.user
        record = attachment.record
        if not can_read_patient(user, record.patient):
            return Response({"detail": "Ruxsat berilmagan."}, status=status.HTTP_403_FORBIDDEN)

        log_audit(
            user, "view", patient=record.patient, medical_record=record,
            detail=f"Fayl yuklab olindi: {attachment.original_filename}",
        )
        return FileResponse(
            attachment.file.open("rb"), as_attachment=True,
            filename=attachment.original_filename, content_type=attachment.content_type,
        )

    def delete(self, request, attachment_id):
        try:
            attachment = RecordAttachment.objects.select_related("record").get(id=attachment_id)
        except RecordAttachment.DoesNotExist:
            return Response({"detail": "Fayl topilmadi."}, status=status.HTTP_404_NOT_FOUND)

        user = request.user
        if user.role != Role.DOCTOR or attachment.uploaded_by_id != user.id:
            return Response({"detail": "Faqat faylni yuklagan shifokor o'chira oladi."}, status=status.HTTP_403_FORBIDDEN)

        detail = f"Fayl o'chirildi: {attachment.original_filename}"
        patient, record = attachment.record.patient, attachment.record
        attachment.file.delete(save=False)
        attachment.delete()
        log_audit(user, "delete", patient=patient, medical_record=record, detail=detail)
        return Response(status=status.HTTP_204_NO_CONTENT)



class PrescriptionListCreateView(generics.ListCreateAPIView):
    serializer_class = PrescriptionSerializer
    permission_classes = [PrescriptionObjectPermission]

    def get_queryset(self):
        user = self.request.user
        qs = Prescription.objects.select_related("patient__user", "prescribed_by")

        if user.role == Role.PATIENT:
            qs = qs.filter(patient__user=user)
        elif user.role == Role.DOCTOR:
            from . import consent
            if consent.consent_required():
                pid = self.request.query_params.get("patient")
                if pid and str(pid).isdigit() and not consent.doctor_has_access(user, int(pid)):
                    consent.deny()
                qs = qs.filter(patient_id__in=consent.accessible_patient_ids(user))
        elif user.role != Role.ADMIN:
            qs = qs.none()

        patient_id = self.request.query_params.get("patient")
        if patient_id:
            qs = qs.filter(patient_id=patient_id)
        return qs.order_by("-start_date")

    def perform_create(self, serializer):
        from .consent import require_access
        require_access(self.request.user, serializer.validated_data["patient"])
        prescription = serializer.save(prescribed_by=self.request.user)
        log_audit(
            self.request.user, "create", patient=prescription.patient,
            detail=f"Retsept yozildi: {prescription.medication_name}",
        )


class PrescriptionDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Prescription.objects.select_related("patient__user", "prescribed_by").all()
    serializer_class = PrescriptionSerializer
    permission_classes = [PrescriptionObjectPermission]

    def perform_update(self, serializer):
        prescription = serializer.save()
        log_audit(
            self.request.user, "update", patient=prescription.patient,
            detail=f"Retsept tahrirlandi: {prescription.medication_name}",
        )

    def perform_destroy(self, instance):
        log_audit(
            self.request.user, "delete", patient=instance.patient,
            detail=f"Retsept o'chirildi: {instance.medication_name}",
        )
        instance.delete()


class MyAccessLogView(APIView):
    """
    "Kartamni kim ko'rdi": bemor o'z tibbiy ma'lumotlariga kim, qachon va qanday
    murojaat qilganini ko'radi (shaxsga doir ma'lumotlar subyektining o'z ma'lumotlari
    kimga berilgani haqida bilish huquqi). Bemorning o'z harakatlari ko'rsatilmaydi.
    """

    permission_classes = [IsPatient]
    MAX_ROWS = 500

    def get(self, request):
        from datetime import timedelta
        from django.utils import timezone
        from .serializers import PatientAccessLogSerializer

        patient = Patient.objects.filter(user=request.user).first()
        if patient is None:
            return Response({"results": [], "summary": {"people": 0, "events": 0}})
        qs = (AuditLog.objects.filter(patient=patient).exclude(actor=request.user)
              .select_related("actor").order_by("-timestamp"))
        days = request.query_params.get("days")
        if days and days.isdigit() and int(days) > 0:
            qs = qs.filter(timestamp__gte=timezone.now() - timedelta(days=int(days)))
        rows = list(qs[: self.MAX_ROWS])
        people = {r.actor_id for r in rows}
        return Response({
            "results": PatientAccessLogSerializer(rows, many=True).data,
            "summary": {"people": len(people), "events": len(rows)},
        })


# ---------- Bemor roziligi va favqulodda kirish ----------

def _doctor_brief(u):
    profile = getattr(u, "doctor_profile", None)
    return {"id": u.id, "name": u.get_full_name() or u.username,
            "specialty": getattr(profile, "specialty", "") or ""}


class MyConsentView(APIView):
    """Bemor: kimga ruxsat berganini ko'radi va yangi ruxsat beradi.
    POST {doctor: id, days: 1|7|30|null}"""

    permission_classes = [IsPatient]
    DURATIONS = (1, 7, 30)

    def _patient(self, request):
        return Patient.objects.get(user=request.user)

    def get(self, request):
        from django.db.models import Q
        from django.utils import timezone

        from appointments.models import QueueTicket
        from appointments.services import today
        from .models import CareGrant, EmergencyAccess

        patient = self._patient(request)
        now = timezone.now()
        grants = (CareGrant.objects.filter(patient=patient, revoked_at__isnull=True)
                  .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
                  .select_related("doctor__doctor_profile"))
        visits = (QueueTicket.objects.filter(patient=patient, date=today(), status__in=("waiting", "called", "done"))
                  .select_related("doctor__doctor_profile"))
        emergencies = EmergencyAccess.objects.filter(patient=patient).select_related("doctor__doctor_profile")[:20]
        return Response({
            "grants": [{"id": g.id, "doctor": _doctor_brief(g.doctor), "created_at": g.created_at,
                        "expires_at": g.expires_at, "note": g.note} for g in grants],
            "visits_today": [_doctor_brief(t.doctor) for t in visits],
            "emergencies": [{"id": e.id, "doctor": _doctor_brief(e.doctor), "reason": e.reason,
                             "created_at": e.created_at, "expires_at": e.expires_at,
                             "active": e.expires_at > now} for e in emergencies],
        })

    def post(self, request):
        from datetime import timedelta

        from django.utils import timezone

        from users.models import User
        from .models import CareGrant

        patient = self._patient(request)
        doctor = User.objects.filter(pk=request.data.get("doctor"), role=Role.DOCTOR, is_active=True).first()
        if not doctor:
            return Response({"detail": "Shifokor topilmadi."}, status=status.HTTP_400_BAD_REQUEST)
        days = request.data.get("days")
        if days not in (None, "", *self.DURATIONS, *map(str, self.DURATIONS)):
            return Response({"detail": "Muddat noto'g'ri."}, status=status.HTTP_400_BAD_REQUEST)
        expires = timezone.now() + timedelta(days=int(days)) if days not in (None, "") else None
        # Shu shifokorga oldingi faol ruxsat yangisi bilan almashtiriladi
        CareGrant.objects.filter(patient=patient, doctor=doctor, revoked_at__isnull=True).update(revoked_at=timezone.now())
        grant = CareGrant.objects.create(patient=patient, doctor=doctor, created_by=request.user, expires_at=expires)
        log_audit(request.user, "update", patient=patient,
                  detail=f"Ruxsat berildi: {doctor.get_full_name() or doctor.username} "
                         f"({'muddatsiz' if expires is None else f'{days} kun'})")
        return Response({"id": grant.id}, status=status.HTTP_201_CREATED)


class MyConsentRevokeView(APIView):
    permission_classes = [IsPatient]

    def post(self, request, pk):
        from django.utils import timezone

        from .models import CareGrant

        grant = CareGrant.objects.filter(pk=pk, patient__user=request.user, revoked_at__isnull=True).select_related("doctor").first()
        if not grant:
            return Response({"detail": "Ruxsat topilmadi."}, status=status.HTTP_404_NOT_FOUND)
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["revoked_at"])
        log_audit(request.user, "update", patient=grant.patient,
                  detail=f"Ruxsat bekor qilindi: {grant.doctor.get_full_name() or grant.doctor.username}")
        return Response({"revoked": True})


class EmergencyAccessView(APIView):
    """Shifokor: roziligi yo'q bemor kartasini favqulodda ochish. POST {reason}"""

    throttle_scope = "emergency"

    def post(self, request, patient_id):
        from . import consent

        if request.user.role != Role.DOCTOR:
            return Response({"detail": "Faqat shifokor uchun."}, status=status.HTTP_403_FORBIDDEN)
        patient = Patient.objects.filter(pk=patient_id).select_related("user").first()
        if not patient:
            return Response({"detail": "Bemor topilmadi."}, status=status.HTTP_404_NOT_FOUND)
        reason = " ".join(str(request.data.get("reason", "")).split())[:500]
        if len(reason) < consent.EMERGENCY_MIN_REASON:
            return Response({"detail": f"Sababni batafsil yozing (kamida {consent.EMERGENCY_MIN_REASON} belgi).",
                             "code": "reason_too_short"}, status=status.HTTP_400_BAD_REQUEST)
        access = consent.open_emergency(request.user, patient, reason)
        security_logger.warning("Favqulodda kirish: shifokor=%s bemor=%s", request.user.pk, patient.pk)
        return Response({"expires_at": access.expires_at}, status=status.HTTP_201_CREATED)


class PatientPhotoView(APIView):
    """Bemorning 3x4 rasmi.
    GET    - kartani ko'ra oladiganlar (bemorning o'zi, roziligi bor shifokor, administrator).
    POST   - multipart {photo, crop_x, crop_y, crop_w, crop_h}: registratura (admin) yoki bemorning o'zi.
    DELETE - registratura (admin) yoki bemorning o'zi."""

    throttle_scope = "patient_photo"

    def _patient(self, patient_id):
        return Patient.objects.filter(pk=patient_id).select_related("user").first()

    def _can_edit(self, user, patient):
        return user.role == Role.ADMIN or (user.role == Role.PATIENT and patient.user_id == user.id)

    def get_throttles(self):
        # Rasmni ko'rish cheklanmaydi; faqat yuklash/o'chirish
        return super().get_throttles() if self.request.method != "GET" else []

    def get(self, request, patient_id):
        from django.http import HttpResponse

        from .permissions import can_read_patient
        from .photo import decode

        patient = self._patient(patient_id)
        if not patient:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if not can_read_patient(request.user, patient):
            if request.user.role == Role.DOCTOR:
                from .consent import deny
                deny()
            return Response(status=status.HTTP_403_FORBIDDEN)
        from .models import PatientPhoto
        photo = PatientPhoto.objects.filter(patient=patient).first()
        data = decode(photo.image) if photo else None
        if not data:
            return Response(status=status.HTTP_404_NOT_FOUND)
        resp = HttpResponse(data, content_type="image/jpeg")
        resp["Cache-Control"] = "private, no-store"
        resp["X-Content-Type-Options"] = "nosniff"
        return resp

    def post(self, request, patient_id):
        from django.utils import timezone

        from .models import PatientPhoto
        from .photo import PhotoError, encode, process_photo

        patient = self._patient(patient_id)
        if not patient:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if not self._can_edit(request.user, patient):
            return Response({"detail": "Rasmni registratura yoki bemorning o'zi yuklaydi."}, status=status.HTTP_403_FORBIDDEN)
        uploaded = request.FILES.get("photo")
        if not uploaded:
            return Response({"detail": "Rasm fayli topilmadi."}, status=status.HTTP_400_BAD_REQUEST)
        crop = {k: request.data.get(f"crop_{k}") for k in ("x", "y", "w", "h")}
        try:
            data = process_photo(uploaded, crop)
        except PhotoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        now = timezone.now()
        PatientPhoto.objects.update_or_create(
            patient=patient, defaults={"image": encode(data), "uploaded_by": request.user, "updated_at": now})
        log_audit(request.user, "update", patient=patient, detail="Bemor rasmi (3x4) yangilandi")
        return Response({"photo_updated_at": now}, status=status.HTTP_201_CREATED)

    def delete(self, request, patient_id):
        from .models import PatientPhoto

        patient = self._patient(patient_id)
        if not patient:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if not self._can_edit(request.user, patient):
            return Response(status=status.HTTP_403_FORBIDDEN)
        deleted, _ = PatientPhoto.objects.filter(patient=patient).delete()
        if deleted:
            log_audit(request.user, "delete", patient=patient, detail="Bemor rasmi (3x4) o'chirildi")
        return Response(status=status.HTTP_204_NO_CONTENT)
