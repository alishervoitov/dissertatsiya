from rest_framework import generics, status
from rest_framework.response import Response

from users.models import Role
from users.permissions import IsAdmin, IsAdminOrDoctor

from .audit import log_audit
from .models import AuditLog, MedicalRecord, Patient
from .permissions import MedicalRecordObjectPermission, PatientObjectPermission
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
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

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
            pass  # shifokor davolash uchun barcha bemorlar yozuvlarini ko'ra oladi
        elif user.role == Role.ADMIN:
            pass  # administrator nazorat uchun ko'ra oladi (faqat o'qish)

        patient_id = self.request.query_params.get("patient")
        if patient_id:
            qs = qs.filter(patient_id=patient_id)
        return qs.order_by("-visit_date")

    def perform_create(self, serializer):
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
    PDF hujjat sifatida yuklab olishi uchun.
    """

    def get(self, request, patient_id):
        try:
            patient = Patient.objects.select_related("user").get(id=patient_id)
        except Patient.DoesNotExist:
            return Response({"detail": "Bemor topilmadi."}, status=status.HTTP_404_NOT_FOUND)

        # Ruxsat tekshiruvi: bemor faqat o'zinikini, shifokor/admin istalganini
        user = request.user
        if user.role == Role.PATIENT and patient.user_id != user.id:
            return Response({"detail": "Ruxsat berilmagan."}, status=status.HTTP_403_FORBIDDEN)

        records = MedicalRecord.objects.filter(patient=patient).select_related("created_by").order_by("-visit_date")

        pdf_buffer = build_patient_history_pdf(patient, records)

        log_audit(
            user, "export", patient=patient,
            detail="Kasallik tarixi PDF sifatida yuklab olindi",
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

        if uploaded_file.content_type not in RecordAttachment.ALLOWED_CONTENT_TYPES:
            return Response(
                {"detail": "Faqat JPEG, PNG, WEBP rasm yoki PDF fayllar qabul qilinadi."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        attachment = RecordAttachment.objects.create(
            record=record, file=uploaded_file, original_filename=uploaded_file.name,
            content_type=uploaded_file.content_type, file_size=uploaded_file.size,
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
        if user.role == Role.PATIENT and record.patient.user_id != user.id:
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

        patient_id = self.request.query_params.get("patient")
        if patient_id:
            qs = qs.filter(patient_id=patient_id)
        return qs.order_by("-start_date")

    def perform_create(self, serializer):
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