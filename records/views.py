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
