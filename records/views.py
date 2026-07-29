from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

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
